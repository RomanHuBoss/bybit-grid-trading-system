import asyncio
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from aiohttp import WSMsgType

from src.integration.bybit.rate_limiter import RateLimiterBybit
from src.integration.bybit.rest_client import BybitRESTClient
from src.integration.bybit.ws_client import (
    BybitWSClient,
    WSConnectionClosed,
)


class _DummyWS:
    """Простейшая заглушка WebSocket-соединения для тестов."""

    def __init__(self, messages: Optional[List[Any]] = None) -> None:
        self._messages = list(messages or [])
        self.closed: bool = False
        self.sent_json: List[Dict[str, Any]] = []

    async def receive(self) -> Any:
        if self._messages:
            return self._messages.pop(0)
        # Если сообщения закончились — эмулируем закрытие соединения
        return type(
            "DummyMsg",
            (),
            {"type": WSMsgType.CLOSED, "data": ""},
        )()

    async def send_json(self, payload: Dict[str, Any]) -> None:
        self.sent_json.append(payload)

    async def pong(self) -> None:
        # Ничего не делаем; важно лишь, что метод существует
        return None

    async def close(self) -> None:
        self.closed = True


class _DummyMsg:
    """Заглушка сообщения WS для listen()."""

    def __init__(self, msg_type: WSMsgType, data: Any) -> None:
        self.type = msg_type
        self.data = data


@pytest.mark.asyncio
async def test_subscribe_single_topic_uses_rate_limiter_and_sends_json() -> None:
    rate_limiter = AsyncMock(spec=RateLimiterBybit)
    rest_client = AsyncMock(spec=BybitRESTClient)

    client = BybitWSClient(
        ws_url="wss://test.example.com/public",
        rate_limiter=rate_limiter,
        rest_client=rest_client,
        is_private=False,
    )

    ws = _DummyWS()
    # Принудительно считаем, что соединение уже установлено
    client._ws = ws  # type: ignore[attr-defined]
    # connect() при is_connected сразу вернёт управление
    client._connect_timeout = 0.1  # type: ignore[attr-defined]  # несущественно, но пусть будет

    topic = "kline.1.BTCUSDT"

    await client.subscribe(topic)

    # Проверяем, что rate limiter дернулся ровно один раз
    assert rate_limiter.consume_ws_subscription.await_count == 1

    # Проверяем, что ушел корректный subscribe-пакет
    assert ws.sent_json == [
        {
            "op": "subscribe",
            "args": [topic],
        }
    ]

    # И что подписка зарегистрирована внутри клиента
    assert client._subscriptions.get(topic) is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_subscribe_multiple_topics_calls_rate_limiter_for_each() -> None:
    rate_limiter = AsyncMock(spec=RateLimiterBybit)
    rest_client = AsyncMock(spec=BybitRESTClient)

    client = BybitWSClient(
        ws_url="wss://test.example.com/public",
        rate_limiter=rate_limiter,
        rest_client=rest_client,
        is_private=False,
    )

    ws = _DummyWS()
    client._ws = ws  # type: ignore[attr-defined]

    topics = ["kline.1.BTCUSDT", "orderbook.50.ETHUSDT"]

    await client.subscribe(topics)

    # Должно быть по одному consume_ws_subscription на каждый топик
    assert rate_limiter.consume_ws_subscription.await_count == len(topics)

    # В один subscribe-запрос уходит сразу список топиков
    assert ws.sent_json == [
        {
            "op": "subscribe",
            "args": topics,
        }
    ]
    for t in topics:
        assert client._subscriptions.get(t) is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_listen_ignores_control_messages_and_yields_normalized_data(monkeypatch: pytest.MonkeyPatch) -> None:
    rate_limiter = AsyncMock(spec=RateLimiterBybit)
    rest_client = AsyncMock(spec=BybitRESTClient)

    client = BybitWSClient(
        ws_url="wss://test.example.com/public",
        rate_limiter=rate_limiter,
        rest_client=rest_client,
        is_private=False,
    )

    # Подменяем connect, чтобы не открывать реальный WS
    client.connect = AsyncMock()  # type: ignore[assignment]

    control_msg = _DummyMsg(
        WSMsgType.TEXT,
        json.dumps({"op": "pong"}),
    )

    market_msg = _DummyMsg(
        WSMsgType.TEXT,
        json.dumps(
            {
                "topic": "kline.1.BTCUSDT",
                "sequence": 42,
                "data": {"foo": "bar"},
            }
        ),
    )

    ws = _DummyWS(messages=[control_msg, market_msg])
    client._ws = ws  # type: ignore[attr-defined]

    async def run_listener() -> None:
        gen = client.listen()
        channel, data, sequence = await gen.__anext__()
        assert channel == "kline.1.BTCUSDT"
        assert sequence == 42
        assert data["foo"] == "bar"
        # _normalize_payload добавляет sequence и channel внутрь data
        assert data["sequence"] == 42
        assert data["channel"] == "kline.1.BTCUSDT"

        # Следующее сообщение: CLOSED → должен быть WSConnectionClosed
        with pytest.raises(WSConnectionClosed):
            await gen.__anext__()

    await run_listener()


