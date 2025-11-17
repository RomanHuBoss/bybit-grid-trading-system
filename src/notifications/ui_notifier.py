from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from src.core.models import Signal


try:
    # Предпочтительно использовать redis.asyncio.Redis, если библиотека доступна.
    from redis.asyncio import Redis  # type: ignore[import]
except Exception:  # pragma: no cover - тип важен только для подсказок
    Redis = Any  # type: ignore[assignment]


class UINotifier:
    """
    Асинхронный паблишер событий для UI.

    Отвечает за публикацию доменных событий (сигналы, BE-события, метрики,
    состояние kill-switch) в Redis pub/sub-канал, откуда их забирает
    SSE-слой и стримит в браузер.

    Формат сообщения в канале (JSON):

    {
        "id": "<uuid>",              # используется SSE-слоем как Last-Event-ID
        "event": "<тип_события>",    # "signal" / "be.<...>" / "metrics" / "kill_switch"
        "timestamp": "<iso8601>",    # UTC-время генерации события
        "data": {...}                # доменные данные
    }
    """

    def __init__(self, redis: Redis, channel: str = "signals") -> None:
        """
        :param redis: Экземпляр Redis-клиента (redis.asyncio.Redis или совместимый).
        :param channel: Имя pub/sub-канала для realtime-событий UI.
                        Обычно соответствует ui.sse_channel из конфигурации.
        """
        if not channel:
            raise ValueError("channel must be non-empty")

        self._redis: Redis = redis
        self._channel: str = channel

    # --------------------------------------------------------------------- #
    # Публичный API публикации событий
    # --------------------------------------------------------------------- #

    async def publish_signal(self, signal: Signal) -> None:
        """
        Публикует событие о новом торговом сигнале.

        :param signal: Доменная модель сигнала, сформированная AVI-5.
        """
        payload = self._to_payload(signal)
        await self._publish("signal", payload)

    async def publish_be_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """
        Публикует backend-событие для UI.

        :param event_type: Логический тип события (например, "order_placed", "order_filled").
                           Используется как суффикс в имени события: "be.<event_type>".
        :param payload: Произвольные данные события, сериализуемые в JSON.
        """
        if not event_type:
            raise ValueError("event_type must be non-empty")

        event_name = f"be.{event_type}"
        await self._publish(event_name, dict(payload))

    async def publish_metrics(self, metrics: Dict[str, Any]) -> None:
        """
        Публикует агрегированные метрики стратегии/инфраструктуры.

        :param metrics: Словарь с метриками (например, win_rate, profit_factor и т.п.).
        """
        await self._publish("metrics", dict(metrics))

    async def publish_kill_switch(self, active: bool, reason: Optional[str] = None) -> None:
        """
        Публикует изменение состояния kill-switch.

        :param active: Новый статус kill-switch (True — включен, новые позиции не открываем).
        :param reason: Текстовое пояснение причины изменения статуса (опционально).
        """
        data: Dict[str, Any] = {
            "active": active,
            "reason": reason,
        }
        await self._publish("kill_switch", data)

    # --------------------------------------------------------------------- #
    # Внутренние помощники
    # --------------------------------------------------------------------- #

    async def _publish(self, event: str, data: Dict[str, Any]) -> None:
        """
        Сериализует и публикует событие в Redis-канал.
        """
        message = self._serialize_event(event, data)
        # publish возвращает количество подписчиков; для UI-нотификатора
        # это не критично, поэтому результат можно игнорировать.
        await self._redis.publish(self._channel, message)

    @staticmethod
    def _to_payload(obj: Any) -> Dict[str, Any]:
        """
        Приводит произвольный объект к словарю для вставки в поле `data`.

        Поддерживаются:
        - pydantic-модели (BaseModel);
        - dataclass-объекты;
        - уже готовые dict'ы;
        - всё остальное — через vars()/asdict()-подобную семантику.
        """
        # Pydantic BaseModel
        if hasattr(obj, "dict") and callable(getattr(obj, "dict")):
            return obj.dict()  # type: ignore[return-value]

        # dataclasses
        if is_dataclass(obj):
            return asdict(obj)

        # dict как есть
        if isinstance(obj, dict):
            return obj

        # Fallback — пытаемся интерпретировать как объект с __dict__
        if hasattr(obj, "__dict__"):
            return dict(vars(obj))

        # Совсем простой случай — оборачиваем как значение
        return {"value": obj}

    @staticmethod
    def _serialize_event(event: str, data: Dict[str, Any]) -> str:
        """
        Собирает финальный JSON, который будет опубликован в Redis.

        :param event: Имя события (signal / be.* / metrics / kill_switch).
        :param data: Доменные данные события.
        :return: JSON-строка.
        """
        event_id = uuid4().hex
        envelope = {
            "id": event_id,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        # default=str позволяет сериализовать Decimal, datetime и прочие типы,
        # которые pydantic мог оставить как есть.
        return json.dumps(envelope, default=str)
