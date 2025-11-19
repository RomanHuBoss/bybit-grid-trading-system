from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from src.core.models import Signal
from src.db.repositories.signal_repository import SignalRepository

__all__ = ["router"]

router = APIRouter(prefix="/signals", tags=["signals"])


# --- DI-хелпер ------------------------------------------------------------- #


def get_signal_repository() -> SignalRepository:
    """
    Простейший провайдер репозитория для FastAPI DI.

    Репозиторий сам тянет пул соединений через src.db.connection.get_pool(),
    поэтому здесь достаточно просто создавать инстанс.
    """
    return SignalRepository()


# --- Маршруты -------------------------------------------------------------- #


@router.get("/", response_model=List[Signal])
async def list_signals(
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="Максимальное количество последних сигналов.",
    ),
    symbol: Optional[str] = Query(
        None,
        description="Опциональный фильтр по символу.",
    ),
    since: Optional[datetime] = Query(
        None,
        description="Опциональный фильтр по created_at >= since (UTC).",
    ),
    repo: SignalRepository = Depends(get_signal_repository),
) -> List[Signal]:
    """
    Получить список последних сигналов AVI-5.

    Используется UI/аналитикой для просмотра того, что сейчас генерирует стратегия.
    """
    signals = await repo.list_recent(limit=limit, symbol=symbol, since=since)
    return signals


@router.get("/{signal_id}", response_model=Signal)
async def get_signal(
    signal_id: UUID,
    repo: SignalRepository = Depends(get_signal_repository),
) -> Signal:
    """
    Получить один сигнал по ID.

    Если сигнал не найден, возвращаем 404.
    """
    signal = await repo.get_by_id(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal
