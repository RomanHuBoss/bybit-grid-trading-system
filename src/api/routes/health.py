from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

__all__ = ["router"]

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    ts: datetime


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    """
    Liveness-проверка.

    Ничего не проверяет, кроме факта, что процесс жив и может отвечать на HTTP.
    Подходит для Kubernetes livenessProbe / простых ping-check'ов.
    """
    return HealthResponse(status="ok", ts=datetime.now(timezone.utc))


@router.get("/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    """
    Readiness-проверка.

    В базовой версии совпадает с liveness и гарантирует только то, что
    сам HTTP-процесс работает. При необходимости сюда можно добавить
    проверки подключения к БД, Redis и внешним сервисам.
    """
    return HealthResponse(status="ok", ts=datetime.now(timezone.utc))
