from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Optional

from src.db.connection import close_pool, init_pool
from redis.asyncio import Redis

from src.core.config_loader import ConfigLoader
from src.core.models import AppConfig
from src.strategies.calibration import CalibrationService
from src.db.repositories.signal_repository import SignalRepository  # type: ignore[import]


logger = logging.getLogger("scripts.run_calibration")


# ---------------------------------------------------------------------------
# Вспомогательные функции для чтения конфига / DSN / Redis
# ---------------------------------------------------------------------------


def _resolve_db_dsn(config: AppConfig) -> str:
    """
    Получить DSN для PostgreSQL из конфига или переменных окружения.

    Приоритет источников (см. docs/deployment.md):
      1) config.db.dsn (pydantic-модель или dict);
      2) переменная окружения DATABASE_URL (основной контракт);
      3) переменная окружения DB_DSN (legacy-алиас).

    Если ничего не найдено — бросаем RuntimeError, чтобы джоба
    не запускалась в неконсистентной конфигурации.
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
        # Основной путь из документации — DATABASE_URL, DB_DSN как алиас
        dsn = os.getenv("DATABASE_URL") or os.getenv("DB_DSN")

    if not dsn:
        raise RuntimeError(
            "PostgreSQL DSN is not configured. "
            "Expected config.db.dsn or environment variable DATABASE_URL/DB_DSN."
        )

    return str(dsn)


def _resolve_redis_dsn(config: AppConfig) -> str:
    """
    Получить DSN/URL для Redis из конфига или переменных окружения.

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

    return str(dsn)


def _create_signal_repository() -> SignalRepository:
    """
    Создать экземпляр SignalRepository.

    Репозиторий использует глобальный пул соединений через
    src.db.connection.get_pool(), поэтому в рамках этой джобы ему
    не нужно явно передавать пул.
    """
    return SignalRepository()


# ---------------------------------------------------------------------------
# Основная асинхронная логика
# ---------------------------------------------------------------------------


async def _run_calibration(symbol: Optional[str], force: bool) -> None:
    """
    Запуск offline-калибровки AVI-5.

    Поведение:
      1) Загружаем AppConfig через ConfigLoader.
      2) Инициализируем глобальный пул PostgreSQL через init_pool(...) и
         создаём Redis-клиент.
      3) Собираем CalibrationService.
      4) Если не указан --force:
           - пробуем посчитать PSI-дрифт;
           - если baseline есть и дрифт в норме — калибровку пропускаем.
      5) Иначе (или при проблемах с PSI) запускаем calibrate() и логируем карту theta(h).
    """
    # Уважаем ту же контрактность выбора конфига, что и основное приложение:
    # APP_CONFIG_PATH позволяет переопределить путь до settings.yaml.
    config_path = os.getenv("APP_CONFIG_PATH")
    if config_path:
        loader = ConfigLoader(config_path=config_path)
    else:
        loader = ConfigLoader()

    config = loader.get_config()

    db_dsn = _resolve_db_dsn(config)
    redis_dsn = _resolve_redis_dsn(config)

    logger.info("Starting calibration job", extra={"symbol": symbol, "force": force})

    redis: Redis | None = None

    try:
        # Инициализируем глобальный пул соединений, как это делает основное приложение.
        await init_pool(db_dsn)
        logger.info(
            "PostgreSQL pool initialized for calibration job",
            extra={"db_dsn": db_dsn},
        )

        redis = Redis.from_url(redis_dsn, encoding="utf-8", decode_responses=False)

        signal_repo = _create_signal_repository()
        service = CalibrationService(redis=redis, signal_repository=signal_repo)

        # 1. Если не force — сначала проверяем PSI-дрифт
        if not force:
            psi, ok = await service.check_psi_drift(symbol=symbol)

            if psi is not None:
                extra: dict[str, Any] = {
                    "psi": str(psi),
                    "is_ok": ok,
                }

                # Пытаемся взять публичный threshold, но без доступа к приватным полям.
                threshold = getattr(service, "psi_threshold", None)
                if threshold is not None:
                    extra["psi_threshold"] = str(threshold)

                logger.info("PSI drift computed", extra=extra)

                if ok:
                    logger.info(
                        "PSI в норме, калибровка не требуется; завершаем без recalibrate()"
                    )
                    return
            else:
                logger.info(
                    "PSI не удалось посчитать (нет baseline или выборки); "
                    "будем выполнять калибровку."
                )

        # 2. Собственно калибровка
        theta_map = await service.calibrate(symbol=symbol)

        for hour in range(24):
            value = theta_map.get(hour)
            logger.info("Theta[%02d] = %s", hour, value)

        logger.info("Calibration job completed successfully")

    finally:
        # Закрываем глобальный пул PostgreSQL.
        try:
            await close_pool()
        except RuntimeError:
            # Пул мог не успеть инициализироваться или уже быть закрыт.
            logger.debug("PostgreSQL pool was not open during calibration shutdown")
        except Exception:  # noqa: BLE001
            logger.exception(
                "Error while closing PostgreSQL pool during calibration shutdown"
            )

        if redis is not None:
            await redis.close()


# ---------------------------------------------------------------------------
# CLI-обёртка
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline-калибровка AVI-5 (scripts/run_calibration.py)."
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Опциональный фильтр по символу (если не задан — калибруем по всем).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Принудительно выполнить калибровку, "
            "игнорируя проверку PSI-дрифта."
        ),
    )
    return parser.parse_args(argv)


def cli(argv: Optional[list[str]] = None) -> None:
    """
    Точка входа при запуске как скрипта.

    Настраивает логирование, парсит аргументы и вызывает асинхронную логику.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = _parse_args(argv)

    try:
        asyncio.run(_run_calibration(symbol=args.symbol, force=args.force))
    except KeyboardInterrupt:
        logger.warning("Calibration job interrupted by user")
        sys.exit(130)
    except RuntimeError as exc:
        logger.error("Configuration/runtime error: %s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error during calibration job: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    cli()