@pytest.mark.asyncio
async def test_normalize_payload_gap_uses_resync_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.integration.bybit import ws_client as ws_module

    rate_limiter = AsyncMock(spec=RateLimiterBybit)
    rest_client = AsyncMock(spec=BybitRESTClient)

    client = BybitWSClient(
        ws_url="wss://test.example.com/public",
        rate_limiter=rate_limiter,
        rest_client=rest_client,
        is_private=False,
    )

    # Первый payload без разрыва
    payload1 = {
        "topic": "kline.1.BTCUSDT",
        "sequence": 1,
        "data": {"value": 100},
    }
    chan1, seq1, data1 = client._normalize_payload(payload1)  # type: ignore[attr-defined]
    assert chan1 == "kline.1.BTCUSDT"
    assert seq1 == 1
    assert data1["value"] == 100
    assert data1["sequence"] == 1

    # Подменяем asyncio.create_task внутри модуля ws_client
    tasks: List[asyncio.Future] = []

    def fake_create_task(coro: asyncio.Future) -> asyncio.Future:
        tasks.append(coro)
        return coro

    monkeypatch.setattr(ws_module.asyncio, "create_task", fake_create_task)

    # Второй payload с разрывом sequence: 1 -> 3
    payload2 = {
        "topic": "kline.1.BTCUSDT",
        "sequence": 3,
        "data": [1, 2, 3],
    }
    chan2, seq2, data2 = client._normalize_payload(payload2)  # type: ignore[attr-defined]

    # Data должен быть обёрнут в dict и дополнен служебными полями
    assert chan2 == "kline.1.BTCUSDT"
    assert seq2 == 3
    assert isinstance(data2, dict)
    assert data2["data"] == [1, 2, 3]
    assert data2["sequence"] == 3
    assert data2["channel"] == "kline.1.BTCUSDT"

    # При разрыве sequence должен был запланироваться resync_snapshot
    assert tasks, "resync_snapshot should have been scheduled via asyncio.create_task"
    coro = tasks[0]
    assert asyncio.iscoroutine(coro)
    # Проверяем, что это корутина resync_snapshot с нужным каналом
    assert coro.cr_code.co_name == "resync_snapshot"  # type: ignore[attr-defined]
    assert coro.cr_frame is not None  # type: ignore[attr-defined]
    assert coro.cr_frame.f_locals.get("self") is client  # type: ignore[attr-defined]
    assert coro.cr_frame.f_locals.get("channel") == "kline.1.BTCUSDT"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_resync_snapshot_kline_calls_rest_with_expected_params() -> None:
    rate_limiter = AsyncMock(spec=RateLimiterBybit)
    rest_client = AsyncMock(spec=BybitRESTClient)
    rest_client.request = AsyncMock(return_value={"foo": "bar"})  # type: ignore[assignment]

    client = BybitWSClient(
        ws_url="wss://test.example.com/public",
        rate_limiter=rate_limiter,
        rest_client=rest_client,
        is_private=False,
    )

    result = await client.resync_snapshot("kline.5.BTCUSDT")

    rest_client.request.assert_awaited_once_with(  # type: ignore[attr-defined]
        method="GET",
        path="v5/market/kline",
        params={
            "category": "linear",
            "symbol": "BTCUSDT",
            "interval": "5",
            "limit": 200,
        },
        read_weight=2,
    )
    assert result == {"foo": "bar"}


@pytest.mark.asyncio
async def test_resync_snapshot_orderbook_calls_rest_with_expected_params() -> None:
    rate_limiter = AsyncMock(spec=RateLimiterBybit)
    rest_client = AsyncMock(spec=BybitRESTClient)
    rest_client.request = AsyncMock(return_value={"ob": []})  # type: ignore[assignment]

    client = BybitWSClient(
        ws_url="wss://test.example.com/public",
        rate_limiter=rate_limiter,
        rest_client=rest_client,
        is_private=False,
    )

    result = await client.resync_snapshot("orderbook.50.ETHUSDT")

    rest_client.request.assert_awaited_once_with(  # type: ignore[attr-defined]
        method="GET",
        path="v5/market/orderbook",
        params={
            "category": "linear",
            "symbol": "ETHUSDT",
            "limit": "50",  # depth сохраняется как строка
        },
        read_weight=2,
    )
    assert result == {"ob": []}


@pytest.mark.asyncio
async def test_resync_snapshot_invalid_channels_raise_value_error() -> None:
    rate_limiter = AsyncMock(spec=RateLimiterBybit)
    rest_client = AsyncMock(spec=BybitRESTClient)

    client = BybitWSClient(
        ws_url="wss://test.example.com/public",
        rate_limiter=rate_limiter,
        rest_client=rest_client,
        is_private=False,
    )

    with pytest.raises(ValueError):
        await client.resync_snapshot("")  # некорректный формат

    with pytest.raises(ValueError):
        await client.resync_snapshot("kline.5")  # не хватает symbol

    with pytest.raises(ValueError):
        await client.resync_snapshot("orderbook.50")  # не хватает symbol

    with pytest.raises(ValueError):
        await client.resync_snapshot("trades.BTCUSDT")  # неподдерживаемый тип канала


@pytest.mark.asyncio
async def test_handle_reconnect_raises_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.integration.bybit import ws_client as ws_module

    rate_limiter = AsyncMock(spec=RateLimiterBybit)
    rest_client = AsyncMock(spec=BybitRESTClient)

    client = BybitWSClient(
        ws_url="wss://test.example.com/public",
        rate_limiter=rate_limiter,
        rest_client=rest_client,
        is_private=False,
        max_reconnect_attempts=3,
    )

    # _reconnect_once всегда падает
    async def failing_reconnect_once() -> None:
        raise WSConnectionClosed("fail")

    monkeypatch.setattr(client, "_reconnect_once", failing_reconnect_once)  # type: ignore[arg-type]

    # sleep заменяем на no-op, чтобы тест не тормозил
    monkeypatch.setattr(ws_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(Exception) as exc_info:
        await client.handle_reconnect()

    # Сообщение должно соответствовать финальному исключению из handle_reconnect
    assert "Exceeded maximum Bybit WS reconnect attempts" in str(exc_info.value)


@pytest.mark.asyncio
async def test_is_control_message_variants() -> None:
    rate_limiter = AsyncMock(spec=RateLimiterBybit)
    rest_client = AsyncMock(spec=BybitRESTClient)

    client = BybitWSClient(
        ws_url="wss://test.example.com/public",
        rate_limiter=rate_limiter,
        rest_client=rest_client,
        is_private=False,
    )

    # ping/pong/subscribe/auth
    assert client._is_control_message({"op": "ping"})  # type: ignore[attr-defined]
    assert client._is_control_message({"op": "pong"})  # type: ignore[attr-defined]
    assert client._is_control_message({"op": "subscribe"})  # type: ignore[attr-defined]
    assert client._is_control_message({"op": "auth"})  # type: ignore[attr-defined]

    # success + request
    assert client._is_control_message({"success": True, "request": {"op": "subscribe"}})  # type: ignore[attr-defined]

    # Обычное рыночное сообщение не считается служебным
    assert not client._is_control_message({"topic": "kline.1.BTCUSDT", "data": {}})  # type: ignore[attr-defined]
