from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

import httpx

from src.core.logging_config import get_logger

__all__ = ["WebhookEndpoint", "WebhookNotifier"]

logger = get_logger("notifications.webhooks")


@dataclass(frozen=True)
class WebhookEndpoint:
    """
    Описание одного webhook-приёмника.

    name    — человекочитаемое имя (например, "slack-trading");
    url     — полный URL endpoint'а;
    secret  — опциональный секрет для HMAC-подписи;
    enabled — если False, endpoint игнорируется.
    """

    name: str
    url: str
    secret: Optional[str] = None
    enabled: bool = True


class WebhookNotifier:
    """
    Асинхронный нотификатор внешних систем через HTTP webhooks.

    Обязанности:
      - формировать единый envelope:
            {
              "event_type": "...",
              "timestamp": "...",   # ISO8601 UTC
              "strategy": "AVI-5",
              "environment": "...",
              "data": {...}        # доменный payload
            }
      - подписывать тело HMAC-SHA256 при наличии секрета;
      - отправлять в несколько endpoints параллельно с ограничением concurrency;
      - реализовывать retries/backoff для временных ошибок.

    Внешняя API:
      - notify(event_type, payload, context) — high-level метод, который
        отправит уведомление во все включённые endpoints.
    """

    def __init__(
        self,
        endpoints: Sequence[WebhookEndpoint],
        *,
        strategy_name: str = "AVI-5",
        environment: str = "prod",
        timeout_seconds: float = 3.0,
        max_retries: int = 3,
        concurrency_limit: int = 5,
    ) -> None:
        self._endpoints = [ep for ep in endpoints if ep.enabled]
        self._strategy_name = strategy_name
        self._environment = environment
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._semaphore = asyncio.Semaphore(max(1, concurrency_limit))

    # ------------------------------------------------------------------ #
    # Публичный API                                                      #
    # ------------------------------------------------------------------ #

    async def notify(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """
        Отправить уведомление о событии во все активные webhook endpoints.

        :param event_type: Тип события (например, "signal_opened", "kill_switch").
        :param payload: Доменный payload события.
        :param context: Дополнительный контекст (user_id, account_id, и т.п.).

        Ошибки доставки **не** пробрасываются наружу: они логируются по каждому
        endpoint отдельно. Критический путь работы стратегии не должен падать
        из-за проблем внешних интеграций.
        """
        if not self._endpoints:
            return

        envelope = self._build_envelope(event_type=event_type, payload=payload, context=context)
        body = json.dumps(envelope, default=str).encode("utf-8")

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            tasks = [
                self._send_to_endpoint(client, endpoint, event_type, body)
                for endpoint in self._endpoints
            ]
            # Собираем все результаты, но исключения внутри обрабатываем сами.
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # Внутренние помощники                                               #
    # ------------------------------------------------------------------ #

    def _build_envelope(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        context: Optional[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """
        Сформировать единый envelope для вебхука.

        Структура:
            {
              "event_type": "...",
              "timestamp": "...",   # ISO8601 UTC
              "strategy": "AVI-5",
              "environment": "...",
              "data": {...},
              "context": {...}      # опционально
            }
        """
        now = datetime.now(timezone.utc).isoformat()

        envelope: dict[str, Any] = {
            "event_type": event_type,
            "timestamp": now,
            "strategy": self._strategy_name,
            "environment": self._environment,
            "data": dict(payload),
        }

        if context:
            envelope["context"] = dict(context)

        return envelope

    @staticmethod
    def _compute_signature(secret: str, body: bytes) -> str:
        """
        Посчитать HMAC-SHA256 подпись для тела запроса.

        Возвращается hex-строка, которая кладётся в заголовок `X-Webhook-Signature`.
        """
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return digest

    async def _send_to_endpoint(
        self,
        client: httpx.AsyncClient,
        endpoint: WebhookEndpoint,
        event_type: str,
        body: bytes,
    ) -> None:
        """
        Отправить уведомление в конкретный endpoint с retry/backoff.

        Ретраи выполняются только для:
            - сетевых ошибок (httpx.RequestError),
            - ответов 5xx.

        4xx считаем неретрабельными (ошибка конфигурации/формата).
        """
        if not endpoint.enabled:
            return

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Event": event_type,
        }

        if endpoint.secret:
            signature = self._compute_signature(endpoint.secret, body)
            headers["X-Webhook-Signature"] = signature

        attempt = 0
        while True:
            try:
                async with self._semaphore:
                    response = await client.post(endpoint.url, content=body, headers=headers)

                status_code = response.status_code

                if 200 <= status_code < 300:
                    # Успех — логируем на уровне debug.
                    logger.debug(
                        "Webhook delivered",
                        extra={
                            "endpoint": endpoint.name,
                            "url": endpoint.url,
                            "status_code": status_code,
                            "event_type": event_type,
                        },
                    )
                    return

                # 4xx — неретрабельные ошибки, кроме 429 (можно рассматривать отдельно).
                if 400 <= status_code < 500 and status_code != 429:
                    logger.error(
                        "Non-retriable webhook error",
                        extra={
                            "endpoint": endpoint.name,
                            "url": endpoint.url,
                            "status_code": status_code,
                            "body": response.text[:500],
                            "event_type": event_type,
                        },
                    )
                    return

                # 5xx или 429 — кандидат на retry.
                attempt += 1
                if attempt > self._max_retries:
                    logger.error(
                        "Webhook delivery failed after retries",
                        extra={
                            "endpoint": endpoint.name,
                            "url": endpoint.url,
                            "status_code": status_code,
                            "event_type": event_type,
                        },
                    )
                    return

                backoff = 0.5 * (2 ** (attempt - 1))  # 0.5, 1.0, 2.0, ...
                logger.warning(
                    "Transient webhook error, will retry",
                    extra={
                        "endpoint": endpoint.name,
                        "url": endpoint.url,
                        "status_code": status_code,
                        "attempt": attempt,
                        "max_retries": self._max_retries,
                        "backoff_seconds": backoff,
                        "event_type": event_type,
                    },
                )
                await asyncio.sleep(backoff)

            except httpx.RequestError as exc:
                # Сетевые ошибки → тоже ретраи, пока не исчерпаем лимит.
                attempt += 1
                if attempt > self._max_retries:
                    logger.error(
                        "Webhook delivery failed due to network error after retries",
                        extra={
                            "endpoint": endpoint.name,
                            "url": endpoint.url,
                            "error": str(exc),
                            "event_type": event_type,
                        },
                    )
                    return

                backoff = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "Network error while sending webhook, will retry",
                    extra={
                        "endpoint": endpoint.name,
                        "url": endpoint.url,
                        "error": str(exc),
                        "attempt": attempt,
                        "max_retries": self._max_retries,
                        "backoff_seconds": backoff,
                        "event_type": event_type,
                    },
                )
                await asyncio.sleep(backoff)
            except Exception as exc:  # noqa: BLE001
                # Любая другая ошибка — логируем и выходим без ретраев.
                logger.error(
                    "Unexpected error while sending webhook",
                    extra={
                        "endpoint": endpoint.name,
                        "url": endpoint.url,
                        "error": str(exc),
                        "event_type": event_type,
                    },
                )
                return
