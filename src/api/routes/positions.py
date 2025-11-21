from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.core.models import Position
from src.db.repositories.position_repository import PositionRepository

__all__ = ["router"]

router = APIRouter(prefix="/positions", tags=["positions"])


# --- DI-хелпер ------------------------------------------------------------- #


def get_position_repository() -> PositionRepository:
    """
    Простейший провайдер репозитория для FastAPI DI.

    Репозиторий сам тянет пул соединений через src.db.connection.get_pool(),
    поэтому здесь достаточно просто создавать инстанс.
    """
    return PositionRepository()


# --- Маршруты -------------------------------------------------------------- #


@router.get("/", response_model=List[Position])
async def list_open_positions(
    symbol: Optional[str] = Query(
        None,
        description="Опциональный фильтр по символу (инструменту).",
    ),
    repo: PositionRepository = Depends(get_position_repository),
) -> List[Position]:
    """
    Получить список всех открытых позиций.

    Согласно docs/api.md, `GET /positions` возвращает список открытых позиций
    текущего пользователя. Дополнительно поддерживаем фильтр по `symbol`
    (расширение по сравнению с минимальным контрактом).
    """
    positions = await repo.list_open(symbol=symbol)
    return positions


@router.post("/{position_id}/close", response_model=Position)
async def close_position(
    position_id: UUID,
    repo: PositionRepository = Depends(get_position_repository),
) -> Position:
    """
    Ручное закрытие позиции по её идентификатору.

    Поведение соответствует описанию `POST /positions/{id}/close`:

    * если позиция не найдена → 404;
    * если позиция уже закрыта (closed_at не NULL) → 409;
    * иначе помечаем её закрытой и возвращаем обновлённый объект Position.

    Фактическое исполнение на бирже в данном слое не моделируем —
    здесь только изменение статуса/метаданных в БД.
    """
    position = await repo.get_by_id(position_id)
    if position is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Position not found",
        )

    if position.closed_at is not None:
        # Позиция уже закрыта или находится в завершающемся состоянии —
        # для API это считается конфликтом бизнес-состояния.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Position is already closed",
        )

    closed_at = datetime.now(timezone.utc)
    updated = await repo.mark_closed(position_id=position_id, closed_at=closed_at)

    if updated is None:
        # Теоретическая гонка: позиция успела исчезнуть между get_by_id и mark_closed
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Position not found",
        )

    return updated


@router.get("/{position_id}", response_model=Position)
async def get_position(
    position_id: UUID,
    repo: PositionRepository = Depends(get_position_repository),
) -> Position:
    """
    Получить одну позицию по её идентификатору.

    Если позиция не найдена, возвращаем 404.
    """
    position = await repo.get_by_id(position_id)
    if position is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found")
    return position
