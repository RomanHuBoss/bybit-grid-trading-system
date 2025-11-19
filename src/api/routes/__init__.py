from __future__ import annotations

from fastapi import FastAPI

from src.api.routes.health import router as health_router
from src.api.routes.signals import router as signals_router
from src.api.routes.positions import router as positions_router
from src.api.routes.admin import router as admin_router

__all__ = ["register_routes"]


def register_routes(app: FastAPI) -> None:
    """
    Подключить все HTTP-роутеры к приложению FastAPI.

    Здесь собираются в единый слой:
    - технические эндпоинты (health);
    - пользовательские эндпоинты (signals, positions);
    - административные эндпоинты (admin).

    Логика маршрутизации не содержит бизнес-правил — только регистрирует
    роутеры, реализованные в соответствующих модулях.
    """

    app.include_router(health_router)
    app.include_router(signals_router)
    app.include_router(positions_router)
    app.include_router(admin_router)
