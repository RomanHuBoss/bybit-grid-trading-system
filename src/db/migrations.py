from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from alembic.command import upgrade
from alembic.config import Config
from alembic.script import ScriptDirectory

from src.core.logging_config import get_logger

logger = get_logger("db.migrations")

DEFAULT_ALEMBIC_INI = "alembic.ini"


async def run_migrations(alembic_ini_path: str = DEFAULT_ALEMBIC_INI) -> None:
    """
    Запустить Alembic-миграции до ревизии head.

    Функция предназначена для вызова при старте приложения (например, в FastAPI lifespan).
    Выполняется как coroutine, но внутри реальный вызов Alembic производится в отдельном
    потоке, чтобы не блокировать event loop.

    :param alembic_ini_path: Путь к конфигурационному файлу alembic.ini.
    :raises FileNotFoundError: если alembic.ini не найден.
    :raises Exception: любые ошибки Alembic пробрасываются дальше (после логирования).
    """
    ini_path = Path(alembic_ini_path)

    if not ini_path.exists():
        raise FileNotFoundError(f"Alembic config not found: {ini_path}")

    config = Config(str(ini_path))

    logger.info("Running Alembic migrations to head", alembic_ini=str(ini_path))

    loop = asyncio.get_running_loop()

    # Alembic API синхронный, поэтому отправляем вызов в executor,
    # чтобы не блокировать event loop.
    def _upgrade() -> None:
        upgrade(config, "head")

    try:
        await loop.run_in_executor(None, _upgrade)
    except Exception:
        logger.exception("Alembic migrations failed", alembic_ini=str(ini_path))
        raise

    logger.info("Alembic migrations applied successfully", alembic_ini=str(ini_path))


def get_current_revision(alembic_ini_path: str = DEFAULT_ALEMBIC_INI) -> Optional[str]:
    """
    Получить текущую целевую ревизию миграций (head), описанную в alembic.ini.

    В контексте этого модуля под «текущей ревизией» понимается head репозитория миграций.
    Проверка фактического состояния БД относительно head остаётся на вызывающей стороне.

    :param alembic_ini_path: Путь к конфигурационному файлу alembic.ini.
    :return: Строковый идентификатор ревизии или None, если репозиторий миграций пуст.
    :raises FileNotFoundError: если alembic.ini не найден.
    """
    ini_path = Path(alembic_ini_path)

    if not ini_path.exists():
        raise FileNotFoundError(f"Alembic config not found: {ini_path}")

    config = Config(str(ini_path))
    script = ScriptDirectory.from_config(config)

    current_head = script.get_current_head()
    logger.info(
        "Fetched current Alembic head revision",
        alembic_ini=str(ini_path),
        revision=current_head,
    )

    return current_head
