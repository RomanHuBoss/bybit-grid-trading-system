from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import sys
from typing import Any, Optional

import asyncpg
from redis.asyncio import Redis

from src.core.config_loader import ConfigLoader
from src.strategies.calibration import CalibrationService
from src.db.repositories.signal_repository import SignalRepository  # type: ignore[import]


logger = logging.getLogger("scripts.run_calibration")


# ---------------------------------------------------------------------------
# Вспомогательные функции для чтения конфига / DSN / Redis
# ---------------------------------------------------------------------------


def _resolve_db_dsn(config: Any) -> str:
    """
    Попробовать достать DSN для PostgreSQL из AppConfig или окружения.

    Приоритет:
      1) config.db.dsn / url / database_url / uri
      2) переменная окружения DATABASE_URL

    Если ничего не найдено — бросает RuntimeError с человекочитаемым текстом.
    """
    db_section = getattr(config, "db", None)

    # Pydantic-модель или dict — поддерживаем оба варианта.
    def _get_from_section(section: Any, name: str) -> Optional[str]:
        if section is None:
            return None
        if isinstance(section, dict):
            return section.get(name)
        return getattr(section, name, None)

    for field in ("dsn", "url", "database_url", "uri"):
        value = _get_from_section(db_section, field)
        if value:
            return str(value)

    env_dsn = os.getenv("DATABASE_URL")
    if env_dsn:
        return env_dsn

    raise RuntimeError(
        "Не удалось определить DSN для БД: ожидается config.db.dsn/url "
        "или переменная окружения DATABASE_URL."
    )


def _resolve_redis_dsn(config: Any) -> str:
    """
    Попробовать достать DSN/URL для Redis из AppConfig или окружения.

    Приоритет:
      1) config.redis.dsn / url
      2) REDIS_URL / REDIS_DSN
      3) host/port/db из config.redis (если заданы)

    Если ничего внятного не найдено — бросает RuntimeError.
    """
    redis_section = getattr(config, "redis", None)

    def _get_from_section(section: Any, name: str) -> Optional[str]:
        if section is None:
            return None
        if isinstance(section, dict):
            return section.get(name)
        return getattr(section, name, None)

    for field in ("dsn", "url"):
        value = _get_from_section(redis_section, field)
        if value:
            return str(value)

    env_url = os.getenv("REDIS_URL") or os.getenv("REDIS_DSN")
    if env_url:
        return env_url

    # Пробуем собрать URL из host/port/db, если они есть в конфиге.
    host = _get_from_section(redis_section, "host") or "localhost"
    port = _get_from_section(redis_section, "port") or 6379
    db = _get_from_section(redis_section, "db") or 0

    try:
        port_int = int(port)
        db_int = int(db)
    except (TypeError, ValueError):
        raise RuntimeError(
            "Не удалось собрать Redis DSN из config.redis.*; "
            "задайте redis.url/redis.dsn или REDIS_URL."
        ) from None

    return f"redis://{host}:{port_int}/{db_int}"


def _create_signal_repository(pool: asyncpg.Pool) -> SignalRepository:
    """
    Создать экземпляр SignalRepository, не зная точной сигнатуры __init__.

    Мы не ломаем чужой код и не навязываем интерфейс, поэтому:
      - рефлексируем сигнатуру SignalRepository;
      - если есть аргумент pool / db_pool / conn — используем его;
      - иначе пробуем передать пул позиционно.

    Такой подход остаётся совместимым с существующей реализацией.
    """
    sig = inspect.signature(SignalRepository)  # type: ignore[call-arg]
    params = sig.parameters

    kwargs: dict[str, Any] = {}
    if "pool" in params:
        kwargs["pool"] = pool
    elif "db_pool" in params:
        kwargs["db_pool"] = pool
    elif "conn" in params:
        kwargs["conn"] = pool

    if kwargs:
        return SignalRepository(**kwargs)  # type: ignore[call-arg]
    # Фоллбек — передаём пул как первый позиционный аргумент.
    return SignalRepository(pool)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Основная асинхронная логика
# ---------------------------------------------------------------------------


async def _run_calibration(symbol: Optional[str], force: bool) -> None:
    """
    Запуск offline-калибровки AVI-5.

    Поведение:
      1) Загружаем AppConfig через ConfigLoader.
      2) Поднимаем пул PostgreSQL и Redis-клиент.
      3) Собираем CalibrationService.
      4) Если не указан --force:
           - пробуем посчитать PSI-дрифт;
           - если baseline есть и дрифт в норме — калибровку пропускаем.
      5) Иначе (или при проблемах с PSI) запускаем calibrate() и логируем карту theta(h).
    """
    loader = ConfigLoader()
    config = loader.get_config()

    db_dsn = _resolve_db_dsn(config)
    redis_dsn = _resolve_redis_dsn(config)

    logger.info("Starting calibration job", extra={"symbol": symbol, "force": force})

    pool: asyncpg.Pool | None = None
    redis: Redis | None = None

    try:
        pool = await asyncpg.create_pool(dsn=db_dsn, min_size=1, max_size=5)
        redis = Redis.from_url(redis_dsn, encoding="utf-8", decode_responses=False)

        signal_repo = _create_signal_repository(pool)
        service = CalibrationService(redis=redis, signal_repository=signal_repo)

        # 1. Если не force — сначала проверяем PSI-дрифт
        if not force:
            psi, ok = await service.check_psi_drift(symbol=symbol)
            if psi is not None:
                logger.info(
                    "PSI drift computed",
                    extra={
                        "psi": str(psi),
                        "psi_threshold": str(service._params.psi_threshold),  # noqa: SLF001
                        "is_ok": ok,
                    },
                )
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

        # Красиво логируем карту theta по часам
        for hour in range(24):
            value = theta_map.get(hour)
            logger.info("Theta[%02d] = %s", hour, value)

        logger.info("Calibration job completed successfully")

    finally:
        # Аккуратно закрываем ресурсы
        if pool is not None:
            await pool.close()
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
