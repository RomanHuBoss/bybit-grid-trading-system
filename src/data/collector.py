from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core.logging_config import get_logger
from src.integration.bybit.ws_client import BybitWSClient

logger = get_logger(__name__)


def _serialize_stream_payload(data: Dict[str, Any]) -> Dict[str, str]:
    """
    Привести словарь данных к формату, совместимому с Redis Streams.

    Redis Streams принимает только строки/числа/байты.
    Поэтому каждый value приводится к строке.

    Это решает проблему mypy:
        dict[str, Any] → dict[str, str]
    """
    return {key: str(value) for key, value in data.items()}


class DataCollector:
    """
    Сборщик сырых данных с Bybit WS и публикация в Redis Streams.

    Экземпляр управляет одним WS-клиентом и одной группой потоков Redis
    с префиксом `ws_raw:{stream}`.
    """

    def __init__(
        self,
        *,
        ws_client: BybitWSClient,
        redis: Redis,
        stream_prefix: str = "ws_raw",
    ) -> None:
        self._ws_client = ws_client
        self._redis = redis
        self._stream_prefix = stream_prefix.rstrip(":")

    async def subscribe_klines(
        self,
        *,
        interval: str,
        symbols: Iterable[str],
    ) -> None:
        symbols_list: List[str] = [s for s in symbols]
        if not symbols_list:
            return

        topics = [f"kline.{interval}.{symbol}" for symbol in symbols_list]

        logger.info(
            "Subscribing to kline topics",
            interval=interval,
            symbols=symbols_list,
            topics=topics,
        )
        await self._ws_client.subscribe(topics)

    async def subscribe_orderbook(
        self,
        *,
        depth: int,
        symbols: Iterable[str],
    ) -> None:
        if depth < 1 or depth > 50:
            raise ValueError("depth must be in range [1, 50]")

        symbols_list: List[str] = [s for s in symbols]
        if not symbols_list:
            return

        topics = [f"orderbook.{depth}.{symbol}" for symbol in symbols_list]

        logger.info(
            "Subscribing to orderbook topics",
            depth=depth,
            symbols=symbols_list,
            topics=topics,
        )
        await self._ws_client.subscribe(topics)

    async def run(self) -> None:
        logger.info("Starting DataCollector run loop")

        async for channel, data, sequence in self._ws_client.listen():
            try:
                is_new = await self.deduplicate_message(channel, sequence=sequence)
            except Exception:
                logger.warning(
                    "Failed to deduplicate WS message, publishing anyway",
                    channel=channel,
                    sequence=sequence,
                )
                is_new = True

            if not is_new:
                continue

            stream = self._channel_to_stream(channel)
            try:
                await self.publish_to_stream(stream=stream, data=data)
            except RedisError:
                logger.error(
                    "Failed to publish WS message to Redis stream",
                    channel=channel,
                    stream=stream,
                    exc_info=True,
                )
                continue

    async def publish_to_stream(
        self,
        *,
        stream: str,
        data: Dict[str, Any],
    ) -> str:
        full_stream = f"{self._stream_prefix}:{stream}"

        # --- FIX: сериализация в строгий формат, валидный для Redis Streams ---
        serialized = _serialize_stream_payload(data)

        msg_id = await self._redis.xadd(full_stream, serialized)

        logger.debug(
            "Published WS message to Redis stream",
            stream=full_stream,
            msg_id=msg_id,
        )
        return msg_id

    async def deduplicate_message(
        self,
        channel: str,
        *,
        sequence: int,
        ttl_seconds: Optional[int] = 3600,
    ) -> bool:
        key = f"last_seq:{channel}"
        last_seq_raw = await self._redis.get(key)
        last_seq: Optional[int] = None

        if last_seq_raw is not None:
            try:
                last_seq = int(last_seq_raw)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid last_seq value in Redis, resetting",
                    channel=channel,
                    last_seq_raw=last_seq_raw,
                )
                last_seq = None

        if last_seq is not None and sequence <= last_seq:
            logger.debug(
                "Duplicate or out-of-order WS message skipped",
                channel=channel,
                sequence=sequence,
                last_seq=last_seq,
            )
            return False

        await self._redis.set(key, str(sequence))
        if ttl_seconds is not None:
            await self._redis.expire(key, ttl_seconds)

        return True

    def _channel_to_stream(self, channel: str) -> str:
        parts = channel.split(".")
        if not parts:
            return channel.replace(".", ":")

        kind = parts[0]

        if kind == "kline":
            interval = parts[1] if len(parts) > 1 else "5"
            interval_label = f"{interval}m" if interval.isdigit() else interval
            return f"kline:{interval_label}"

        if kind == "orderbook":
            depth = parts[1] if len(parts) > 1 else "10"
            return "ob:L10" if depth == "10" else f"ob:L{depth}"

        return channel.replace(".", ":")
