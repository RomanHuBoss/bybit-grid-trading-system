from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Dict, Type

from src.core.exceptions import RateLimitExceededError


class RateLimitTimeoutError(RateLimitExceededError):
    """
    Специализированное исключение: превышено допустимое время ожидания
    при попытке получить токены из rate limiter'а.
    """
    pass


class WSRateLimitError(RateLimitExceededError):
    """
    Исключение для превышения лимитов подписок WebSocket Bybit.
    Используется, когда система не успевает уложиться в лимиты
    по частоте подписок.
    """
    pass


@dataclass
class _Bucket:
    """
    Состояние одного token bucket.

    capacity:
        Максимальное количество токенов (размер "ведра").
    refill_rate:
        Скорость пополнения в токенах в секунду.
    tokens:
        Текущее количество токенов.
    last_refill:
        Время (monotonic), когда ведро в последний раз пополнялось.
    lock:
        Асинхронный лок для конкурентного доступа из нескольких корутин.
    """
    capacity: float
    refill_rate: float
    tokens: float
    last_refill: float
    lock: asyncio.Lock


class RateLimiterBybit:
    """
    Асинхронный token-bucket rate limiter для лимитов Bybit.

    Лимиты по умолчанию (из спецификации / R-05):
        - REST read: 1200 запросов в минуту;
        - ордера: 10 запросов в секунду;
        - WS-подписки: 30 подписок в секунду.

    Для каждой категории поддерживается отдельный bucket.
    При исчерпании токенов вызывающая корутина ждёт с backoff'ом и jitter'ом.
    """

    # Константы лимитов по умолчанию
    _READ_PER_MINUTE = 1200
    _ORDER_PER_SECOND = 10
    _WS_SUBS_PER_SECOND = 30

    # Таймауты ожидания
    _READ_TIMEOUT_SEC = 5.0
    _ORDER_TIMEOUT_SEC = 3.0
    # Для WS берём более жёсткий таймаут: подписки обычно происходят сериями при старте.
    _WS_TIMEOUT_SEC = 2.0

    def __init__(self) -> None:
        now = time.monotonic()
        self._buckets: Dict[str, _Bucket] = {
            "read": _Bucket(
                capacity=float(self._READ_PER_MINUTE),
                refill_rate=float(self._READ_PER_MINUTE) / 60.0,
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
            "ws_sub": _Bucket(
                capacity=float(self._WS_SUBS_PER_SECOND),
                refill_rate=float(self._WS_SUBS_PER_SECOND),
                tokens=float(self._WS_SUBS_PER_SECOND),
                last_refill=now,
                lock=asyncio.Lock(),
            ),
        }

    async def consume_read(self, n: int = 1) -> None:
        """
        Потребить n токенов из бакета для REST-чтения (лимит 1200 req/min).

        Если в течение _READ_TIMEOUT_SEC (≈5с) получить токены не удаётся,
        выбрасывается RateLimitTimeoutError.
        """
        if n <= 0:
            raise ValueError("n must be positive for consume_read")
        await self._wait_for_tokens(
            bucket_name="read",
            tokens=float(n),
            timeout=self._READ_TIMEOUT_SEC,
            error_cls=RateLimitTimeoutError,
        )

    async def consume_order(self) -> None:
        """
        Потребить один токен из бакета для ордеров (лимит 10 req/sec).

        Если в течение _ORDER_TIMEOUT_SEC (≈3с) получить токен не удаётся,
        выбрасывается RateLimitTimeoutError.
        """
        await self._wait_for_tokens(
            bucket_name="order",
            tokens=1.0,
            timeout=self._ORDER_TIMEOUT_SEC,
            error_cls=RateLimitTimeoutError,
        )

    async def consume_ws_subscription(self) -> None:
        """
        Потребить один токен из бакета для WS-подписок (лимит 30 subs/sec).

        Если в течение _WS_TIMEOUT_SEC получить токен не удаётся, выбрасывается
        WSRateLimitError, сигнализирующий о невозможности укладываться в
        текущие лимиты подписок.
        """
        await self._wait_for_tokens(
            bucket_name="ws_sub",
            tokens=1.0,
            timeout=self._WS_TIMEOUT_SEC,
            error_cls=WSRateLimitError,
        )

    async def _wait_for_tokens(
        self,
        bucket_name: str,
        tokens: float,
        timeout: float | None,
        error_cls: Type[RateLimitExceededError],
    ) -> None:
        """
        Ожидание появления нужного количества токенов в выбранном bucket'е.

        Внутри используется asyncio.wait_for вокруг _acquire_tokens, поэтому
        при превышении таймаута сначала возникает asyncio.TimeoutError, которая
        затем маппится в доменно-специфичное исключение error_cls.
        """
        try:
            if timeout is None:
                await self._acquire_tokens(bucket_name=bucket_name, tokens=tokens)
            else:
                await asyncio.wait_for(
                    self._acquire_tokens(bucket_name=bucket_name, tokens=tokens),
                    timeout=timeout,
                )
        except asyncio.TimeoutError as exc:
            # Преобразуем низкоуровневый timeout в доменное исключение.
            raise error_cls(
                message=(
                    f"Rate limit timeout in bucket '{bucket_name}' "
                    f"while waiting for {tokens} tokens (timeout={timeout}s)."
                ),
                details={
                    "bucket": bucket_name,
                    "tokens_requested": tokens,
                    "timeout": timeout,
                },
            ) from exc

    async def _acquire_tokens(self, bucket_name: str, tokens: float) -> None:
        """
        Блокирующее (по-асинхронному) ожидание до тех пор, пока в bucket'е
        не накопится достаточное количество токенов.
        """
        bucket = self._buckets[bucket_name]

        while True:
            # Работаем под локом, чтобы несколько корутин не "перерасходовали" ведро.
            async with bucket.lock:
                self._refill_bucket(bucket)
                if bucket.tokens >= tokens:
                    bucket.tokens -= tokens
                    return

                # Сколько времени понадобится на пополнение недостающих токенов
                needed = tokens - bucket.tokens
                # Защита от деления на ноль, хотя по спецификации refill_rate > 0.
                if bucket.refill_rate <= 0:
                    # Это программная ошибка конфигурации лимитов.
                    raise RuntimeError(
                        "Refill rate must be positive for rate limiter bucket",
                    )

                base_sleep = needed / bucket.refill_rate

            # Выйдя из лока — спим. Добавляем небольшой jitter, чтобы сгладить шипы.
            jitter_factor = random.uniform(0.9, 1.1)
            sleep_for = max(base_sleep * jitter_factor, 0.0)
            await asyncio.sleep(sleep_for)

    def _refill_bucket(self, bucket: _Bucket) -> None:
        """
        Пополнение bucket'а на основании прошедшего времени.

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
