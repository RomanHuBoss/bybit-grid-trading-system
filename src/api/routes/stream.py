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
SSE_CHANNEL_NAME = "signals"

# Как часто отправлять keep-alive комментарии (в секундах).
KEEPALIVE_INTERVAL_SEC = 15.0


async def get_redis(request: Request) -> Redis:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise RuntimeError("Redis client is not initialized on application.state")
    if not isinstance(redis, Redis):
        raise RuntimeError("application.state.redis is not a Redis instance")
    return redis


async def _sse_event_stream(
    request: Request,
    redis: Redis,
    channel: str = SSE_CHANNEL_NAME,
    last_event_id: str | None = None,
) -> AsyncGenerator[bytes, None]:
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    if last_event_id:
        logger.info(
            "SSE client connected with Last-Event-ID",
            last_event_id=last_event_id,
        )

    try:
        loop = asyncio.get_running_loop()
        last_keepalive = loop.time()

        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected")
                break

            message: dict[str, Any] | None = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )

            now = loop.time()

            if message is None:
                if now - last_keepalive >= KEEPALIVE_INTERVAL_SEC:
                    yield b": keepalive\n\n"
                    last_keepalive = now
                continue

            raw_data = message.get("data")

            # ----- FIX: типобезопасная нормализация -----
            if isinstance(raw_data, bytes):
                raw_str = raw_data.decode("utf-8")
            elif isinstance(raw_data, str):
                raw_str = raw_data
            else:
                logger.warning("Invalid SSE message type", raw=raw_data)
                continue
            # Теперь raw_str: str — подходит под json.loads

            try:
                envelope = json.loads(raw_str)
            except Exception:  # noqa: BLE001
                logger.warning("Invalid JSON in SSE Redis channel", raw=raw_str)
                continue
            # ----- END FIX -----

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
        logger.info("SSE stream task was cancelled")
        raise
    finally:
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


@router.get("/stream")
async def stream(
    request: Request,
    redis: Redis = Depends(get_redis),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
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
