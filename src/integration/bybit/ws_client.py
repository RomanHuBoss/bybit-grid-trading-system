from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import time
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Union

import aiohttp

from src.core.exceptions import WSConnectionError
from src.core.logging_config import get_logger
from src.integration.bybit.rate_limiter import RateLimiterBybit
from src.integration.bybit.rest_client import BybitRESTClient

logger = get_logger(__name__)


class WSTimeoutError(WSConnectionError):
    """Таймаут при установлении WebSocket-соединения с Bybit."""
    pass


class WSConnectionClosed(WSConnectionError):
    """WebSocket-соединение было закрыто сервером или локально."""
    pass


class BybitWSClient:
    """
    Низкоуровневый WebSocket-клиент Bybit.

    Экземпляр управляет одним WS-соединением (публичным или приватным),
    поддерживает:

      * установление/закрытие соединения;
      * подписки на топики;
      * чтение сообщений через async-генератор;
      * авто-reconnect с экспоненциальным backoff;
      * gap-detector по sequence с fallback на REST snapshot.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        rate_limiter: RateLimiterBybit,
        rest_client: BybitRESTClient,
        is_private: bool = False,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        recv_window_ms: int = 5000,
        connect_timeout: float = 5.0,
        max_reconnect_attempts: int = 5,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        """
        :param ws_url: Полный URL WebSocket-эндпоинта Bybit.
        :param rate_limiter: Rate limiter для контроля частоты подписок.
        :param rest_client: REST-клиент для snapshot-fallback'а.
        :param is_private: Флаг приватного канала (требуется auth).
        :param api_key: API-ключ Bybit (обязателен для приватных каналов).
        :param api_secret: Секретный ключ Bybit (обязателен для приватных каналов).
        :param recv_window_ms: recv_window для подписи приватных сообщений.
        :param connect_timeout: Таймаут установления WS-соединения.
        :param max_reconnect_attempts: Максимум попыток reconnect'а перед ошибкой.
        :param session: Опциональный aiohttp.ClientSession (если не передан,
                        клиент создаёт свой и управляет его жизненным циклом).
        """
        self._ws_url = ws_url
        self._rate_limiter = rate_limiter
        self._rest_client = rest_client
        self._is_private = is_private
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8") if api_secret is not None else None
        self._recv_window_ms = recv_window_ms
        self._connect_timeout = connect_timeout
        self._max_reconnect_attempts = max_reconnect_attempts

        self._session: Optional[aiohttp.ClientSession] = session
        self._owns_session: bool = session is None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

        # Зарегистрированные подписки: topic -> True
        self._subscriptions: Dict[str, bool] = {}
        # Последние sequence по каналам для gap-detector'а
        self._last_sequence: Dict[str, int] = {}

    @property
    def is_connected(self) -> bool:
        """Возвращает True, если WebSocket-соединение активно."""
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        """
        Установить WebSocket-соединение (если ещё не установлено).

        Таймаут подключения — connect_timeout. При превышении выбрасывается
        WSTimeoutError, при любых сетевых проблемах — WSConnectionError.
        """
        if self.is_connected:
            return

        if self._session is None:
            self._session = aiohttp.ClientSession()

        try:
            logger.info(
                "Connecting to Bybit WebSocket",
                ws_url=self._ws_url,
                is_private=self._is_private,
            )
            self._ws = await asyncio.wait_for(
                self._session.ws_connect(self._ws_url, heartbeat=30),
                timeout=self._connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            logger.error("WebSocket connection timeout", ws_url=self._ws_url)
            raise WSTimeoutError(
                "Timed out while connecting to Bybit WebSocket",
                details={"ws_url": self._ws_url, "timeout": self._connect_timeout},
            ) from exc
        except aiohttp.ClientError as exc:
            logger.error(
                "Failed to establish WebSocket connection",
                ws_url=self._ws_url,
                exc_info=True,
            )
            raise WSConnectionError(
                "Failed to establish WebSocket connection to Bybit",
                details={"ws_url": self._ws_url},
            ) from exc

        # Приватные каналы требуют аутентификации.
        if self._is_private:
            await self._authenticate()

        logger.info(
            "Bybit WebSocket connected",
            ws_url=self._ws_url,
            is_private=self._is_private,
        )

    async def _authenticate(self) -> None:
        """
        Выполнить аутентификацию на приватном WS-канале.

        Алгоритм подписи приближен к REST v5:
            sign = HMAC_SHA256(secret, timestamp + api_key + recv_window)
        """
        assert self._ws is not None
        assert self._api_key is not None and self._api_secret is not None

        timestamp_ms = int(time.time() * 1000)
        recv_window = str(self._recv_window_ms)

        sign_str = f"{timestamp_ms}{self._api_key}{recv_window}"
        signature = hmac.new(
            self._api_secret,
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        auth_msg = {
            "op": "auth",
            "args": [self._api_key, timestamp_ms, recv_window, signature],
        }

        await self._ws.send_json(auth_msg)
        resp_msg = await self._ws.receive()

        if resp_msg.type != aiohttp.WSMsgType.TEXT:
            raise WSConnectionError(
                "Unexpected auth response type from Bybit WS",
                details={"type": str(resp_msg.type)},
            )

        try:
            payload = json.loads(resp_msg.data)
        except json.JSONDecodeError as exc:
            raise WSConnectionError(
                "Failed to decode auth response from Bybit WS",
                details={"raw": resp_msg.data},
            ) from exc

        if not isinstance(payload, dict) or not payload.get("success", False):
            raise WSConnectionError(
                "Bybit WS authentication failed",
                details={"response": payload},
            )

        logger.info("Bybit WS private authentication successful", ws_url=self._ws_url)

    async def subscribe(
        self,
        topics: Union[str, Iterable[str]],
    ) -> None:
        """
        Подписаться на один или несколько WS-топиков.

        Перед каждой подпиской соблюдаются локальные лимиты `RateLimiterBybit`
        по бакету `ws_sub`.
        """
        await self.connect()

        if isinstance(topics, str):
            topic_list: List[str] = [topics]
        else:
            topic_list = [t for t in topics]

        if not topic_list:
            return

        # На всякий случай проверим, что соединение реально живо.
        if self._ws is None or self._ws.closed:
            raise WSConnectionError(
                "WebSocket is not connected",
                details={"ws_url": self._ws_url},
            )

        # Соблюдаем лимиты подписок.
        for _ in topic_list:
            await self._rate_limiter.consume_ws_subscription()

        subscribe_msg = {
            "op": "subscribe",
            "args": topic_list,
        }

        await self._ws.send_json(subscribe_msg)

        for topic in topic_list:
            self._subscriptions[topic] = True

        logger.info(
            "Subscribed to Bybit WS topics",
            ws_url=self._ws_url,
            topics=topic_list,
            is_private=self._is_private,
        )

    async def subscribe_user_data(self) -> None:
        """
        Специализированная подписка на приватный поток `user.order`.

        Используется исполнителем `FillTracker`.
        """
        if not self._is_private:
            raise ValueError("subscribe_user_data is only valid for private WS connections")
        await self.subscribe("user.order")

    async def listen(self) -> AsyncGenerator[tuple[str, Dict[str, Any], int], None]:
        """
        Основной цикл чтения сообщений из WS.

        Yield'ит кортежи (channel, data, sequence). При закрытии соединения
        выбрасывает WSConnectionClosed.
        """
        await self.connect()

        if self._ws is None:
            raise WSConnectionError(
                "WebSocket is not connected",
                details={"ws_url": self._ws_url},
            )

        while True:
            try:
                msg = await self._ws.receive()
            except asyncio.CancelledError:
                # Позволяем корректно отменять вызывающей стороне.
                raise
            except Exception:
                logger.warning(
                    "Error while receiving WS message; will attempt reconnect",
                    ws_url=self._ws_url,
                    exc_info=True,
                )
                await self.handle_reconnect()
                continue

            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    logger.warning("Received non-JSON WS message", raw=msg.data)
                    continue

                # Игнорируем служебные ping/pong и ответы на subscribe/auth.
                if self._is_control_message(payload):
                    continue

                try:
                    channel, sequence, data = self._normalize_payload(payload)
                except KeyError:
                    logger.warning("WS payload missing required fields", payload=payload)
                    continue

                yield channel, data, sequence

            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                logger.warning(
                    "Bybit WS connection closed",
                    ws_url=self._ws_url,
                    msg_type=str(msg.type),
                )
                raise WSConnectionClosed(
                    "Bybit WebSocket connection closed",
                    details={"ws_url": self._ws_url, "msg_type": str(msg.type)},
                )
            elif msg.type == aiohttp.WSMsgType.PING:
                if self._ws is not None:
                    await self._ws.pong()
            elif msg.type == aiohttp.WSMsgType.PONG:
                # Ничего делать не нужно.
                continue
            else:
                # Binary, close и прочие типы нам не интересны.
                continue

    async def handle_reconnect(self) -> None:
        """
        Выполнить попытки переподключения с экспоненциальным backoff'ом.

        Шаги:
        1. Закрыть текущее соединение (если ещё не закрыто).
        2. Повторять connect() с backoff'ом 200ms → 3s (+ jitter).
        3. После успешного коннекта восстановить подписки.
        4. При превышении max_reconnect_attempts выбросить WSConnectionError.
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, self._max_reconnect_attempts + 1):
            try:
                await self._reconnect_once()
                logger.info(
                    "Bybit WS reconnected successfully",
                    ws_url=self._ws_url,
                    attempt=attempt,
                )
                return
            except WSConnectionError as exc:
                last_error = exc
                # Экспоненциальный backoff 0.2, 0.4, 0.8, ..., до 3 секунд + jitter.
                base_delay = min(0.2 * (2 ** (attempt - 1)), 3.0)
                jitter = random.uniform(0.9, 1.1)
                sleep_for = base_delay * jitter
                logger.warning(
                    "Bybit WS reconnect failed, will retry",
                    ws_url=self._ws_url,
                    attempt=attempt,
                    sleep_for=sleep_for,
                )
                await asyncio.sleep(sleep_for)

        raise WSConnectionError(
            "Exceeded maximum Bybit WS reconnect attempts",
            details={"ws_url": self._ws_url, "max_attempts": self._max_reconnect_attempts},
        ) from last_error

    async def _reconnect_once(self) -> None:
        """Одна попытка переподключения с восстановлением подписок."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

        self._ws = None
        await self.connect()

        if self._subscriptions:
            await self.subscribe(list(self._subscriptions.keys()))

    def _is_control_message(self, payload: Dict[str, Any]) -> bool:
        """
        Определить, является ли сообщение служебным:
        ping/pong, подтверждения подписок, auth и т.п.
        """
        # Bybit обычно шлёт поле "op" для служебных сообщений.
        op = payload.get("op")
        if op in {"ping", "pong", "subscribe", "auth"}:
            return True

        # Ответы на подписку часто содержат "success": true и "request".
        if "success" in payload and "request" in payload:
            return True

        return False

    def _normalize_payload(self, payload: Dict[str, Any]) -> tuple[str, int, Dict[str, Any]]:
        """
        Нормализовать сырое WS-сообщение в (channel, sequence, data).

        По умолчанию:
        - channel берётся из поля "topic" или "channel";
        - sequence — из поля "sequence" или "ts" (timestamp) как fallback;
        - data — содержимое поля "data" или весь payload.
        """
        channel = payload.get("topic") or payload.get("channel")
        if channel is None:
            raise KeyError("Missing 'topic'/'channel' in WS payload")

        raw_seq = payload.get("sequence")
        if raw_seq is None:
            raw_seq = payload.get("ts")
        if raw_seq is None:
            raise KeyError("Missing 'sequence'/'ts' in WS payload")

        try:
            sequence = int(raw_seq)
        except (TypeError, ValueError):
            raise KeyError("WS payload sequence field is not an integer")

        data_field = payload.get("data")
        if data_field is None:
            data: Dict[str, Any] = payload
        elif isinstance(data_field, dict):
            data = data_field
        else:
            # Bybit часто присылает список; для унификации оборачиваем в dict.
            data = {"data": data_field}

        # Gap detection: если sequence разрывается — триггерим snapshot через REST.
        last_seq = self._last_sequence.get(channel)
        if last_seq is not None and sequence > last_seq + 1:
            logger.warning(
                "WS sequence gap detected, scheduling snapshot resync",
                channel=channel,
                last_seq=last_seq,
                new_seq=sequence,
            )
            # Не блокируем основной поток обработки сообщений.
            asyncio.create_task(self.resync_snapshot(channel))

        self._last_sequence[channel] = sequence

        # Сделаем sequence и channel частью payload, чтобы downstream-код мог им пользоваться.
        data.setdefault("sequence", sequence)
        data.setdefault("channel", channel)

        return channel, sequence, data

    async def resync_snapshot(self, channel: str) -> Dict[str, Any]:
        """
        Получить snapshot по REST для указанного канала.

        Ожидаемый формат channel:
            - kline.<interval>.<symbol>
            - orderbook.<depth>.<symbol>

        Возвращает словарь с сырым REST-ответом Bybit.
        """
        parts = channel.split(".")
        if not parts:
            raise ValueError("Invalid channel format for snapshot")

        kind = parts[0]

        if kind == "kline":
            if len(parts) < 3:
                raise ValueError(
                    "Invalid kline channel format, expected 'kline.<interval>.<symbol>'",
                )
            interval = parts[1]
            symbol = parts[2]
            params: Dict[str, Any] = {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": 200,
            }
            path = "v5/market/kline"
        elif kind == "orderbook":
            if len(parts) < 3:
                raise ValueError(
                    "Invalid orderbook channel format, expected 'orderbook.<depth>.<symbol>'",
                )
            depth = parts[1]
            symbol = parts[2]
            params = {
                "category": "linear",
                "symbol": symbol,
                "limit": depth,
            }
            path = "v5/market/orderbook"
        else:
            raise ValueError(f"Unsupported channel type for snapshot: {channel}")

        logger.info(
            "Requesting REST snapshot for WS channel",
            channel=channel,
            path=path,
            params=params,
        )

        # read_weight ~2, т.к. snapshot тяжелее обычного запроса.
        snapshot = await self._rest_client.request(
            method="GET",
            path=path,
            params=params,
            read_weight=2,
        )

        return dict(snapshot)

    async def close(self) -> None:
        """
        Корректно закрыть WebSocket и HTTP-сессию (если клиент ей владеет).

        Используется при штатном останове приложения, чтобы не оставлять
        незакрытых соединений.
        """
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
