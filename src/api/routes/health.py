from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Awaitable, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from redis.asyncio import Redis

from src.db.connection import get_pool

__all__ = ["router"]

router = APIRouter(prefix="/health", tags=["health"])


class HealthComponents(BaseModel):
    db: Literal["up", "down", "degraded"]
    redis: Literal["up", "down", "degraded"]
    bybit_ws: Literal["up", "down", "degraded"]
    bybit_rest: Literal["up", "down", "degraded"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    components: HealthComponents


class LiveResponse(BaseModel):
    status: Literal["alive"]
    ts: datetime


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]
    ts: datetime


async def _get_redis(request: Request) -> Redis | None:
    redis = getattr(request.app.state, "redis", None)
    if redis is None or not isinstance(redis, Redis):
        return None
    return redis


async def _check_db() -> Literal["up", "down"]:
    try:
        pool = get_pool()
    except Exception:  # noqa: BLE001
        return "down"

    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception:  # noqa: BLE001
        return "down"

    return "up"


async def _check_redis(redis: Redis | None) -> Literal["up", "down"]:
    """
    Проверка доступности Redis через команду PING.

    FIX for mypy:
    redis.ping() имеет тип Awaitable[bool] | bool.
    Мы явно приводим его к Awaitable[bool], чтобы mypy не ругался.
    """
    if redis is None:
        return "down"

    try:
        ping_call: Awaitable[bool] = cast(Awaitable[bool], redis.ping())  # <-- FIX
        ok = await ping_call
    except Exception:  # noqa: BLE001
        return "down"

    return "up" if ok else "down"


def _check_bybit_ws() -> Literal["up", "degraded"]:
    return "up"


def _check_bybit_rest() -> Literal["up", "degraded"]:
    return "up"


@router.get("", response_model=HealthResponse)
@router.get("/", response_model=HealthResponse, include_in_schema=False)
async def health(redis: Redis | None = Depends(_get_redis)) -> HealthResponse:
    db_status = await _check_db()
    redis_status = await _check_redis(redis)
    bybit_ws_status = _check_bybit_ws()
    bybit_rest_status = _check_bybit_rest()

    components = HealthComponents(
        db=db_status,
        redis=redis_status,
        bybit_ws=bybit_ws_status,
        bybit_rest=bybit_rest_status,
    )

    critical_down = db_status == "down" or redis_status == "down"

    if critical_down:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "degraded",
                "components": components.model_dump(),
            },
        )

    overall_status: Literal["ok", "degraded"]
    if (
        bybit_ws_status == "degraded"
        or bybit_rest_status == "degraded"
    ):
        overall_status = "degraded"
    else:
        overall_status = "ok"

    return HealthResponse(status=overall_status, components=components)


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(status="alive", ts=datetime.now(timezone.utc))


@router.get("/ready", response_model=ReadyResponse)
async def ready(redis: Redis | None = Depends(_get_redis)) -> ReadyResponse:
    db_ok = (await _check_db()) == "up"
    redis_ok = (await _check_redis(redis)) == "up"

    checks = {
        "redis": redis_ok,
        "db": db_ok,
    }

    all_ok = db_ok and redis_ok
    status_value: Literal["ready", "not_ready"] = "ready" if all_ok else "not_ready"

    if not all_ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": status_value,
                "checks": checks,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )

    return ReadyResponse(
        status=status_value,
        checks=checks,
        ts=datetime.now(timezone.utc),
    )
