from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

import asyncpg
from pydantic import ValidationError

from src.core.exceptions import DatabaseError
from src.core.logging_config import get_logger
from src.core.models import Signal
from src.db.connection import get_pool

__all__ = ["SignalRepository"]

logger = get_logger("db.repositories.signal_repository")


class SignalRepository:
    """
    Репозиторий для работы с таблицей `signals`.

    Обязанности:
      * сохранять новые сигналы, приходящие из стратегии AVI-5;
      * читать сигнал по ID;
      * читать последние сигналы (для UI / калибрации / аналитики);
      * обновлять error_code / error_message после обработки сигнала.

    Контракт по схеме БД:

      - в таблице используется колонка `side`, а в модели — поле `direction`;
      - TP/SL хранятся как `tp1_price`, `tp2_price`, `tp3_price`, `sl_price`,
        а в модели называются `tp1`, `tp2`, `tp3`, `stop_loss`.
      - репозиторий берёт на себя маппинг имён колонок ↔ полям модели.
    """

    # ---------- Внутренние помощники ----------

    @staticmethod
    def _get_pool() -> asyncpg.pool.Pool:
        """
        Получить пул соединений, оборачивая ошибку инициализации в DatabaseError.
        """
        try:
            return get_pool()
        except RuntimeError as exc:  # пул не инициализирован / закрыт
            logger.error("PostgreSQL pool is not available", error=str(exc))
            raise DatabaseError(
                "PostgreSQL pool is not available",
                details={"error": str(exc)},
            ) from exc

    @staticmethod
    def _record_to_signal(record: asyncpg.Record) -> Signal:
        """
        Преобразовать запись asyncpg в доменную модель Signal.

        Выполняется маппинг колонок БД к полям pydantic-модели.
        """
        data = dict(record)

        # side -> direction
        if "side" in data and "direction" not in data:
            data["direction"] = data.pop("side")

        # tp*_price / sl_price -> tp*, stop_loss
        mapping = {
            "tp1_price": "tp1",
            "tp2_price": "tp2",
            "tp3_price": "tp3",
            "sl_price": "stop_loss",
        }
        for db_field, model_field in mapping.items():
            if db_field in data and model_field not in data:
                data[model_field] = data.pop(db_field)

        try:
            return Signal(**data)
        except ValidationError as exc:
            # Если данные в БД не соответствуют контракту модели — это ошибка целостности.
            raise DatabaseError(
                "Failed to hydrate Signal from database record",
                details={"errors": exc.errors(), "raw": data},
            ) from exc

    # ---------- Публичный API ----------

    async def create(self, signal: Signal) -> Signal:
        """
        Сохранить новый сигнал в БД.

        :param signal: Доменная модель сигнала.
        :return: Сохранённый сигнал (по данным из БД).
        :raises DatabaseError: при ошибках уровня БД.
        """
        pool = self._get_pool()

        query = """
            INSERT INTO signals (
                id,
                created_at,
                symbol,
                side,
                entry_price,
                stake_usd,
                probability,
                strategy,
                strategy_version,
                queued_until,
                tp1_price,
                tp2_price,
                tp3_price,
                sl_price,
                error_code,
                error_message
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8,
                $9, $10, $11, $12, $13, $14, $15, $16
            )
            RETURNING *
        """

        logger.info(
            "Inserting new signal into DB",
            signal_id=str(signal.id),
            symbol=signal.symbol,
            direction=signal.direction,
        )

        values = (
            signal.id,
            signal.created_at,
            signal.symbol,
            signal.direction,  # маппится в колонку side
            signal.entry_price,
            signal.stake_usd,
            signal.probability,
            signal.strategy,
            signal.strategy_version,
            signal.queued_until,
            signal.tp1,
            signal.tp2,
            signal.tp3,
            signal.stop_loss,
            signal.error_code,
            signal.error_message,
        )

        try:
            async with pool.acquire() as conn:
                record = await conn.fetchrow(query, *values)
        except asyncpg.PostgresError as exc:
            logger.exception(
                "Failed to insert signal into DB",
                signal_id=str(signal.id),
                symbol=signal.symbol,
            )
            raise DatabaseError(
                "Failed to insert signal into DB",
                details={
                    "signal_id": str(signal.id),
                    "symbol": signal.symbol,
                    "error": str(exc),
                },
            ) from exc

        if record is None:
            # Такое в норме не должно происходить (INSERT .. RETURNING *)
            raise DatabaseError(
                "INSERT INTO signals returned no rows",
                details={"signal_id": str(signal.id)},
            )

        return self._record_to_signal(record)

    async def get_by_id(self, signal_id: UUID) -> Signal:
        """
        Получить сигнал по его идентификатору.

        :raises DatabaseError: если сигнал не найден или при ошибке БД.
        """
        pool = self._get_pool()

        query = """
            SELECT *
            FROM signals
            WHERE id = $1
        """

        try:
            async with pool.acquire() as conn:
                record = await conn.fetchrow(query, signal_id)
        except asyncpg.PostgresError as exc:
            logger.exception(
                "Failed to fetch signal by id",
                signal_id=str(signal_id),
            )
            raise DatabaseError(
                "Failed to fetch signal by id",
                details={"signal_id": str(signal_id), "error": str(exc)},
            ) from exc

        if record is None:
            raise DatabaseError(
                "Signal not found",
                details={"signal_id": str(signal_id)},
            )

        return self._record_to_signal(record)

    async def list_recent(
        self,
        *,
        limit: int,
        symbol: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Signal]:
        """
        Вернуть список последних сигналов.

        :param limit: Максимальное количество записей.
        :param symbol: Опциональный фильтр по символу.
        :param since: Опциональная нижняя граница по created_at.
        :return: Список сигналов, отсортированный по created_at DESC.
        """
        pool = self._get_pool()

        params = []
        where_clauses = []
        idx = 1

        if symbol is not None:
            where_clauses.append(f"symbol = ${idx}")
            params.append(symbol)
            idx += 1

        if since is not None:
            where_clauses.append(f"created_at >= ${idx}")
            params.append(since)
            idx += 1

        where_clause = ""
        if where_clauses:
            where_clause = "WHERE " + " AND ".join(where_clauses)

        query = f"""
            SELECT *
            FROM signals
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ${idx}
        """

        params.append(limit)

        logger.debug(
            "Listing recent signals",
            symbol=symbol,
            since=since.isoformat() if since else None,
            limit=limit,
        )

        try:
            async with pool.acquire() as conn:
                records = await conn.fetch(query, *params)
        except asyncpg.PostgresError as exc:
            logger.exception(
                "Failed to list recent signals",
                symbol=symbol,
            )
            raise DatabaseError(
                "Failed to list recent signals",
                details={
                    "symbol": symbol,
                    "since": since.isoformat() if since else None,
                    "limit": limit,
                    "error": str(exc),
                },
            ) from exc

        return [self._record_to_signal(r) for r in records]

    async def update_error_fields(
        self,
        *,
        signal_id: UUID,
        error_code: Optional[int],
        error_message: Optional[str],
    ) -> Signal:
        """
        Обновить поля error_code / error_message у сигнала.

        Возвращает обновлённый сигнал.
        """
        pool = self._get_pool()

        query = """
            UPDATE signals
            SET
                error_code    = $2,
                error_message = $3
            WHERE id = $1
            RETURNING *
        """

        logger.debug(
            "Updating signal error fields",
            signal_id=str(signal_id),
            error_code=error_code,
        )

        try:
            async with pool.acquire() as conn:
                record = await conn.fetchrow(query, signal_id, error_code, error_message)
        except asyncpg.PostgresError as exc:
            logger.exception(
                "Failed to update signal error fields",
                signal_id=str(signal_id),
            )
            raise DatabaseError(
                "Failed to update signal error fields",
                details={
                    "signal_id": str(signal_id),
                    "error_code": error_code,
                    "error": str(exc),
                },
            ) from exc

        if record is None:
            raise DatabaseError(
                "Signal not found when updating error fields",
                details={"signal_id": str(signal_id)},
            )

        return self._record_to_signal(record)
