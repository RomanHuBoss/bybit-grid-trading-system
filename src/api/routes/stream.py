# src/api/routes/stream.py
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends, Header, Request
from redis.asyncio import Redis
from starlette.responses import StreamingResponse

from src.core.logging_config import get_logger

__all__ = ["router"]

logger = get_logger(__name__)

router = APIRouter(tags=["stream"])

# Канал pub/sub, куда пишет UINotifier.
# В ui_notifier по умолчанию channel="signals", так что здесь держим тот же инвариант.
SSE_CHANNEL_NAME = "signals"

# Как часто отправлять keep-alive комментарии, если в канале тишина (в секундах).
KEEPALIVE_INTERVAL_SEC = 15.0


# --------------------------------------------------------------------------- #
# DI-хелперы
# --------------------------------------------------------------------------- #


async def get_redis(request: Request) -> Redis:
    """
    Получить Redis из состояния приложения.

    Ожидается, что в фабрике FastAPI-приложения (src/main.py) настроено:

        app.state.redis = Redis(...)

    Если Redis не инициализирован — считаем это ошибкой конфигурации.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise RuntimeError("Redis client is not initialized on application.state")
    if not isinstance(redis, Redis):
        raise RuntimeError("application.state.redis is not a Redis instance")
    return redis


# --------------------------------------------------------------------------- #
# Внутренний генератор SSE-событий
# --------------------------------------------------------------------------- #


async def _sse_event_stream(
    request: Request,
    redis: Redis,
    channel: str = SSE_CHANNEL_NAME,
    last_event_id: str | None = None,
) -> AsyncGenerator[bytes, None]:
    """
    Асинхронный генератор SSE-событий.

    Подписывается на Redis pub/sub-канал и транслирует сообщения в формат SSE:

        id: <envelope.id>
        event: <envelope.event>
        data: <JSON(envelope.data)>

    Сырые сообщения ожидаются в "envelope"-формате, который формирует UINotifier:

        {
            "id": "<uuid>",
            "event": "<тип_события>",
            "timestamp": "<iso8601>",
            "data": {...}
        }
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    if last_event_id:
        # Сейчас мы никак не восстанавливаем пропущенные события
        # (используем pub/sub, а не Redis Streams), но сам факт передачи
        # last_event_id полезен для логов и будущего расширения.
        logger.info(
            "SSE client connected with Last-Event-ID",
            last_event_id=last_event_id,
        )

    try:
        loop = asyncio.get_running_loop()
        last_keepalive = loop.time()

        while True:
            # Клиент отвалился — выходим из цикла.
            if await request.is_disconnected():
                logger.info("SSE client disconnected")
                break

            # Ждём сообщение из Redis с таймаутом.
            message: dict[str, Any] | None = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )

            now = loop.time()

            if message is None:
                # В канале тихо — периодически шлём keep-alive, чтобы
                # не было idle-таймаутов на балансерах / браузере.
                if now - last_keepalive >= KEEPALIVE_INTERVAL_SEC:
                    # Комментарий по SSE-спеке: не отображается в UI,
                    # но держит соединение живым.
                    yield b": keepalive\n\n"
                    last_keepalive = now
                continue

            raw_data = message.get("data")
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode("utf-8")

            try:
                envelope = json.loads(raw_data)
            except Exception:  # noqa: BLE001
                logger.warning("Invalid JSON in SSE Redis channel", raw=raw_data)
                continue

            event_name = envelope.get("event") or "message"
            event_id = envelope.get("id")
            payload = envelope.get("data")

            try:
                payload_str = json.dumps(payload, default=str)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to serialize SSE payload", event=event_name)
                continue

            lines: list[str] = []
            if event_id:
                lines.append(f"id: {event_id}")
            if event_name:
                lines.append(f"event: {event_name}")
            lines.append(f"data: {payload_str}")

            chunk = ("\n".join(lines) + "\n\n").encode("utf-8")
            yield chunk

    except asyncio.CancelledError:
        # Стрим отменили (обычно клиент разорвал соединение / сервер гасится).
        # Логируем и пробрасываем дальше, чтобы корректно завершить StreamingResponse.
        logger.info("SSE stream task was cancelled")
        raise
    finally:
        # Важно корректно отписаться и закрыть pubsub, чтобы не протекали ресурсы.
        try:
            await pubsub.unsubscribe(channel)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Error while unsubscribing from SSE Redis channel",
                channel=channel,
            )
        try:
            await pubsub.close()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Error while closing Redis pubsub in SSE stream",
                channel=channel,
            )


# --------------------------------------------------------------------------- #
# Публичный эндпоинт /stream
# --------------------------------------------------------------------------- #


@router.get("/stream")
async def stream(
    request: Request,
    redis: Redis = Depends(get_redis),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """
    SSE-эндпоинт для фронтенда AVI-5.

    * URL: `GET /stream`
    * Content-Type: `text/event-stream`
    * Авторизация реализуется общим middleware (JWT / RBAC), как и для остальных
      эндпоинтов; сам handler только стримит события.

    Формат отдельных событий соответствует описанию в docs/api.md:

      - `event: signal`   + `data: {...модель Signal...}`
      - `event: position` + `data: {...модель Position...}`
      - дополнительные: `metrics`, `kill_switch` и т.п.

    Сырые сообщения читаются из Redis-канала (см. UINotifier), где они
    уже приведены к универсальному "envelope"-формату:

        {"id": "<uuid>", "event": "signal", "timestamp": "...", "data": {...}}

    Здесь мы:
      * используем `id` как SSE-идентификатор события (для Last-Event-ID);
      * пробрасываем `event` как тип события;
      * сериализуем `data` как data-payload события.
    """
    event_generator = _sse_event_stream(
        request=request,
        redis=redis,
        channel=SSE_CHANNEL_NAME,
        last_event_id=last_event_id,
    )

    return StreamingResponse(
        event_generator,
        media_type="text/event-stream",
    )
