from __future__ import annotations

import logging
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

__pg_pool: Optional[asyncpg.Pool] = None
__pg_dsn: Optional[str] = None
__pool_closed: bool = False

# Публичный контракт модуля
__all__ = ["init_pool", "get_pool", "close_pool"]


async def init_pool(dsn: str) -> asyncpg.Pool:
    """
    Инициализировать глобальный пул соединений к PostgreSQL.

    - Создаёт asyncpg.Pool с min_size=5, max_size=20.
    - Выполняет health-check запросом `SELECT 1`.
    - Кеширует пул в модуле и возвращает его.
    - При невозможности подключиться выбрасывает ConnectionError.

    Повторный вызов, если пул уже живой, возвращает существующий экземпляр.
    """
    global __pg_pool, __pg_dsn, __pool_closed

    if __pg_pool is not None and not __pool_closed:
        # Пул уже инициализирован и не закрыт — просто возвращаем его.
        return __pg_pool

    logger.info("Initializing PostgreSQL connection pool")

    __pg_dsn = dsn
    pool: Optional[asyncpg.Pool] = None

    try:
        pool = await asyncpg.create_pool(dsn=dsn, min_size=5, max_size=20)

        # Health-check: убеждаемся, что подключение живое.
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to initialize PostgreSQL pool")
        if pool is not None:
            try:
                await pool.close()
            except Exception:  # noqa: BLE001
                logger.debug("Error while closing failed PostgreSQL pool", exc_info=True)
        __pg_pool = None
        __pool_closed = True
        raise ConnectionError(f"Unable to connect to PostgreSQL: {exc}") from exc

    __pg_pool = pool
    __pool_closed = False
    logger.info("PostgreSQL connection pool initialized successfully")
    return pool


def get_pool() -> asyncpg.Pool:
    """
    Получить текущий глобальный пул соединений к БД.

    :raises RuntimeError: если пул ещё не инициализирован или уже закрыт.
    """
    if __pg_pool is None or __pool_closed:
        raise RuntimeError("PostgreSQL pool is not initialized or already closed")
    return __pg_pool


async def close_pool() -> None:
    """
    Аккуратно закрыть глобальный пул соединений.

    - Закрывает пул и сбрасывает ссылку в модуле.
    - Повторный вызов приведёт к RuntimeError.
    """
    global __pg_pool, __pool_closed

    if __pg_pool is None or __pool_closed:
        raise RuntimeError("PostgreSQL pool is not initialized or already closed")

    logger.info("Closing PostgreSQL connection pool")

    pool = __pg_pool
    __pg_pool = None
    __pool_closed = True

    try:
        await pool.close()
    except Exception:  # noqa: BLE001
        # Логируем, но не перекидываем — на этапе shutdown важнее не упасть.
        logger.exception("Error while closing PostgreSQL pool")
