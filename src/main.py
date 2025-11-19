# src/main.py
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI as FastAPIBase
from redis.asyncio import Redis
from starlette.datastructures import State

from src.api.routes.admin import router as admin_router
from src.api.routes.health import router as health_router
from src.api.routes.positions import router as positions_router
from src.api.routes.signals import router as signals_router
from src.core.config_loader import ConfigLoader
from src.core.logging_config import get_logger, setup_logging
from src.db.connection import close_pool, init_pool


class FastAPI(FastAPIBase):
    """Расширенный тип FastAPI с явно типизированным app.state для IDE/линтеров."""
    state: State


logger = get_logger("main")


def _resolve_db_dsn(config: Any) -> str:
    """
    Получить DSN для PostgreSQL из конфига или переменных окружения.

    Приоритет:
    1) config.db.dsn (pydantic-модель или dict)
    2) переменная окружения DB_DSN

    Если ничего не найдено — бросаем RuntimeError, чтобы приложение
    не стартовало в неконсистентной конфигурации.
    """
    db_section = getattr(config, "db", None)

    dsn: str | None = None

    if db_section is not None:
        # Поддерживаем и pydantic-модели, и dict-подобные структуры
        if hasattr(db_section, "dsn"):
            dsn = getattr(db_section, "dsn", None)
        elif isinstance(db_section, dict):
            dsn = db_section.get("dsn")  # type: ignore[assignment]

    if not dsn:
        dsn = os.getenv("DB_DSN")

    if not dsn:
        raise RuntimeError(
            "PostgreSQL DSN is not configured. "
            "Expected config.db.dsn or environment variable DB_DSN."
        )

    return dsn


def _resolve_redis_dsn(config: Any) -> str:
    """
    Получить DSN для Redis из конфига или переменных окружения.

    Приоритет:
    1) config.redis.dsn (pydantic-модель или dict, если такая секция есть)
    2) переменная окружения REDIS_DSN

    Если ничего не найдено — бросаем RuntimeError.
    """
    redis_section = getattr(config, "redis", None)

    dsn: str | None = None

    if redis_section is not None:
        if hasattr(redis_section, "dsn"):
            dsn = getattr(redis_section, "dsn", None)
        elif isinstance(redis_section, dict):
            dsn = redis_section.get("dsn")  # type: ignore[assignment]

    if not dsn:
        dsn = os.getenv("REDIS_DSN")

    if not dsn:
        raise RuntimeError(
            "Redis DSN is not configured. "
            "Expected config.redis.dsn or environment variable REDIS_DSN."
        )

    return dsn


def _create_config_loader() -> ConfigLoader:
    """
    Создать ConfigLoader с путём до YAML-конфига.

    Путь берётся из переменной окружения APP_CONFIG_PATH
    или по умолчанию `config/app.yml`.
    """
    config_path_str = os.getenv("APP_CONFIG_PATH", "config/app.yml")
    config_path = Path(config_path_str)
    return ConfigLoader(config_path=config_path)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """
    Жизненный цикл приложения FastAPI.

    На старте:
    - настраиваем логирование,
    - создаём загрузчик конфигурации и читаем AppConfig,
    - инициализируем пул PostgreSQL,
    - создаём подключение к Redis и кладём его в application.state.

    На остановке:
    - аккуратно закрываем пул PostgreSQL,
    - закрываем подключение к Redis.
    """
    # --- logging -------------------------------------------------------------
    log_level = os.getenv("LOG_LEVEL", "INFO")
    log_file = os.getenv("LOG_FILE", "logs/app.jsonl")
    setup_logging(log_level=log_level, log_file=log_file)
    logger.info("Logging initialized", log_level=log_level, log_file=log_file)

    # --- config --------------------------------------------------------------
    config_loader = _create_config_loader()
    config = config_loader.get_config()
    application.state.config_loader = config_loader
    application.state.config = config
    logger.info("Application config loaded")

    # --- PostgreSQL ----------------------------------------------------------
    db_dsn = _resolve_db_dsn(config)
    await init_pool(db_dsn)
    logger.info("PostgreSQL pool initialized")

    # --- Redis ---------------------------------------------------------------
    redis_dsn = _resolve_redis_dsn(config)
    redis = Redis.from_url(redis_dsn)
    # Храним Redis в состоянии приложения для DI-хелперов (см. admin.get_redis)
    application.state.redis = redis
    logger.info("Redis client initialized", redis_dsn=redis_dsn)

    try:
        yield
    finally:
        # --- shutdown: Redis -------------------------------------------------
        redis_obj: Redis | None = getattr(application.state, "redis", None)
        if redis_obj is not None:
            try:
                await redis_obj.close()
                logger.info("Redis client closed")
            except Exception:  # noqa: BLE001
                logger.exception("Error while closing Redis client")

        # --- shutdown: PostgreSQL -------------------------------------------
        try:
            await close_pool()
            logger.info("PostgreSQL pool closed")
        except RuntimeError:
            # Пул мог не успеть инициализироваться или уже быть закрыт
            logger.debug("PostgreSQL pool was not open on shutdown")
        except Exception:  # noqa: BLE001
            logger.exception("Error while closing PostgreSQL pool")


def create_app() -> FastAPI:
    """
    Фабрика FastAPI-приложения.

    - Подключает все публичные роутеры API.
    - Назначает lifespan-менеджер для управления ресурсами (БД, Redis, конфиг).
    """
    application = FastAPI(
        title="AVI-5 Algo-Grid API",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Роутеры прикладного API
    application.include_router(health_router)
    application.include_router(positions_router)
    application.include_router(signals_router)
    application.include_router(admin_router)

    return application


# Глобальный экземпляр для ASGI-сервера
app = create_app()


if __name__ == "__main__":
    # Локальный запуск через `python -m src.main`
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload_flag = os.getenv("UVICORN_RELOAD", "0") == "1"

    uvicorn.run(
        "src.main:app",
        host=host,
        port=port,
        reload=reload_flag,
    )
