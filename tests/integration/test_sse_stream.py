# tests/integration/test_sse_stream.py

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, cast

import pytest
from fastapi import FastAPI, Request
from redis.asyncio import Redis
from starlette.responses import StreamingResponse

from src.api.routes import stream as stream_module


# ======================================================================
# Фейки Redis / pubsub для тестов
# ======================================================================


class FakePubSub:
    """Минимальная заглушка Redis pub/sub для тестов SSE."""

    def __init__(self, messages: List[Dict[str, Any]]) -> None:
        # messages — список словарей формата {"data": <str|bytes>}
        self._messages = messages
        self.subscribed_channels: List[str] = []
        self.closed: bool = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed_channels.append(channel)

    async def get_message(
        self,
        ignore_subscribe_messages: bool = True,
        timeout: float = 1.0,
    ) -> Optional[Dict[str, Any]]:
        # Помечаем параметры как использованные, чтобы IDE не ругалась.
        _ = ignore_subscribe_messages
        _ = timeout

        if self._messages:
            return self._messages.pop(0)
        # небольшая уступка event-loop'у, чтобы не крутиться в tight loop
        await asyncio.sleep(0)
        return None

    async def unsubscribe(self, channel: str) -> None:
        if channel in self.subscribed_channels:
            self.subscribed_channels.remove(channel)

    async def close(self) -> None:
        self.closed = True


class FakeRedis:
    """Fake Redis-клиент, отдающий заранее подготовленный pubsub."""

    def __init__(self, messages: List[Dict[str, Any]]) -> None:
        self._pubsub = FakePubSub(messages)

    def pubsub(self) -> FakePubSub:
        return self._pubsub


class FakeRequest:
    """
    Простейший объект-заглушка вместо FastAPI Request
    для тестирования _sse_event_stream.

    Нас интересует только метод is_disconnected() — всё остальное
    в генераторе SSE не используется.
    """

    def __init__(self) -> None:
        self._disconnected: bool = False

    async def is_disconnected(self) -> bool:
        return self._disconnected

    def disconnect(self) -> None:
        self._disconnected = True


# ======================================================================
# Тесты внутреннего генератора _sse_event_stream
# ======================================================================


@pytest.mark.asyncio
async def test_sse_event_stream_yields_valid_sse_chunk() -> None:
    """
    При приходе валидного envelope в Redis-пабсабе, генератор должен
    отдать корректный SSE-блок с id / event / data.
    """
    envelope = {
        "id": "123",
        "event": "signal",
        "data": {"foo": "bar", "n": 1},
    }
    raw = json.dumps(envelope)

    messages = [
        {"data": raw},
    ]

    redis = FakeRedis(messages)
    request = FakeRequest()

    # noinspection PyProtectedMember
    gen = stream_module._sse_event_stream(
        request=request,  # type: ignore[arg-type]
        redis=redis,      # type: ignore[arg-type]
        channel=stream_module.SSE_CHANNEL_NAME,
        last_event_id=None,
    )

    chunk = await gen.__anext__()
    text = chunk.decode("utf-8").strip()

    # Ожидаем формат:
    # id: 123
    # event: signal
    # data: {...}
    lines = text.split("\n")
    assert lines[0] == "id: 123"
    assert lines[1] == "event: signal"
    assert lines[2].startswith("data: ")

    data_part = lines[2][len("data: ") :]
    parsed = json.loads(data_part)
    assert parsed == envelope["data"]


@pytest.mark.asyncio
async def test_sse_event_stream_skips_invalid_json_and_continues() -> None:
    """
    Если в канале лежит мусор (не JSON), генератор должен его пропустить
    и обработать следующее нормальное сообщение.
    """
    bad_message = {"data": "not-a-json"}
    good_envelope = {
        "id": "42",
        "event": "position",
        "data": {"pos": "opened"},
    }
    good_raw = json.dumps(good_envelope)

    messages = [
        bad_message,
        {"data": good_raw},
    ]

    redis = FakeRedis(messages)
    request = FakeRequest()

    # noinspection PyProtectedMember
    gen = stream_module._sse_event_stream(
        request=request,  # type: ignore[arg-type]
        redis=redis,      # type: ignore[arg-type]
        channel=stream_module.SSE_CHANNEL_NAME,
        last_event_id=None,
    )

    # Первый __anext__ вернёт уже нормальное SSE-событие,
    # т.к. мусорное сообщение будет проигнорировано.
    chunk = await gen.__anext__()
    text = chunk.decode("utf-8").strip()
    lines = text.split("\n")

    assert lines[0] == "id: 42"
    assert lines[1] == "event: position"
    assert lines[2].startswith("data: ")
    data_part = lines[2][len("data: ") :]
    parsed = json.loads(data_part)
    assert parsed == good_envelope["data"]


@pytest.mark.asyncio
async def test_sse_event_stream_stops_on_client_disconnect() -> None:
    """
    Как только request.is_disconnected() начинает возвращать True,
    генератор должен завершиться (StopAsyncIteration).
    """
    envelope = {
        "id": "1",
        "event": "signal",
        "data": {"hello": "world"},
    }
    raw = json.dumps(envelope)
    messages = [{"data": raw}]

    redis = FakeRedis(messages)
    request = FakeRequest()

    # noinspection PyProtectedMember
    gen = stream_module._sse_event_stream(
        request=request,  # type: ignore[arg-type]
        redis=redis,      # type: ignore[arg-type]
        channel=stream_module.SSE_CHANNEL_NAME,
        last_event_id=None,
    )

    # Сначала читаем одно событие
    _ = await gen.__anext__()

    # Затем имитируем разрыв соединения
    request.disconnect()

    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


# ======================================================================
# Тест публичного эндпоинта stream()
# ======================================================================


@pytest.mark.asyncio
async def test_stream_endpoint_returns_streaming_response() -> None:
    """
    Эндпоинт /stream должен возвращать StreamingResponse с
    media_type = text/event-stream.
    """
    app = FastAPI()
    fake_redis = FakeRedis([])
    # на всякий случай, чтобы Request(scope)["app"] существовал
    app.state.redis = fake_redis  # type: ignore[attr-defined]

    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/stream",
        "headers": [],
    }
    request = Request(scope)

    # Явно говорим анализатору типов, что этот объект можно трактовать как Redis.
    redis_typed: Redis = cast(Redis, fake_redis)

    response = await stream_module.stream(
        request=request,
        redis=redis_typed,  # минуем Depends(get_redis), подставляем сами
        last_event_id=None,
    )

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"
