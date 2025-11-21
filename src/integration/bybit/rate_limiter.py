from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Dict, Type

from src.core.exceptions import RateLimitExceededError


__all__ = [
    "RateLimitTimeoutError",
    "WSRateLimitError",
    "RateLimiterBybit",
]


class RateLimitTimeoutError(RateLimitExceededError):
    """
    Превышено допустимое время ожидания при попытке получить токены
    из rate limiter'а (REST / ордера / WS-подписки).
    """

    pass


class WSRateLimitError(RateLimitExceededError):
    """
    Специализированное исключение для превышения лимитов подписок WebSocket.

    Используется, когда за отведённое время не удаётся получить токен
    для новой подписки.
    """

    pass


@dataclass
class _Bucket:
    """
    Ведро токенов для одного типа запросов.

    capacity    — максимальное количество токенов;
    refill_rate — скорость пополнения в токенах/сек;
    tokens      — текущее количество токенов (float для плавного пополнения);
    last_refill — монотонное время последнего пополнения;
    lock        — asyncio.Lock для конкурентного доступа.
    """

    capacity: float
    refill_rate: float
    tokens: float
    last_refill: float
    lock: asyncio.Lock


class RateLimiterBybit:
    """
    Простейший token-bucket rate limiter для Bybit REST/WS.

    Лимиты (по умолчанию):
      * READ  — до 1200 запросов в минуту (≈20 req/sec);
      * ORDER — до 10 ордеров в секунду;
      * WS SUBSCRIPTIONS — до 30 подписок в секунду.

    При исчерпании токенов вызывающая корутина ждёт с экспоненциальным
    backoff'ом и jitter'ом, но не дольше заданного таймаута, после чего
    выбрасывается соответствующее исключение.
    """

    # Глобальные лимиты (можно при необходимости сделать конфигурируемыми)
    _READ_PER_MINUTE = 1200
    _ORDER_PER_SECOND = 10
    _WS_SUBS_PER_SECOND = 30

    # Таймауты ожидания (секунды)
    _READ_TIMEOUT_SEC = 5.0
    _ORDER_TIMEOUT_SEC = 3.0
    # Для WS берём более жёсткий таймаут: подписки обычно происходят пачкой при старте.
    _WS_TIMEOUT_SEC = 2.0

    def __init__(self) -> None:
        now = time.monotonic()
        self._buckets: Dict[str, _Bucket] = {
            "read": _Bucket(
                capacity=float(self._READ_PER_MINUTE),
                refill_rate=float(self._READ_PER_MINUTE) / 60.0,  # токенов/сек
                tokens=float(self._READ_PER_MINUTE),
                last_refill=now,
                lock=asyncio.Lock(),
            ),
            "order": _Bucket(
                capacity=float(self._ORDER_PER_SECOND),
                refill_rate=float(self._ORDER_PER_SECOND),
                tokens=float(self._ORDER_PER_SECOND),
                last_refill=now,
                lock=asyncio.Lock(),
            ),
            "ws": _Bucket(
                capacity=float(self._WS_SUBS_PER_SECOND),
                refill_rate=float(self._WS_SUBS_PER_SECOND),
                tokens=float(self._WS_SUBS_PER_SECOND),
                last_refill=now,
                lock=asyncio.Lock(),
            ),
        }

    # ------------------------------------------------------------------ #
    # Публичный API                                                      #
    # ------------------------------------------------------------------ #

    async def consume_read(self, weight: int = 1) -> None:
        """
        Потребить `weight` токенов из бакета для READ-запросов.

        Используется всеми "читающими" REST-вызовами (market data, позиции и т.п.).
        """
        tokens = float(max(weight, 1))
        await self._wait_for_tokens(
            bucket_name="read",
            tokens=tokens,
            timeout=self._READ_TIMEOUT_SEC,
            error_cls=RateLimitTimeoutError,
        )

    async def consume_order(self) -> None:
        """
        Потребить один токен из бакета для ордеров (лимит ~10 req/sec).

        Если в течение `_ORDER_TIMEOUT_SEC` получить токен не удаётся,
        выбрасывается `RateLimitTimeoutError`.
        """
        await self._wait_for_tokens(
            bucket_name="order",
            tokens=1.0,
            timeout=self._ORDER_TIMEOUT_SEC,
            error_cls=RateLimitTimeoutError,
        )

    async def consume_ws_subscription(self) -> None:
        """
        Потребить один токен из бакета для подписок WebSocket.

        При невозможности получить токен за `_WS_TIMEOUT_SEC` выбрасывается
        `WSRateLimitError`.
        """
        await self._wait_for_tokens(
            bucket_name="ws",
            tokens=1.0,
            timeout=self._WS_TIMEOUT_SEC,
            error_cls=WSRateLimitError,
        )

    # ------------------------------------------------------------------ #
    # Внутренние методы                                                  #
    # ------------------------------------------------------------------ #

    async def _wait_for_tokens(
        self,
        *,
        bucket_name: str,
        tokens: float,
        timeout: float,
        error_cls: Type[RateLimitExceededError],
    ) -> None:
        """
        Дождаться появления `tokens` токенов в указанном бакете.

        Реализует:
          * пополнение ведра по времени;
          * экспоненциальный backoff с jitter'ом;
          * общий таймаут ожидания, после которого кидает `error_cls`.
        """
        bucket = self._buckets[bucket_name]
        start_time = time.monotonic()
        deadline = start_time + timeout
        attempt = 0

        while True:
            async with bucket.lock:
                self._refill_bucket(bucket)
                if bucket.tokens >= tokens:
                    bucket.tokens -= tokens
                    return

                # Сколько нужно токенов дополнительно
                needed = max(tokens - bucket.tokens, 0.0)
                # Сколько примерно ждать до пополнения нужного количества
                base_sleep = (
                    needed / bucket.refill_rate if bucket.refill_rate > 0 else 0.01
                )

            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                raise error_cls(
                    "Rate limit exceeded while waiting for tokens",
                    details={
                        "bucket": bucket_name,
                        "requested_tokens": tokens,
                        "timeout_sec": timeout,
                        "attempts": attempt,
                    },
                )

            # Экспоненциальный backoff с небольшим jitter'ом
            attempt += 1
            backoff = min(base_sleep * (2 ** (attempt - 1)), remaining)
            jitter = random.uniform(0.9, 1.1)
            sleep_for = max(min(backoff * jitter, remaining), 0.01)

            await asyncio.sleep(sleep_for)

    @staticmethod
    def _refill_bucket(bucket: _Bucket) -> None:
        """
        Пополнить ведро токенов в соответствии с прошедшим временем.

        Используется монотонное время, чтобы не зависеть от изменений системных часов.
        """
        now = time.monotonic()
        elapsed = now - bucket.last_refill
        if elapsed <= 0:
            return

        added = elapsed * bucket.refill_rate
        if added <= 0:
            bucket.last_refill = now
            return

        bucket.tokens = min(bucket.capacity, bucket.tokens + added)
        bucket.last_refill = now
