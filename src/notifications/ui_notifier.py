# src/notifications/ui_notifier.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional
from uuid import uuid4

from redis.asyncio import Redis

from src.core.logging_config import get_logger
from src.core.models import Position, Signal

__all__ = ["UIEventType", "UIEnvelope", "UINotifier"]

logger = get_logger("notifications.ui_notifier")

# Канал pub/sub, который слушает SSE-эндпоинт /stream.
DEFAULT_UI_CHANNEL = "signals"


class UIEventType(str, Enum):
    """
    Типы событий, которые транслируются во фронтенд через SSE.
    """

    SIGNAL = "signal"
    POSITION = "position"
    METRICS = "metrics"
    KILL_SWITCH = "kill_switch"
    GENERIC = "generic"


@dataclass(frozen=True)
class UIEnvelope:
    """
    Универсальный формат сообщения, которое уходит в Redis pub/sub.
    """

    id: str
    event: str
    timestamp: str
    data: Mapping[str, Any]

    def to_json(self) -> str:
        payload = {
            "id": self.id,
            "event": self.event,
            "timestamp": self.timestamp,
            "data": self.data,
        }
        return json.dumps(payload, default=str)


class UINotifier:
    """
    Нотификатор для фронтенда AVI-5 через Redis pub/sub.

    - формирует envelope (id + event + timestamp + payload);
    - публикует его в Redis-канал, который слушает SSE /stream;
    - даёт high-level методы для сигналов, позиций, метрик и kill-switch.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        channel: str = DEFAULT_UI_CHANNEL,
    ) -> None:
        self._redis = redis
        self._channel = channel

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _build_envelope(
        self,
        *,
        event_type: UIEventType,
        data: Mapping[str, Any],
        id_: Optional[str] = None,
    ) -> UIEnvelope:
        if id_ is None:
            id_ = str(uuid4())

        return UIEnvelope(
            id=id_,
            event=event_type.value,
            timestamp=self._now_iso(),
            data=data,
        )

    async def _publish_envelope(self, envelope: UIEnvelope) -> None:
        payload = envelope.to_json()

        try:
            await self._redis.publish(self._channel, payload)
            logger.debug(
                "UI envelope published",
                extra={
                    "channel": self._channel,
                    "event": envelope.event,
                    "id": envelope.id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to publish UI envelope to Redis",
                extra={
                    "channel": self._channel,
                    "event": envelope.event,
                    "id": envelope.id,
                    "error": str(exc),
                },
            )

    @staticmethod
    def _model_to_dict(model: Any) -> Mapping[str, Any]:
        """
        Аккуратно превратить pydantic-модель (v1/v2) или произвольный объект
        в dict **без** использования .dict(), чтобы не ловить deprecation warning.
        """
        # pydantic v2
        if hasattr(model, "model_dump"):
            return model.model_dump()  # type: ignore[no-any-return]

        # pydantic v1 (на всякий случай, если где-то ещё такая зависимость)
        model_dict = getattr(model, "__dict__", None)
        if isinstance(model_dict, dict):
            # выкидываем приватные/служебные поля, чтобы не светить лишнее
            return {k: v for k, v in model_dict.items() if not k.startswith("_")}

        # Фолбэк совсем на чёрный день
        return {}

    # --- public API ---------------------------------------------------------

    async def notify_raw(
        self,
        event_type: UIEventType,
        data: Mapping[str, Any],
        id_: Optional[str] = None,
    ) -> None:
        envelope = self._build_envelope(event_type=event_type, data=data, id_=id_)
        await self._publish_envelope(envelope)

    async def notify_signal(self, signal: Signal) -> None:
        """
        Отправить во фронт событие о новом/обновлённом Signal.
        """
        try:
            payload = self._model_to_dict(signal)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to serialize Signal for UI notification",
                extra={"signal_id": str(getattr(signal, "id", "?")), "error": str(exc)},
            )
            return

        envelope = self._build_envelope(event_type=UIEventType.SIGNAL, data=payload)
        await self._publish_envelope(envelope)

    async def notify_position(self, position: Position) -> None:
        """
        Отправить во фронт событие об изменении позиции.
        """
        try:
            payload = self._model_to_dict(position)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to serialize Position for UI notification",
                extra={"position_id": str(getattr(position, "id", "?")), "error": str(exc)},
            )
            return

        envelope = self._build_envelope(event_type=UIEventType.POSITION, data=payload)
        await self._publish_envelope(envelope)

    async def notify_metrics(self, metrics: Mapping[str, Any]) -> None:
        """
        Отправить агрегированные метрики стратегии во фронт.
        """
        envelope = self._build_envelope(event_type=UIEventType.METRICS, data=metrics)
        await self._publish_envelope(envelope)

    async def notify_kill_switch(self, payload: Mapping[str, Any]) -> None:
        """
        Отправить во фронт событие о срабатывании kill-switch.
        """
        envelope = self._build_envelope(
            event_type=UIEventType.KILL_SWITCH,
            data=payload,
        )
        await self._publish_envelope(envelope)

    async def notify_generic(self, data: Mapping[str, Any]) -> None:
        """
        Универсальное generic-событие для новых типов, пока без отдельного event_type.
        """
        envelope = self._build_envelope(event_type=UIEventType.GENERIC, data=data)
        await self._publish_envelope(envelope)
