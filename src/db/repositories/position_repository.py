from __future__ import annotations

from datetime import datetime
from typing import Any, List, Mapping, Optional, cast
from uuid import UUID

import asyncpg
from pydantic import ValidationError

from src.core.exceptions import DatabaseError
from src.core.logging_config import get_logger
from src.core.models import Position
from src.db.connection import get_pool

__all__ = ["PositionRepository"]

logger = get_logger("db.repositories.position_repository")


class PositionRepository:
    """
    Репозиторий для работы с таблицей `positions`.

    Логика согласована со схемой из docs/database_schema.md:

    - в БД хранится поле `side`, в доменной модели и API — `direction`;
    - "открытые" позиции определяются через статус `status = 'open'`;
    - часть технических полей (`pnl_usd`, `slippage_entry_bps`,
      `slippage_exit_bps`, `executed_size_base` и т.п.) может не
      отображаться в модель Position и остаётся внутри БД.
    """

    # ---------- Внутренние помощники ----------

    def _get_pool(self) -> asyncpg.Pool:
        """
        Получить пул соединений к PostgreSQL.

        Оборачивает отсутствие пула в DatabaseError, чтобы уровень API
        не зависел напрямую от деталей asyncpg.
        """
        try:
            return get_pool()
        except RuntimeError as exc:
            logger.error("PostgreSQL pool is not available", error=str(exc))
            raise DatabaseError(
                "PostgreSQL pool is not available",
                details={"error": str(exc)},
            ) from exc

    @staticmethod
    def _record_to_position(record: asyncpg.Record) -> Position:
        """
        Преобразовать запись БД (asyncpg.Record) в доменную модель Position.

        Так как схема БД и модель расходятся по части имён полей
        (`side` vs `direction`), выполняем явное отображение и
        фильтруем только известные полям модели ключи.
        """
        raw: dict[str, Any] = dict(record)

        # Явное отображение полей БД -> полей модели
        field_mapping: dict[str, str] = {
            "side": "direction",
        }

        # pydantic v2: model_fields — словарь FieldInfo
        fields_mapping = cast(Mapping[str, Any], Position.model_fields)
        allowed_fields = set(fields_mapping.keys())

        data: dict[str, Any] = {}
        for db_field, value in raw.items():
            model_field = field_mapping.get(db_field, db_field)
            if model_field in allowed_fields:
                data[model_field] = value

        try:
            return Position(**data)
        except ValidationError as exc:
            raise DatabaseError(
                "Failed to hydrate Position from database record",
                details={"errors": exc.errors()},
            ) from exc

    # ---------- Публичный API ----------

    async def create(self, position: Position) -> Position:
        """
        Создать новую позицию в БД.

        Поля, которые записываются:

        - id, signal_id, symbol, entry_price, size_base, size_quote;
        - direction → side;
        - fill_ratio;
        - status: на момент создания — всегда 'open';
        - opened_at, closed_at.

        Остальные поля схемы (`tp*_price`, `sl_price`, `pnl_usd`,
        `slippage_entry_bps`, `slippage_exit_bps`, `executed_size_base`)
        остаются NULL/DEFAULT и могут заполняться иными компонентами.
        """
        pool = self._get_pool()

        sql = """
            INSERT INTO positions (
                id,
                signal_id,
                symbol,
                side,
                entry_price,
                size_base,
                size_quote,
                fill_ratio,
                status,
                opened_at,
                closed_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11
            )
            RETURNING *
        """

        values = (
            position.id,
            position.signal_id,
            position.symbol,
            position.direction,   # side
            position.entry_price,
            position.size_base,
            position.size_quote,
            position.fill_ratio,
            "open",               # новая позиция всегда в статусе open
            position.opened_at,
            position.closed_at,
        )

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, *values)
        except asyncpg.PostgresError as exc:
            logger.error(
                "Failed to create position",
                error=str(exc),
                position_id=str(position.id),
            )
            raise DatabaseError(
                "Failed to create position",
                details={"position_id": str(position.id), "error": str(exc)},
            ) from exc

        if row is None:
            raise DatabaseError(
                "INSERT INTO positions returned no row",
                details={"position_id": str(position.id)},
            )

        return self._record_to_position(row)

    async def update(self, position: Position) -> Position:
        """
        Обновить существующую позицию в БД по её id.

        Обновляются основные поля доменной модели Position.
        Поле `status` управляется отдельно (например, через mark_closed).

        :raises DatabaseError: при ошибках уровня БД или если запись не найдена.
        """
        pool = self._get_pool()

        sql = """
            UPDATE positions
            SET
                signal_id   = $2,
                symbol      = $3,
                side        = $4,
                entry_price = $5,
                size_base   = $6,
                size_quote  = $7,
                fill_ratio  = $8,
                opened_at   = $9,
                closed_at   = $10
            WHERE id = $1
            RETURNING *
        """

        values = (
            position.id,
            position.signal_id,
            position.symbol,
            position.direction,   # side
            position.entry_price,
            position.size_base,
            position.size_quote,
            position.fill_ratio,
            position.opened_at,
            position.closed_at,
        )

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, *values)
        except asyncpg.PostgresError as exc:
            logger.error(
                "Failed to update position",
                error=str(exc),
                position_id=str(position.id),
            )
            raise DatabaseError(
                "Failed to update position",
                details={"position_id": str(position.id), "error": str(exc)},
            ) from exc

        if row is None:
            logger.error(
                "Position not found for update",
                position_id=str(position.id),
            )
            raise DatabaseError(
                "Position not found for update",
                details={"position_id": str(position.id)},
            )

        return self._record_to_position(row)

    async def get_by_id(self, position_id: UUID) -> Optional[Position]:
        """
        Получить позицию по её идентификатору.

        :return: Position или None, если не найдена.
        """
        pool = self._get_pool()

        sql = "SELECT * FROM positions WHERE id = $1"

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, position_id)
        except asyncpg.PostgresError as exc:
            logger.error(
                "Failed to fetch position by id",
                error=str(exc),
                position_id=str(position_id),
            )
            raise DatabaseError(
                "Failed to fetch position by id",
                details={"position_id": str(position_id), "error": str(exc)},
            ) from exc

        if row is None:
            return None

        return self._record_to_position(row)

    async def list_open(self, symbol: Optional[str] = None) -> List[Position]:
        """
        Вернуть список всех открытых позиций.

        По ТЗ "открытая" позиция — это позиция со статусом `open`.
        Для соответствия схеме и индексу `idx_positions_symbol_status`
        используем именно поле `status`, а не только `closed_at`.

        :param symbol: Необязательный фильтр по инструменту.
        """
        pool = self._get_pool()

        if symbol is None:
            sql = """
                SELECT *
                FROM positions
                WHERE status = 'open'
            """
            args: tuple[object, ...] = ()
        else:
            sql = """
                SELECT *
                FROM positions
                WHERE status = 'open'
                  AND symbol = $1
            """
            args = (symbol,)

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
        except asyncpg.PostgresError as exc:
            logger.error(
                "Failed to list open positions",
                error=str(exc),
                symbol=symbol,
            )
            raise DatabaseError(
                "Failed to list open positions",
                details={"symbol": symbol, "error": str(exc)},
            ) from exc

        return [self._record_to_position(row) for row in rows]

    async def list_by_signal(self, signal_id: UUID) -> List[Position]:
        """
        Вернуть все позиции, связанные с указанным сигналом.
        """
        pool = self._get_pool()

        sql = "SELECT * FROM positions WHERE signal_id = $1"

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, signal_id)
        except asyncpg.PostgresError as exc:
            logger.error(
                "Failed to list positions by signal_id",
                error=str(exc),
                signal_id=str(signal_id),
            )
            raise DatabaseError(
                "Failed to list positions by signal_id",
                details={"signal_id": str(signal_id), "error": str(exc)},
            ) from exc

        return [self._record_to_position(row) for row in rows]

    async def mark_closed(
        self,
        position_id: UUID,
        closed_at: datetime,
    ) -> Optional[Position]:
        """
        Пометить позицию как закрытую.

        Для соответствия схеме не только устанавливаем `closed_at`,
        но и обновляем `status` → 'closed'.
        """
        pool = self._get_pool()

        sql = """
            UPDATE positions
            SET
                status    = 'closed',
                closed_at = $2
            WHERE id = $1
            RETURNING *
        """

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, position_id, closed_at)
        except asyncpg.PostgresError as exc:
            logger.error(
                "Failed to mark position as closed",
                error=str(exc),
                position_id=str(position_id),
            )
            raise DatabaseError(
                "Failed to mark position as closed",
                details={"position_id": str(position_id), "error": str(exc)},
            ) from exc

        if row is None:
            return None

        return self._record_to_position(row)
