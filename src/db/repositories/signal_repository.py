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

    Контракт по схеме БД (упрощённо, по модели Signal):
      * id: UUID (PK)
      * created_at: TIMESTAMPTZ
      * symbol: TEXT
      * direction: TEXT ('long' / 'short')
      * entry_price: NUMERIC
      * stake_usd: NUMERIC
      * probability: NUMERIC
      * strategy_version: TEXT
      * queued_until: TIMESTAMPTZ NULL
      * tp1/tp2/tp3: NUMERIC NULL
      * stop_loss: NUMERIC NULL
      * error_code: INT NULL
      * error_message: TEXT NULL
    """

    def __init__(self) -> None:
        # Репозиторий опирается на глобальный пул соединений из src.db.connection.
        self._pool: Optional[asyncpg.Pool] = None

    def _get_pool(self) -> asyncpg.Pool:
        pool = get_pool()
        if pool is None:
            raise RuntimeError("Database pool is not initialized")
        return pool

    @staticmethod
    def _record_to_signal(record: asyncpg.Record) -> Signal:
        """
        Преобразовать запись asyncpg в доменную модель Signal.

        Выполняется маппинг колонок БД к полям pydantic-модели.
        """
        data = dict(record)

        # На всякий случай нормализуем типы UUID.
        if "id" in data and not isinstance(data["id"], UUID):
            data["id"] = UUID(str(data["id"]))

        # side -> direction (на случай, если в БД/старом API поле называлось side).
        if "side" in data and "direction" not in data:
            data["direction"] = data.pop("side")

        # При несовпадении имён колонок и модели (например, tp1_price → tp1),
        # здесь можно сделать дополнительный маппинг.
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
        except ValidationError as exc:  # pragma: no cover — защитный слой
            raise DatabaseError(
                "Failed to validate Signal from database record",
                details={"record": repr(record), "error": str(exc)},
            ) from exc

    async def create(self, signal: Signal) -> Signal:
        """
        Сохранить новый сигнал в БД и вернуть его фактическое состояние.

        :param signal: Доменная модель сигнала.
        :return: Сохранённая модель сигнала (включая id/created_at и т.п.).
        """
        pool = self._get_pool()

        query = """
            INSERT INTO signals (
                id,
                created_at,
                symbol,
                direction,
                entry_price,
                stake_usd,
                probability,
                strategy_version,
                queued_until,
                tp1,
                tp2,
                tp3,
                stop_loss,
                error_code,
                error_message
            )
            VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11, $12,
                $13, $14, $15
            )
            RETURNING *
        """

        values = (
            signal.id,
            signal.created_at,
            signal.symbol,
            signal.direction,
            signal.entry_price,
            signal.stake_usd,
            signal.probability,
            signal.strategy_version,
            signal.queued_until,
            signal.tp1,
            signal.tp2,
            signal.tp3,
            signal.stop_loss,
            signal.error_code,
            signal.error_message,
        )

        logger.debug(
            "Inserting new signal",
            extra={
                "signal_id": str(signal.id),
                "symbol": signal.symbol,
                "direction": signal.direction,
            },
        )

        try:
            async with pool.acquire() as conn:
                record = await conn.fetchrow(query, *values)
        except asyncpg.PostgresError as exc:
            logger.exception(
                "Failed to insert signal",
                extra={
                    "signal_id": str(signal.id),
                    "symbol": signal.symbol,
                },
            )
            raise DatabaseError(
                "Failed to insert signal",
                details={"signal_id": str(signal.id), "error": str(exc)},
            ) from exc

        if record is None:
            # По INSERT ... RETURNING мы ожидаем ровно одну запись.
            raise DatabaseError(
                "Insert signal returned no rows",
                details={"signal_id": str(signal.id)},
            )

        return self._record_to_signal(record)

    async def get_by_id(self, signal_id: UUID) -> Signal:
        """
        Получить сигнал по его идентификатору.

        :param signal_id: UUID сигнала.
        :return: Найденный сигнал.
        :raises DatabaseError: Если сигнал не найден или возникает ошибка БД.
        """
        pool = self._get_pool()

        query = """
            SELECT *
            FROM signals
            WHERE id = $1
        """

        logger.debug("Fetching signal by id", extra={"signal_id": str(signal_id)})

        try:
            async with pool.acquire() as conn:
                record = await conn.fetchrow(query, signal_id)
        except asyncpg.PostgresError as exc:
            logger.exception(
                "Failed to fetch signal by id", extra={"signal_id": str(signal_id)}
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

        # Список параметров для asyncpg. Здесь могут быть строки, даты, числа,
        # поэтому типизируем как list[object], чтобы mypy не пытался сузить до list[str].
        params: list[object] = []
        where_clauses: list[str] = []
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
            extra={
                "symbol": symbol,
                "since": since.isoformat() if since else None,
                "limit": limit,
            },
        )

        try:
            async with pool.acquire() as conn:
                records = await conn.fetch(query, *params)
        except asyncpg.PostgresError as exc:
            logger.exception(
                "Failed to list recent signals",
                extra={
                    "symbol": symbol,
                    "since": since.isoformat() if since else None,
                    "limit": limit,
                },
            )
            raise DatabaseError(
                "Failed to list recent signals",
                details={
                    "symbol": symbol,
                    "since": since.isoformat() if since else None,
                    "limit": limit,
                    "error": str(exc)},
            ) from exc

        return [self._record_to_signal(record) for record in records]

    async def update_error_fields(
        self,
        *,
        signal_id: UUID,
        error_code: Optional[int],
        error_message: Optional[str],
    ) -> Signal:
        """
        Обновить error_code и error_message у сигнала.

        Используется исполнением/ордер-менеджером для фиксации ошибок обработки
        конкретного сигнала.
        """
        pool = self._get_pool()

        query = """
            UPDATE signals
            SET
                error_code = $2,
                error_message = $3
            WHERE id = $1
            RETURNING *
        """

        logger.debug(
            "Updating signal error fields",
            extra={
                "signal_id": str(signal_id),
                "error_code": error_code,
            },
        )

        try:
            async with pool.acquire() as conn:
                record = await conn.fetchrow(
                    query,
                    signal_id,
                    error_code,
                    error_message,
                )
        except asyncpg.PostgresError as exc:
            logger.exception(
                "Failed to update signal error fields",
                extra={
                    "signal_id": str(signal_id),
                    "error_code": error_code,
                },
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
