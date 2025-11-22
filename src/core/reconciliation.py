from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Tuple

from redis.asyncio import Redis

from src.core.distributed_lock import acquire_lock
from src.core.logging_config import get_logger
from src.core.models import Position
from src.db.repositories.position_repository import PositionRepository
from src.integration.bybit.rest_client import BybitRESTClient

__all__ = ["ReconciliationConfig", "ReconciliationService"]

logger = get_logger("core.reconciliation")


@dataclass(frozen=True)
class ReconciliationConfig:
    """
    Параметры процесса reconciliation.

    run_interval_sec:
        Как часто запускать reconciliation (используется внешним планировщиком).
        Здесь только как справочная информация/настройка.
    close_missing_in_db:
        Зарезервированный флаг на будущее. В текущей реализации модуль
        никогда не инициирует действий по бирже и только логирует факт,
        что позиция присутствует на бирже и отсутствует в БД.
    close_missing_on_exchange:
        Если True — при отсутствии позиции на бирже, но наличии в БД,
        позиция в БД помечается закрытой с closed_at=now.
    """

    run_interval_sec: int = 60
    close_missing_in_db: bool = False
    close_missing_on_exchange: bool = True


class ReconciliationService:
    """
    Сервис сверки состояния позиций между локальной БД и Bybit.

    Задачи:
    - гарантировать, что одновременно работает только один экземпляр (через Redis-lock);
    - получить список открытых позиций из БД;
    - получить актуальный список позиций с биржи;
    - сравнить их по (symbol, direction) и размеру:
        * "висячие" позиции в БД, которых нет на бирже → опционально закрыть в БД;
        * позиции на бирже, которых нет в БД → залогировать как аномалию;
        * расхождения по размеру → обновить размер в БД под фактический размер с биржи.
    """

    def __init__(
        self,
        *,
        redis: Redis,
        rest_client: BybitRESTClient,
        position_repository: PositionRepository,
        config: ReconciliationConfig | None = None,
        lock_name: str = "positions_reconciliation",
    ) -> None:
        self._redis = redis
        self._rest = rest_client
        self._positions = position_repository
        self._config = config or ReconciliationConfig()
        self._lock_name = lock_name

    # ------------------------------------------------------------------ #
    # Публичный API
    # ------------------------------------------------------------------ #

    async def reconcile(self) -> None:
        """
        Запустить один проход reconciliation.

        Используется внешним планировщиком (cron, background-task и т.п.).
        Если lock уже удерживается другим процессом, просто выходим.
        """
        async with acquire_lock(self._redis, self._lock_name) as lock:
            if not lock.locked:
                logger.info(
                    "Reconciliation skipped: lock is held by another worker",
                    lock_name=self._lock_name,
                )
                return

            await self._do_reconcile()

    # ------------------------------------------------------------------ #
    # Основная логика сверки
    # ------------------------------------------------------------------ #

    async def _do_reconcile(self) -> None:
        now = datetime.now(timezone.utc)

        db_positions = await self._positions.list_open()
        exchange_positions = await self._load_exchange_positions()

        db_index = self._index_db_positions(db_positions)
        exch_index = self._index_exchange_positions(exchange_positions)

        logger.info(
            "Starting reconciliation",
            db_positions=len(db_positions),
            exchange_positions=len(exchange_positions),
        )

        # 1) Позиции, которые есть в БД, но отсутствуют на бирже.
        await self._handle_missing_on_exchange(
            db_index=db_index,
            exch_index=exch_index,
            now=now,
        )

        # 2) Позиции, которые есть на бирже, но отсутствуют в БД.
        self._handle_missing_in_db(
            db_index=db_index,
            exch_index=exch_index,
        )

        # 3) Расхождения по размеру позиций.
        await self._handle_size_mismatches(
            db_index=db_index,
            exch_index=exch_index,
        )

        logger.info("Reconciliation completed")

    # ------------------------------------------------------------------ #
    # Загрузка и индексация позиций
    # ------------------------------------------------------------------ #

    async def _load_exchange_positions(self) -> List[Mapping[str, object]]:
        """
        Получить список позиций с Bybit.

        Ожидаемый формат ответа (упрощённо):
            {
              "result": {
                "list": [
                  {
                    "symbol": "BTCUSDT",
                    "side": "Buy" / "Sell",
                    "size": "0.01",
                    "entryPrice": "30000",
                    ...
                  },
                  ...
                ]
              }
            }

        В случае ошибки API пробрасываем исключение наверх — пусть оркестратор
        решает, ретраить или алертить.
        """
        resp = await self._rest.request(
            "GET",
            "/v5/position/list",
            params={"category": "linear"},
            auth=True,
            is_order=False,
            read_weight=1,
        )

        result = resp.get("result") if isinstance(resp, dict) else None
        if not isinstance(result, dict):
            logger.warning("Bybit position list response missing 'result' dict", response=resp)
            return []

        raw_list = result.get("list")
        if not isinstance(raw_list, list):
            logger.warning("Bybit position list response missing 'list'", response=resp)
            return []

        positions: List[Mapping[str, object]] = [
            row for row in raw_list if isinstance(row, dict)
        ]
        return positions

    @staticmethod
    def _index_db_positions(
        positions: Iterable[Position],
    ) -> Dict[Tuple[str, str], Position]:
        """
        Индексация позиций из БД по ключу (symbol_upper, direction_lower).

        Здесь `direction` — доменное направление позиции: long / short.
        """
        index: Dict[Tuple[str, str], Position] = {}
        for p in positions:
            key = (p.symbol.upper(), p.direction.lower())
            index[key] = p
        return index

    @staticmethod
    def _index_exchange_positions(
        positions: Iterable[Mapping[str, object]],
    ) -> Dict[Tuple[str, str], Mapping[str, object]]:
        """
        Индексация позиций с биржи по ключу (symbol_upper, direction_lower).

        Bybit возвращает поле `side` в формате `Buy` / `Sell`. Для того,
        чтобы ключи совпадали с доменной моделью Position (direction: long/short),
        мы нормализуем значения:
        - Buy/long  -> long
        - Sell/short -> short
        """
        index: Dict[Tuple[str, str], Mapping[str, object]] = {}
        for row in positions:
            symbol_raw = row.get("symbol")
            side_raw = row.get("side")
            if not isinstance(symbol_raw, str) or not isinstance(side_raw, str):
                continue

            symbol = str(symbol_raw).upper()
            side = str(side_raw).lower()
            if side in ("buy", "long"):
                direction = "long"
            elif side in ("sell", "short"):
                direction = "short"
            else:
                # Непонятное состояние — лучше залогировать и пропустить позицию.
                logger.warning(
                    "Unknown position side from exchange, skipping row",
                    raw_side=side_raw,
                    symbol=symbol_raw,
                )
                continue

            key = (symbol, direction)
            index[key] = row
        return index

    # ------------------------------------------------------------------ #
    # Обработка расхождений
    # ------------------------------------------------------------------ #

    async def _handle_missing_on_exchange(
        self,
        *,
        db_index: Dict[Tuple[str, str], Position],
        exch_index: Dict[Tuple[str, str], Mapping[str, object]],
        now: datetime,
    ) -> None:
        """
        Позиции, которые есть в БД, но отсутствуют на бирже.

        Если close_missing_on_exchange == True — помечаем их закрытыми.
        В любом случае логируем предупреждение.
        """
        for key, position in db_index.items():
            if key in exch_index:
                continue

            symbol, direction = key
            logger.warning(
                "DB position missing on exchange",
                position_id=str(position.id),
                symbol=symbol,
                direction=direction,
            )

            if not self._config.close_missing_on_exchange:
                continue

            closed = await self._positions.mark_closed(position.id, closed_at=now)
            logger.info(
                "DB position marked closed due to missing on exchange",
                position_id=str(position.id),
                closed_exists=closed is not None,
            )

    def _handle_missing_in_db(
        self,
        *,
        db_index: Dict[Tuple[str, str], Position],
        exch_index: Dict[Tuple[str, str], Mapping[str, object]],
    ) -> None:
        """
        Позиции, которые есть на бирже, но отсутствуют в БД.

        Модуль не открывает/не закрывает позиции на бирже — только логирует
        критические расхождения. Дополнительные действия (алерты, ручной разбор)
        остаются на внешних консьюмерах логов/метрик.
        """
        for key, row in exch_index.items():
            if key in db_index:
                continue

            symbol, direction = key
            size = row.get("size")
            entry_price = row.get("entryPrice") or row.get("avgPrice")

            logger.error(
                "Position present on exchange but missing in DB",
                symbol=symbol,
                direction=direction,
                size=str(size),
                entry_price=str(entry_price),
            )

            # По желанию в будущем можно добавить метрику/алерт.

    async def _handle_size_mismatches(
        self,
        *,
        db_index: Dict[Tuple[str, str], Position],
        exch_index: Dict[Tuple[str, str], Mapping[str, object]],
    ) -> None:
        """
        Для совпадающих по (symbol, direction) позиций проверяем размер.

        Если фактический размер на бирже отличается от размера в БД, обновляем
        size_base / size_quote позиции в БД, а также логируем сам факт.
        """
        for key, position in db_index.items():
            row = exch_index.get(key)
            if row is None:
                continue

            exch_size = self._to_decimal(row.get("size"))
            exch_entry_price = self._to_decimal(row.get("entryPrice") or row.get("avgPrice"))

            if exch_size is None or exch_entry_price is None:
                continue

            db_size = position.size_base
            if db_size == exch_size:
                continue

            old_base = position.size_base
            old_quote = position.size_quote

            position.size_base = exch_size
            position.size_quote = (exch_size * exch_entry_price).copy_abs()

            await self._positions.update(position)

            logger.info(
                "Position size reconciled with exchange",
                position_id=str(position.id),
                symbol=position.symbol,
                direction=position.direction,
                old_size_base=str(old_base),
                new_size_base=str(position.size_base),
                old_size_quote=str(old_quote),
                new_size_quote=str(position.size_quote),
            )

    # ------------------------------------------------------------------ #
    # Утилиты
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_decimal(value: object) -> Decimal | None:
        """
        Мягкое преобразование к Decimal.

        При None или непарсимом значении возвращает None.
        """
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:  # noqa: BLE001
            return None
