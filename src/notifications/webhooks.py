# src/notifications/webhooks.py

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from aiohttp import ClientError, ClientSession

from src.core.exceptions import WebhookError, WebhookHTTPError
from src.core.logging_config import get_logger
from src.core.models import Position


class WebhookNotifier:
    """
    Отправка webhook-уведомлений во внешний сервис (Telegram/Slack и т.п.).

    Экземпляр конфигурируется URL, секретом для HMAC-подписи и простой retry-политикой.
    Жизненным циклом ClientSession управляет вызывающая сторона.
    """

    def __init__(
        self,
        session: ClientSession,
        url: str,
        secret: str,
        timeout: float = 5.0,
        max_retries: int = 3,
    ) -> None:
        if not url:
            raise ValueError("Webhook URL must not be empty")
        if timeout <= 0:
            raise ValueError("Webhook timeout must be positive")
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")

        self._session = session
        self._url = url
        self._secret = secret
        self._timeout = timeout
        self._max_retries = max_retries

    def _sign_payload(self, payload: Dict[str, Any]) -> str:
        """
        Подписывает payload с помощью HMAC-SHA256.

        Используется детерминированное JSON-представление (отсортированные ключи),
        чтобы подпись не зависела от порядка ключей.

        :raises ValueError: если секрет пустой.
        """
        if not self._secret:
            raise ValueError("Webhook secret must not be empty")

        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        mac = hmac.new(self._secret.encode("utf-8"), body, hashlib.sha256)
        return mac.hexdigest()

    async def send(self, payload: Dict[str, Any]) -> None:
        """
        Отправка произвольного JSON-payload на сконфигурированный webhook.

        Используется AlertManager-ом и другими компонентами, которым нужен
        общий механизм отправки с подписью и retry.

        :raises WebhookError: при сетевых ошибках или исчерпании ретраев.
        :raises WebhookHTTPError: при неуспешном HTTP-статусе (4xx/5xx).
        """
        signature = self._sign_payload(payload)
        headers = {
            "Content-Type": "application/json",
            "X-Signature": signature,
        }

        last_exc: Optional[BaseException] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                async with (self._session.post(
                    self._url,
                    json=payload,
                    timeout=self._timeout,
                    headers=headers,
                ) as resp):
                    if 200 <= resp.status < 300:
                        # Успешный ответ — выходим.
                        return

                    body_text = await resp.text()
                    logger = get_logger("notifications.webhooks")
                    logger.error(
                        "webhook_http_error",
                        url=self._url,
                        status=resp.status,
                        body=body_text,
                        attempt=attempt
                    )
                    last_exc = WebhookHTTPError(
                        f"Webhook responded with HTTP {resp.status}",
                        details={"status": resp.status, "body": body_text},
                    )
            except (ClientError, asyncio.TimeoutError) as exc:
                # Сетевые/таймаут-ошибки — логируем и готовим WebhookError.
                logger = get_logger("notifications.webhooks")
                logger.error(
                    "webhook_send_error",
                    url=self._url,
                    attempt=attempt,
                    error=str(exc)
                )
                last_exc = WebhookError(
                    "Failed to send webhook request",
                    details={"error": str(exc), "attempt": attempt},
                )

            if attempt < self._max_retries:
                # Простейший backoff: 1с, 2с, 3с, ...
                await asyncio.sleep(attempt)
            else:
                break

        # Все попытки исчерпаны — пробрасываем последнее исключение.
        if last_exc is not None:
            raise last_exc

        # Теоретически сюда не должны попасть, но оставляем safety-net.
        raise WebhookError("Unknown webhook error without exception context")

    async def send_be_event(
        self,
        position: Position,
        triggered_at: Optional[datetime] = None,
    ) -> None:
        """
        Отправка BE-события согласно спецификации формата:

        {
            "event": "be_triggered",
            "position_id": "...",
            "symbol": "BTCUSDT",
            "at": "2025-01-01T00:00:00Z"
        }

        :param position: доменная модель позиции.
        :param triggered_at: момент срабатывания BE; по умолчанию — сейчас (UTC).
        :raises WebhookError: при проблемах с формированием payload.
        :raises WebhookHTTPError: при неуспешном HTTP-ответе.
        """
        if triggered_at is None:
            triggered_at = datetime.now(timezone.utc)

        # Аккуратно вытаскиваем id и symbol, чтобы не завязываться жёстко
        # на конкретные имена полей, но всё же валидировать их наличие.
        position_id = getattr(position, "id", None)
        if position_id is None:
            position_id = getattr(position, "position_id", None)

        symbol = getattr(position, "symbol", None)

        if position_id is None:
            raise WebhookError(
                "Position id is missing for BE event payload",
                details={"position": repr(position)},
            )

        if symbol is None:
            raise WebhookError(
                "Position symbol is missing for BE event payload",
                details={"position": repr(position)},
            )

        payload: Dict[str, Any] = {
            "event": "be_triggered",
            "position_id": str(position_id),
            "symbol": symbol,
            "at": triggered_at.astimezone(timezone.utc)
            .replace(tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }

        await self.send(payload)


async def send(
    position: Position,
    triggered_at: Optional[datetime],
    notifier: WebhookNotifier,
) -> None:
    """
    Фасад для совместимости с использованием из RiskManager:

    - в спецификации `generate_be_event` ссылается на `notifications.webhooks.send`.

    Предполагается, что вызывающий код управляет созданием и переиспользованием
    экземпляра WebhookNotifier (URL, секрет, retry-политика и т.д.).

    :param position: позиция, по которой сработал BE-ивент.
    :param triggered_at: момент срабатывания BE.
    :param notifier: сконфигурированный WebhookNotifier.
    """
    await notifier.send_be_event(position=position, triggered_at=triggered_at)
