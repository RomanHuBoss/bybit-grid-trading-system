from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

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


@router.get("/open", response_model=List[Position])
async def list_open_positions(
    symbol: Optional[str] = Query(
        None,
        description="Опциональный фильтр по символу (инструменту).",
    ),
    repo: PositionRepository = Depends(get_position_repository),
) -> List[Position]:
    """
    Получить список всех открытых позиций.

    Опционально можно отфильтровать по конкретному инструменту (symbol).
    """
    positions = await repo.list_open(symbol=symbol)
    return positions


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
        raise HTTPException(status_code=404, detail="Position not found")
    return position
