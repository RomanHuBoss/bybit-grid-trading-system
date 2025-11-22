from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core.logging_config import get_logger
from src.integration.bybit.ws_client import BybitWSClient

logger = get_logger(__name__)


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
        """
        :param ws_client: Низкоуровневый клиент Bybit WebSocket.
        :param redis: Экземпляр async Redis-клиента.
        :param stream_prefix: Префикс для Redis Streams (по умолчанию `ws_raw`).
        """
        self._ws_client = ws_client
        self._redis = redis
        self._stream_prefix = stream_prefix.rstrip(":")

    async def subscribe_klines(
        self,
        *,
        interval: str,
        symbols: Iterable[str],
    ) -> None:
        """
        Подписаться на kline-каналы для списка символов.

        Формат топика: `kline.{interval}.{symbol}`.

        Исключения:
            RateLimitError из нижележащего RateLimiter/WS-клиента.
        """
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
        """
        Подписаться на orderbook-каналы для списка символов.

        Формат топика: `orderbook.{depth}.{symbol}`.

        :param symbols: перечисление торгуемых символов
        :param depth: Глубина стакана от 1 до 50.
        :raises ValueError: если depth вне диапазона [1, 50].
        """
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
        """
        Бесконечный цикл чтения сообщений из WS и публикации в Redis Streams.

        Для каждого сообщения:
            1. Проверяется дубликат по sequence (per-channel).
            2. Сообщение публикуется в соответствующий Stream: `ws_raw:{stream}`.
        """
        logger.info("Starting DataCollector run loop")

        async for channel, data, sequence in self._ws_client.listen():
            try:
                is_new = await self.deduplicate_message(channel, sequence=sequence)
            except Exception:
                # В случае ошибки дедупликации считаем сообщение уникальным, но логируем.
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
                # Ошибка Redis не должна ронять весь collector, продолжаем.
                continue

    async def publish_to_stream(
        self,
        *,
        stream: str,
        data: Dict[str, Any],
    ) -> str:
        """
        Опубликовать сообщение в указанный Redis Stream.

        :param stream: Логическое имя потока без префикса, например `kline:5m`.
        :param data: Словарь с данными сообщения.
        :return: Идентификатор сообщения в Stream.
        :raises RedisError: при ошибке записи в Redis.
        """
        full_stream = f"{self._stream_prefix}:{stream}"

        # redis-py принимает значения, приводимые к байтам/строкам.
        msg_id = await self._redis.xadd(full_stream, data)

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
        """
        Проверка сообщения на дубликат по `sequence` в рамках канала.

        Алгоритм:
            * читаем `last_seq:{channel}` из Redis;
            * если sequence <= last_seq — считаем сообщение дубликатом;
            * иначе обновляем last_seq и возвращаем True.

        :param channel: Имя WS-канала (`topic`), например `kline.5.BTCUSDT`.
        :param sequence: Порядковый номер сообщения из Bybit WS.
        :param ttl_seconds: TTL ключа last_seq в Redis, по умолчанию 1 час.
        :return: True, если сообщение новое; False, если дубликат/устаревшее.
        """
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

        # Обновляем last_seq и выставляем TTL.
        await self._redis.set(key, str(sequence))
        if ttl_seconds is not None:
            await self._redis.expire(key, ttl_seconds)

        return True

    def _channel_to_stream(self, channel: str) -> str:
        """
        Преобразовать имя WS-канала в логическое имя Redis Stream.

        Примеры:
            * `kline.5.BTCUSDT`      -> `kline:5m`
            * `orderbook.10.BTCUSDT` -> `ob:L10`
        """
        parts = channel.split(".")
        if not parts:
            return channel.replace(".", ":")

        kind = parts[0]

        if kind == "kline":
            # Стратегия работает на 5m; интервал храним с суффиксом `m`.
            interval = parts[1] if len(parts) > 1 else "5"
            if interval.isdigit():
                interval_label = f"{interval}m"
            else:
                interval_label = interval
            return f"kline:{interval_label}"

        if kind == "orderbook":
            depth = parts[1] if len(parts) > 1 else "10"
            return "ob:L10" if depth == "10" else f"ob:L{depth}"

        # Фоллбэк для неизвестных каналов.
        return channel.replace(".", ":")
