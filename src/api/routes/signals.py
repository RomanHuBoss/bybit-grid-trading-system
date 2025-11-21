from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Literal
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
    symbol: Optional[str] = Query(
        default=None,
        description="Опциональный фильтр по тикеру, например BTCUSDT.",
    ),
    direction: Optional[Literal["long", "short"]] = Query(
        default=None,
        description="Фильтр по направлению сигнала: long или short.",
    ),
    min_probability: Optional[float] = Query(
        default=None,
        ge=0.0,
        le=1.0,
        description="Минимальная вероятность (p_win) в диапазоне [0, 1].",
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Максимальное количество последних сигналов.",
    ),
    since: Optional[datetime] = Query(
        default=None,
        description="Необязательный фильтр по created_at >= since (UTC).",
    ),
    repo: SignalRepository = Depends(get_signal_repository),
) -> List[Signal]:
    """
    Получить список активных сигналов стратегии AVI-5.

    Соответствует описанию `GET /signals` в docs/api.md:

    * поддерживает фильтры `symbol`, `direction`, `min_probability`;
    * параметр `limit` ограничивает число возвращаемых записей;
    * параметр `since` позволяет запрашивать сигналы не старше заданного момента.
    """
    # Базовая выборка из БД (ограничение по времени и символу на уровне SQL).
    signals = await repo.list_recent(limit=limit, symbol=symbol, since=since)

    # Дополнительная фильтрация по направлению, если указано.
    if direction is not None:
        signals = [s for s in signals if s.direction == direction]

    # Фильтрация по минимальной вероятности p_win.
    if min_probability is not None:
        # probability у Signal — Decimal; сравнение через float
        # для диапазона [0, 1] достаточно точно.
        signals = [s for s in signals if float(s.probability) >= min_probability]

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
