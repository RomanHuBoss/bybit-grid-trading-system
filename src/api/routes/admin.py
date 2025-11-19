from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from redis.asyncio import Redis

from src.monitoring.metrics import Metrics
from src.notifications.ui_notifier import UINotifier

__all__ = ["router"]

router = APIRouter(prefix="/admin", tags=["admin"])

_KILL_SWITCH_KEY = "kill_switch:state"


# --------------------------------------------------------------------------- #
# DI-хелперы
# --------------------------------------------------------------------------- #


async def get_redis(request: Request) -> Redis:
    """
    Получить Redis из состояния приложения.

    Ожидается, что в фабрике FastAPI-приложения будет настроено:
        app.state.redis = Redis(...)

    Если этого нет — это ошибка конфигурации приложения.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise RuntimeError("app.state.redis is not configured")
    return redis


def get_ui_notifier(redis: Redis = Depends(get_redis)) -> UINotifier:
    """
    Провайдер UINotifier для DI.

    Создаёт лёгкий обёрточный инстанс поверх Redis; состояние в нём не хранится,
    поэтому создание на каждый запрос допустимо.
    """
    return UINotifier(redis)


def get_metrics() -> Metrics:
    """
    Провайдер Metrics-синглтона.

    Используется для регистрации админских событий в метриках (при необходимости).
    """
    return Metrics()


# --------------------------------------------------------------------------- #
# Модели ответов/запросов
# --------------------------------------------------------------------------- #


class KillSwitchStatus(BaseModel):
    active: bool
    reason: Optional[str] = None
    updated_at: datetime


class KillSwitchUpdateRequest(BaseModel):
    active: bool
    reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Kill-switch
# --------------------------------------------------------------------------- #


@router.get("/kill-switch", response_model=KillSwitchStatus)
async def get_kill_switch(
    redis: Redis = Depends(get_redis),
) -> KillSwitchStatus:
    """
    Получить текущее состояние kill-switch.

    Kill-switch хранится в Redis в ключе `_KILL_SWITCH_KEY` в виде JSON:
        {"active": bool, "reason": str | null, "updated_at": "<iso8601>"}

    Если ключа нет — считаем, что kill-switch выключен (active=False).
    """
    raw = await redis.get(_KILL_SWITCH_KEY)
    if raw is None:
        return KillSwitchStatus(
            active=False,
            reason=None,
            updated_at=datetime.now(timezone.utc),
        )

    try:
        import json

        data = json.loads(raw)
        active = bool(data.get("active", False))
        reason = data.get("reason")
        updated_raw = data.get("updated_at")
        if updated_raw is not None:
            updated_at = datetime.fromisoformat(str(updated_raw))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
        else:
            updated_at = datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001
        # Если в Redis лежит мусор — считаем, что kill-switch выключен.
        return KillSwitchStatus(
            active=False,
            reason=None,
            updated_at=datetime.now(timezone.utc),
        )

    return KillSwitchStatus(active=active, reason=reason, updated_at=updated_at)


@router.put("/kill-switch", response_model=KillSwitchStatus)
async def set_kill_switch(
    payload: KillSwitchUpdateRequest,
    redis: Redis = Depends(get_redis),
    ui_notifier: UINotifier = Depends(get_ui_notifier),
    metrics: Metrics = Depends(get_metrics),
) -> KillSwitchStatus:
    """
    Установить состояние kill-switch.

    - active=True  → включить kill-switch (новые позиции не открываем);
    - active=False → выключить kill-switch (разрешить открытие новых позиций).

    Причина (reason) опциональна и используется только для UI/аудита.
    """
    now = datetime.now(timezone.utc)

    state = KillSwitchStatus(
        active=payload.active,
        reason=payload.reason,
        updated_at=now,
    )

    # Сохраняем состояние в Redis как JSON.
    import json

    await redis.set(
        _KILL_SWITCH_KEY,
        json.dumps(
            {
                "active": state.active,
                "reason": state.reason,
                "updated_at": state.updated_at.isoformat(),
            }
        ),
    )

    # Уведомляем UI через UINotifier.
    await ui_notifier.publish_kill_switch(
        active=state.active,
        reason=state.reason,
    )

    # Метрики можно использовать для учёта переключений (если понадобится).
    # Сейчас просто инициализируем синглтон, чтобы он зарегистрировал метрики.
    _ = metrics  # noqa: F841

    return state
