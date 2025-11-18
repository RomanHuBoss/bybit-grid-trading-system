from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Sequence

import asyncpg

from src.core.exceptions import DatabaseError
from src.core.logging_config import get_logger
from src.integration.bybit.ws_client import BybitWSClient

logger = get_logger(__name__)


class OrderbookLogger:
    """
    Логирование снимков стакана L50 в таблицу `orderbook_l50_log`.

    Основная идея R-01:
    - отдельный потребитель, который слушает orderbook-каналы Bybit WS;
    - пишет снимки стакана (BTC/ETH и др. символы по конфигу) в TimescaleDB;
    - используется для ресёрча и пост-анализов (slippage, impact, ликвидность).
    """

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        ws_client: BybitWSClient,
        symbols: Sequence[str],
        depth: int = 50,
        table_name: str = "orderbook_l50_log",
    ) -> None:
        """
        :param pool: Пул соединений asyncpg к PostgreSQL/TimescaleDB.
        :param ws_client: Низкоуровневый клиент Bybit WS (orderbook).
        :param symbols: Список символов для логирования (BTCUSDT, ETHUSDT, ...).
        :param depth: Глубина стакана в канале WS. Для R-01 — 50 (L50).
        :param table_name: Имя целевой таблицы (по умолчанию `orderbook_l50_log`).
        """
        if depth <= 0:
            raise ValueError("depth must be positive")

        if not symbols:
            raise ValueError("symbols must be a non-empty sequence")

        self._pool = pool
        self._ws_client = ws_client
        self._symbols: List[str] = list(symbols)
        self._depth = depth
        self._table_name = table_name

        # Предподготовленный SQL шаблон для вставки.
        self._insert_sql = (
            f"INSERT INTO {self._table_name} "
            "(ts, symbol, snapshot) "
            "VALUES ($1, $2, $3)"
        )

    async def start(self) -> None:
        """
        Старт логгера:

        1. Подписывается на `orderbook.{depth}.{symbol}` для всех символов.
        2. Запускает бесконечный цикл чтения WS и записи в БД.

        Исключения WS (WSConnectionError и т.п.) наружу не глушатся — пусть
        решает оркестратор (systemd/k8s), перезапускать ли сервис.
        """
        topics = [f"orderbook.{self._depth}.{symbol}" for symbol in self._symbols]

        logger.info(
            "Starting OrderbookLogger: subscribing to orderbook topics",
            depth=self._depth,
            symbols=self._symbols,
            topics=topics,
        )

        await self._ws_client.subscribe(topics)
        await self._run_loop()

    async def _run_loop(self) -> None:
        """Основной цикл обработки сообщений WS и записи в БД."""
        async for channel, data, _seq in self._ws_client.listen():
            # Нас интересуют только orderbook-каналы.
            if not channel.startswith("orderbook."):
                continue

            try:
                symbol = self._extract_symbol_from_channel(channel)
                ts = self._extract_timestamp(data)
                snapshot = self._normalize_snapshot(data)

                await self._insert_snapshot(ts=ts, symbol=symbol, snapshot=snapshot)
            except asyncio.CancelledError:
                raise
            except DatabaseError:
                # Ошибки БД логируем и продолжаем — это вспомогательный ресёрч-лог.
                logger.error(
                    "Database error while inserting orderbook snapshot",
                    channel=channel,
                    symbol=data.get("symbol"),
                    exc_info=True,
                )
            except Exception:  # noqa: BLE001
                # Любые неожиданные ошибки тоже логируем, но не роняем цикл.
                logger.error(
                    "Unexpected error in OrderbookLogger",
                    channel=channel,
                    data_preview=str(data)[:512],
                    exc_info=True,
                )

    def _extract_symbol_from_channel(self, channel: str) -> str:
        """
        Извлечь символ из имени канала.

        Ожидаемый формат: `orderbook.{depth}.{symbol}`.
        """
        parts = channel.split(".")
        if len(parts) < 3:
            # Фоллбэк: берём последний кусок.
            return parts[-1]
        return parts[2]

    def _extract_timestamp(self, data: Dict[str, Any]) -> datetime:
        """
        Вытащить timestamp из WS-снимка.

        Приоритет:
        1. data["ts"] — в миллисекундах, как часто делает Bybit.
        2. data["T"] или data["time"] — в миллисекундах/секундах.
        3. текущий `utcnow()` как фоллбэк.
        """
        ts = data.get("ts")

        if ts is None:
            ts = data.get("T") or data.get("time")

        if ts is None:
            return datetime.now(tz=timezone.utc)

        # Попробуем интерпретировать как число (ms или s).
        try:
            ts_num = float(ts)
        except (TypeError, ValueError):
            return datetime.now(tz=timezone.utc)

        # Heuristics: миллисекунды vs секунды.
        # В 2020-х годах timestamp в секундах ≈ 1.6e9, в мс — 1.6e12.
        if ts_num > 10**11:  # похоже на мс
            seconds = ts_num / 1000.0
        else:
            seconds = ts_num

        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    def _normalize_snapshot(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Нормализовать snapshot для записи в JSONB.

        `BybitWSClient.listen()` уже отдаёт нам "data" по сути полезной нагрузки,
        но структура может отличаться между full и delta-обновлениями.
        Мы храним snapshot как есть (dict), не навязывая жёсткую схему — для ресёрча
        это допустимо, а DDL может эволюционировать.
        """
        # Клонируем объект, чтобы не словить побочные изменения.
        try:
            snapshot = json.loads(json.dumps(data))
        except TypeError:
            # На случай, если внутри есть несерилизуемые типы — выкинем их.
            snapshot = {}
            for k, v in data.items():
                try:
                    json.dumps(v)
                except TypeError:
                    continue
                else:
                    snapshot[k] = v
        return snapshot

    async def _insert_snapshot(
        self,
        *,
        ts: datetime,
        symbol: str,
        snapshot: Dict[str, Any],
    ) -> None:
        """
        Записать снимок стакана в таблицу `orderbook_l50_log`.

        Структура таблицы (логический контракт):
        - ts      — TIMESTAMPTZ, время замера;
        - symbol  — TEXT;
        - snapshot — JSONB с полным содержимым WS-сообщения (нормализованным).
        """
        payload = json.dumps(snapshot, separators=(",", ":"))

        try:
            await self._pool.execute(self._insert_sql, ts, symbol, payload)
        except asyncpg.PostgresError as exc:
            sqlstate = getattr(exc, "sqlstate", None)
            raise DatabaseError(
                "Database error while inserting orderbook snapshot",
                details={
                    "sqlstate": sqlstate or "",
                    "symbol": symbol,
                    "ts": ts.isoformat(),
                    "table": self._table_name,
                },
            ) from exc


async def run_orderbook_logger(
    *,
    pool: asyncpg.Pool,
    ws_client: BybitWSClient,
    symbols: Iterable[str],
    depth: int = 50,
) -> None:
    """
    Утилитарная обёртка для запуска логгера стакана L50 как отдельного таска.

    Пример использования в сервисе:

        logger_task = asyncio.create_task(
            run_orderbook_logger(
                pool=db_pool,
                ws_client=ws_client,
                symbols=["BTCUSDT", "ETHUSDT"],
                depth=50,
            )
        )

    Решение, включать ли этот логгер, принимается на уровне конфигурации
    (например, `config.data.log_orderbook_l50.enabled == true`).
    """
    symbols_list = list(symbols)
    ob_logger = OrderbookLogger(
        pool=pool,
        ws_client=ws_client,
        symbols=symbols_list,
        depth=depth,
    )
    await ob_logger.start()
