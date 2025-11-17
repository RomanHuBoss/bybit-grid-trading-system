from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from src.core.exceptions import DatabaseError
from src.core.logging_config import get_logger
from src.core.models import Position
from src.db.connection import get_pool

__all__ = ["PositionRepository"]

logger = get_logger("db.repositories.position_repository")


class PositionRepository:
    """
    Репозиторий для работы с таблицей позиций.

    Инкапсулирует всю работу с БД по сущности Position:
    создание, обновление, получение по идентификатору и выборки
    наборов позиций по простым критериям.
    """

    async def create(self, position: Position) -> Position:
        """
        Создать новую позицию в БД.

        :param position: Модель Position с заполненными полями.
        :return: Созданная позиция (может отличаться, если в БД есть доп. поля/триггеры).
        :raises DatabaseError: при ошибках уровня БД.
        """
        pool = get_pool()

        sql = """
            INSERT INTO positions (
                id,
                signal_id,
                opened_at,
                closed_at,
                symbol,
                direction,
                entry_price,
                size_base,
                size_quote,
                fill_ratio,
                slippage,
                funding
            )
            VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11, $12
            )
            RETURNING *
        """

        values = (
            position.id,
            position.signal_id,
            position.opened_at,
            position.closed_at,
            position.symbol,
            position.direction,
            position.entry_price,
            position.size_base,
            position.size_quote,
            position.fill_ratio,
            position.slippage,
            position.funding,
        )

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, *values)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to create position",
                error=str(exc),
                position_id=str(position.id),
            )
            raise DatabaseError(
                "Failed to create position",
                details={"position_id": str(position.id)},
            ) from exc

        return self._row_to_position(row)

    async def update(self, position: Position) -> Position:
        """
        Обновить существующую позицию в БД по её id.

        Ожидается, что позиция уже существует.
        Обновляются все основные поля доменной модели Position.

        :param position: Модель Position с обновлёнными полями.
        :return: Актуальная версия позиции из БД.
        :raises DatabaseError: при ошибках уровня БД или если запись не найдена.
        """
        pool = get_pool()

        sql = """
            UPDATE positions
            SET
                signal_id = $2,
                opened_at = $3,
                closed_at = $4,
                symbol = $5,
                direction = $6,
                entry_price = $7,
                size_base = $8,
                size_quote = $9,
                fill_ratio = $10,
                slippage = $11,
                funding = $12
            WHERE id = $1
            RETURNING *
        """

        values = (
            position.id,
            position.signal_id,
            position.opened_at,
            position.closed_at,
            position.symbol,
            position.direction,
            position.entry_price,
            position.size_base,
            position.size_quote,
            position.fill_ratio,
            position.slippage,
            position.funding,
        )

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, *values)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to update position",
                error=str(exc),
                position_id=str(position.id),
            )
            raise DatabaseError(
                "Failed to update position",
                details={"position_id": str(position.id)},
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

        return self._row_to_position(row)

    async def get_by_id(self, position_id: UUID) -> Optional[Position]:
        """
        Получить позицию по её идентификатору.

        :param position_id: UUID позиции.
        :return: Модель Position или None, если не найдена.
        :raises DatabaseError: при ошибках уровня БД.
        """
        pool = get_pool()

        sql = "SELECT * FROM positions WHERE id = $1"

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, position_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to fetch position by id",
                error=str(exc),
                position_id=str(position_id),
            )
            raise DatabaseError(
                "Failed to fetch position by id",
                details={"position_id": str(position_id)},
            ) from exc

        if row is None:
            return None

        return self._row_to_position(row)

    async def list_open(self, symbol: Optional[str] = None) -> List[Position]:
        """
        Вернуть список всех открытых позиций (closed_at IS NULL).

        :param symbol: Необязательный фильтр по инструменту.
        :return: Список моделей Position.
        :raises DatabaseError: при ошибках уровня БД.
        """
        pool = get_pool()

        if symbol is None:
            sql = "SELECT * FROM positions WHERE closed_at IS NULL"
            args: tuple[object, ...] = ()
        else:
            sql = "SELECT * FROM positions WHERE closed_at IS NULL AND symbol = $1"
            args = (symbol,)

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to list open positions",
                error=str(exc),
                symbol=symbol,
            )
            raise DatabaseError(
                "Failed to list open positions",
                details={"symbol": symbol},
            ) from exc

        return [self._row_to_position(row) for row in rows]

    async def list_by_signal(self, signal_id: UUID) -> List[Position]:
        """
        Вернуть список позиций, связанных с указанным сигналом.

        :param signal_id: UUID сигнала.
        :return: Список моделей Position (может быть пустым).
        :raises DatabaseError: при ошибках уровня БД.
        """
        pool = get_pool()

        sql = "SELECT * FROM positions WHERE signal_id = $1"

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, signal_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to list positions by signal_id",
                error=str(exc),
                signal_id=str(signal_id),
            )
            raise DatabaseError(
                "Failed to list positions by signal_id",
                details={"signal_id": str(signal_id)},
            ) from exc

        return [self._row_to_position(row) for row in rows]

    async def mark_closed(
        self,
        position_id: UUID,
        closed_at: datetime,
    ) -> Optional[Position]:
        """
        Пометить позицию как закрытую (установить closed_at).

        :param position_id: UUID позиции.
        :param closed_at: Время закрытия в UTC.
        :return: Обновлённая позиция или None, если запись не найдена.
        :raises DatabaseError: при ошибках уровня БД.
        """
        pool = get_pool()

        sql = """
            UPDATE positions
            SET closed_at = $2
            WHERE id = $1
            RETURNING *
        """

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, position_id, closed_at)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to mark position as closed",
                error=str(exc),
                position_id=str(position_id),
            )
            raise DatabaseError(
                "Failed to mark position as closed",
                details={"position_id": str(position_id)},
            ) from exc

        if row is None:
            return None

        return self._row_to_position(row)

    @staticmethod
    def _row_to_position(row) -> Position:
        """
        Преобразовать запись БД в доменную модель Position.
        """
        data = dict(row)
        return Position(**data)
