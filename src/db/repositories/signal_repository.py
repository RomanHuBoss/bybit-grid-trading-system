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


class SignalRepository:
    """
    Репозиторий для работы с таблицей сигналов в БД.

    Отвечает за:
    - сохранение новых сигналов, публикуемых AVI-5;
    - выборку сигналов по ID;
    - выборку последних сигналов (для UI/аналитики);
    - обновление полей error_code / error_message после обработки сигнала.
    """

    def __init__(self) -> None:
        # Имя логгера соответствует пространству модулей
        self._logger = get_logger("db.repositories.signal_repository")

    # ---------- Внутренние помощники ----------

    def _get_pool(self) -> asyncpg.Pool:
        """
        Получить пул соединений, оборачивая ошибку инициализации в DatabaseError.
        """
        try:
            return get_pool()
        except RuntimeError as exc:  # пул не инициализирован / закрыт
            self._logger.error("PostgreSQL pool is not available", error=str(exc))
            raise DatabaseError(
                "PostgreSQL pool is not available",
                details={"error": str(exc)},
            ) from exc

    @staticmethod
    def _record_to_signal(record: asyncpg.Record) -> Signal:
        """
        Преобразовать запись asyncpg в доменную модель Signal.

        Предполагается, что имена колонок в таблице `signals`
        совпадают с полями модели Signal.
        """
        data = dict(record)
        try:
            return Signal(**data)
        except ValidationError as exc:
            # Если данные в БД не соответствуют контракту модели — это считаем
            # ошибкой целостности.
            raise DatabaseError(
                "Failed to hydrate Signal from database record",
                details={"errors": exc.errors()},
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
                direction,
                entry_price,
                stake_usd,
                probability,
                strategy,
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
                $1, $2, $3, $4, $5, $6, $7, $8,
                $9, $10, $11, $12, $13, $14, $15, $16
            )
            RETURNING *
        """

        self._logger.info(
            "Inserting new signal into DB",
            signal_id=str(signal.id),
            symbol=signal.symbol,
            direction=signal.direction,
        )

        try:
            async with pool.acquire() as conn:
                record = await conn.fetchrow(
                    query,
                    signal.id,
                    signal.created_at,
                    signal.symbol,
                    signal.direction,
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
        except asyncpg.PostgresError as exc:
            self._logger.exception(
                "Failed to insert signal",
                signal_id=str(signal.id),
            )
            raise DatabaseError(
                "Failed to insert signal",
                details={"signal_id": str(signal.id), "error": str(exc)},
            ) from exc

        if record is None:
            # INSERT ... RETURNING всегда должен вернуть строку.
            raise DatabaseError(
                "INSERT INTO signals returned no row",
                details={"signal_id": str(signal.id)},
            )

        return self._record_to_signal(record)

    async def get_by_id(self, signal_id: UUID) -> Optional[Signal]:
        """
        Получить сигнал по его ID.

        :param signal_id: Идентификатор сигнала.
        :return: Signal или None, если не найден.
        :raises DatabaseError: при ошибках уровня БД.
        """
        pool = self._get_pool()

        query = """
            SELECT *
            FROM signals
            WHERE id = $1
        """

        self._logger.debug("Fetching signal by id", signal_id=str(signal_id))

        try:
            async with pool.acquire() as conn:
                record = await conn.fetchrow(query, signal_id)
        except asyncpg.PostgresError as exc:
            self._logger.exception("Failed to fetch signal by id", signal_id=str(signal_id))
            raise DatabaseError(
                "Failed to fetch signal by id",
                details={"signal_id": str(signal_id), "error": str(exc)},
            ) from exc

        if record is None:
            return None

        return self._record_to_signal(record)

    async def list_recent(
        self,
        *,
        limit: int = 100,
        symbol: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Signal]:
        """
        Получить список последних сигналов для UI/аналитики.

        :param limit: Максимальное количество записей.
        :param symbol: Опциональный фильтр по символу.
        :param since: Опциональный фильтр по created_at >= since.
        :return: Список сигналов, отсортированных по created_at DESC.
        :raises DatabaseError: при ошибках уровня БД.
        """
        pool = self._get_pool()

        conditions = []
        params: list[object] = []
        idx = 1

        if symbol is not None:
            conditions.append(f"symbol = ${idx}")
            params.append(symbol)
            idx += 1

        if since is not None:
            conditions.append(f"created_at >= ${idx}")
            params.append(since)
            idx += 1

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT *
            FROM signals
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ${idx}
        """

        params.append(limit)

        self._logger.debug(
            "Listing recent signals",
            symbol=symbol,
            since=since.isoformat() if since else None,
            limit=limit,
        )

        try:
            async with pool.acquire() as conn:
                records = await conn.fetch(query, *params)
        except asyncpg.PostgresError as exc:
            self._logger.exception(
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

        return [self._record_to_signal(record) for record in records]

    async def update_error(
        self,
        signal_id: UUID,
        *,
        error_code: Optional[int],
        error_message: Optional[str],
    ) -> Signal:
        """
        Обновить поля error_code и error_message для сигнала.

        Используется после обработки сигнала, когда нужно зафиксировать
        результат (например, отказ по риск-лимитам или ошибку выставления ордера).

        :param signal_id: Идентификатор сигнала.
        :param error_code: Код ошибки (или None для сброса).
        :param error_message: Текст ошибки (или None).
        :return: Обновлённый сигнал.
        :raises DatabaseError: если сигнал не найден или при ошибках БД.
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

        self._logger.info(
            "Updating signal error fields",
            signal_id=str(signal_id),
            error_code=error_code,
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
            self._logger.exception(
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
