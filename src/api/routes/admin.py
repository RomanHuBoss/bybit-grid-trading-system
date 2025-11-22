from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import json

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from redis.asyncio import Redis

from src.monitoring.metrics import Metrics
from src.notifications.ui_notifier import UINotifier

__all__ = ["router"]

router = APIRouter(prefix="/admin", tags=["admin"])

# Ключи в Redis для хранения состояния kill-switch.
_KILL_SWITCH_STATE_KEY = "kill_switch:state"  # детальное состояние (JSON)
_KILL_SWITCH_ACTIVE_KEY = "kill_switch:active"  # простой флаг для потребителей/мониторинга


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
# Модели запросов/ответов
# --------------------------------------------------------------------------- #


class KillSwitchStatus(BaseModel):
    """
    Внутреннее представление состояния kill-switch.

    Используется как ответ для GET `/admin/kill_switch` и как основа
    для сериализации в Redis.
    """

    active: bool
    reason: Optional[str] = None
    updated_at: datetime


class KillSwitchRequest(BaseModel):
    """
    Тело запроса для POST `/admin/kill_switch`.

    По умолчанию (`active=True`) — включает kill-switch. При передаче
    `active=False` — выключает его. Поле `reason` используется только
    для UI/аудита и логов.

    При этом форма из документации:

        {"reason": "Manual kill switch due to abnormal behavior"}

    остаётся валидной, так как `active` имеет значение по умолчанию.
    """

    active: bool = True
    reason: Optional[str] = None


class KillSwitchResponse(BaseModel):
    """
    Формат ответа, выровненный с docs/api.md (§8.1).

    Пример из документации:

        {
          "kill_switch_active": true
        }
    """

    kill_switch_active: bool


# --------------------------------------------------------------------------- #
# Вспомогательные функции для работы с Redis
# --------------------------------------------------------------------------- #


async def _load_kill_switch_state(redis: Redis) -> KillSwitchStatus:
    """
    Загрузить текущее состояние kill-switch из Redis.

    Формат в Redis (ключ `_KILL_SWITCH_STATE_KEY`):
        {"active": bool, "reason": str | null, "updated_at": "<iso8601>"}

    При отсутствии ключа или некорректном содержимом считаем, что
    kill-switch выключен.
    """
    raw = await redis.get(_KILL_SWITCH_STATE_KEY)
    if raw is None:
        return KillSwitchStatus(
            active=False,
            reason=None,
            updated_at=datetime.now(timezone.utc),
        )

    try:
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


async def _persist_kill_switch_state(redis: Redis, state: KillSwitchStatus) -> None:
    """
    Сохранить состояние kill-switch в Redis.

    - В `_KILL_SWITCH_STATE_KEY` кладётся подробное состояние (JSON);
    - В `_KILL_SWITCH_ACTIVE_KEY` — простой флаг для потребителей
      (например, SignalEngine), как описано в project_overview.md.
    """
    # Подробное состояние для UI/отладки.
    await redis.set(
        _KILL_SWITCH_STATE_KEY,
        json.dumps(
            {
                "active": state.active,
                "reason": state.reason,
                "updated_at": state.updated_at.isoformat(),
            }
        ),
    )

    # Простой флаг, который удобно читать из других компонентов.
    await redis.set(_KILL_SWITCH_ACTIVE_KEY, "1" if state.active else "0")


# --------------------------------------------------------------------------- #
# Kill-switch API
# --------------------------------------------------------------------------- #


@router.get("/kill_switch", response_model=KillSwitchStatus)
async def get_kill_switch(
    redis: Redis = Depends(get_redis),
) -> KillSwitchStatus:
    """
    Получить текущее состояние kill-switch.

    Эндпоинт не описан явно в docs/api.md, но удобен для UI и отладки.
    Формат ответа совпадает с `KillSwitchStatus`.
    """
    return await _load_kill_switch_state(redis)


@router.post("/kill_switch", response_model=KillSwitchResponse)
async def set_kill_switch(
    payload: KillSwitchRequest,
    redis: Redis = Depends(get_redis),
    ui_notifier: UINotifier = Depends(get_ui_notifier),
    metrics: Metrics = Depends(get_metrics),
) -> KillSwitchResponse:
    """
    Включить или выключить kill-switch вручную.

    Документация (`docs/api.md`, §8.1) описывает кейс принудительного
    выключения торговли и возвращает только поле `kill_switch_active`.

    Здесь по умолчанию (`active=True`) включаем kill-switch, но допускаем
    явное выключение (`active=False`) тем же эндпоинтом.
    """
    now = datetime.now(timezone.utc)

    state = KillSwitchStatus(
        active=payload.active,
        reason=payload.reason,
        updated_at=now,
    )

    # Сохраняем состояние в Redis (детальное + простой флаг).
    await _persist_kill_switch_state(redis, state)

    # Уведомляем UI через UINotifier.
    await ui_notifier.notify_kill_switch(
        {
            "active": state.active,
            "reason": state.reason,
            "updated_at": state.updated_at.isoformat(),
        }
    )

    # Метрики можно использовать для учёта переключений (если понадобится).
    # Сейчас просто инициализируем синглтон, чтобы он зарегистрировал метрики.
    _ = metrics  # noqa: F841

    return KillSwitchResponse(kill_switch_active=state.active)
