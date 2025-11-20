# src/main.py
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI as FastAPIBase
from redis.asyncio import Redis
from starlette.datastructures import State

from src.api.routes.admin import router as admin_router
from src.api.routes.health import router as health_router
from src.api.routes.positions import router as positions_router
from src.api.routes.signals import router as signals_router
from src.api.routes.stream import router as stream_router
from src.core.config_loader import ConfigLoader
from src.core.logging_config import get_logger, setup_logging
from src.core.models import AppConfig
from src.db.connection import close_pool, init_pool


class FastAPI(FastAPIBase):
    """
    Расширенный тип FastAPI с явно типизированным app.state
    для IDE/линтеров. Типизированное состояние особенно полезно,
    т.к. мы складываем туда config, config_loader и Redis-клиент.
    """
    state: State


logger = get_logger("main")


def _resolve_db_dsn(config: AppConfig) -> str:
    """
    Получить DSN для PostgreSQL из конфига или переменных окружения.

    Приоритет источников (см. docs/deployment.md):
    1) config.db.dsn (pydantic-модель или dict);
    2) переменная окружения DATABASE_URL (основной контракт);
    3) переменная окружения DB_DSN (legacy-алиас для обратной совместимости).

    Если ничего не найдено — бросаем RuntimeError, чтобы приложение
    не стартовало в неконсистентной конфигурации.
    """
    db_section: Any = getattr(config, "db", None)

    dsn: Optional[str] = None

    if db_section is not None:
        # Поддерживаем и pydantic-модели, и dict-подобные структуры
        if hasattr(db_section, "dsn"):
            dsn = getattr(db_section, "dsn", None)
        elif isinstance(db_section, dict):
            dsn = db_section.get("dsn")  # type: ignore[assignment]

    if not dsn:
        # Основной путь из документации — DATABASE_URL
        dsn = os.getenv("DATABASE_URL") or os.getenv("DB_DSN")

    if not dsn:
        raise RuntimeError(
            "PostgreSQL DSN is not configured. "
            "Expected config.db.dsn or environment variable DATABASE_URL/DB_DSN."
        )

    return dsn


def _resolve_redis_dsn(config: AppConfig) -> str:
    """
    Получить DSN для Redis из конфига или переменных окружения.

    Приоритет источников (см. docs/deployment.md):
    1) config.redis.dsn (pydantic-модель или dict, если такая секция есть);
    2) переменная окружения REDIS_URL (основной контракт);
    3) переменная окружения REDIS_DSN (legacy-алиас).

    Если ничего не найдено — бросаем RuntimeError.
    """
    redis_section: Any = getattr(config, "redis", None)

    dsn: Optional[str] = None

    if redis_section is not None:
        if hasattr(redis_section, "dsn"):
            dsn = getattr(redis_section, "dsn", None)
        elif isinstance(redis_section, dict):
            dsn = redis_section.get("dsn")  # type: ignore[assignment]

    if not dsn:
        dsn = os.getenv("REDIS_URL") or os.getenv("REDIS_DSN")

    if not dsn:
        raise RuntimeError(
            "Redis DSN is not configured. "
            "Expected config.redis.dsn or environment variable REDIS_URL/REDIS_DSN."
        )

    return dsn


def _create_config_loader(config_path: str | Path | None = None) -> ConfigLoader:
    """
    Создать ConfigLoader с путём до YAML-конфига.

    Приоритет выбора пути (см. project_overview.md и docs/api.md):
    1) явный аргумент `config_path` (см. create_app);
    2) переменная окружения APP_CONFIG_PATH;
    3) дефолт `config/settings.yaml` (контракт проекта).

    Таким образом код согласован с описанием create_app(...) в документации
    и с самим ConfigLoader, у которого такой же дефолт.
    """
    if config_path is not None:
        resolved = Path(config_path)
    else:
        env_path = os.getenv("APP_CONFIG_PATH")
        if env_path:
            resolved = Path(env_path)
        else:
            resolved = Path("config/settings.yaml")

    return ConfigLoader(config_path=resolved)


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
    # create_app может передать явный путь до settings.yaml через state
    state_config_path: Path | None = getattr(application.state, "config_path", None)
    config_loader = _create_config_loader(config_path=state_config_path)
    config = config_loader.get_config()

    application.state.config_loader = config_loader
    application.state.config = config
    logger.info("Application config loaded")

    # --- PostgreSQL ----------------------------------------------------------
    db_dsn = _resolve_db_dsn(config)
    await init_pool(db_dsn)
    logger.info("PostgreSQL pool initialized", extra={"db_dsn": db_dsn})

    # --- Redis ---------------------------------------------------------------
    redis_dsn = _resolve_redis_dsn(config)
    redis = Redis.from_url(redis_dsn)
    # Храним Redis в состоянии приложения для DI-хелперов (см. admin.get_redis, stream router)
    application.state.redis = redis
    logger.info("Redis client initialized", extra={"redis_dsn": redis_dsn})

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
                # На shutdown важнее не уронить приложение ещё раз
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


def create_app(config_path: str | Path | None = None) -> FastAPI:
    """
    Фабрика FastAPI-приложения.

    Args:
        config_path:
            Необязательный путь до файла конфигурации (обычно `config/settings.yaml`).
            Если не задан, используется APP_CONFIG_PATH или дефолт
            `config/settings.yaml`, как описано в project_overview.md.

    - Подключает все публичные роутеры API.
    - Назначает lifespan-менеджер для управления ресурсами (БД, Redis, конфиг).
    """
    application = FastAPI(
        title="AVI-5 Algo-Grid API",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Сохраняем путь до конфига в state, чтобы lifespan мог им воспользоваться
    if config_path is not None:
        application.state.config_path = Path(config_path)

    # Роутеры прикладного API
    application.include_router(health_router)
    application.include_router(positions_router)
    application.include_router(signals_router)
    application.include_router(admin_router)
    # SSE-стрим для UI / мониторинга (см. docs/api.md, раздел Streaming)
    application.include_router(stream_router)

    return application


# Готовый экземпляр приложения для uvicorn / gunicorn / тестов
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
