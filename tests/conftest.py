# tests/conftest.py

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncGenerator, Generator, Optional
from uuid import uuid4

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

import asyncpg
import redis.asyncio as aioredis

from src.core.models import ConfirmedCandle, Signal

# ============================================
# Конфигурация тестовой среды
# ============================================

TEST_DB_DSN = os.getenv("TEST_DB_DSN")
TEST_REDIS_DSN = os.getenv("TEST_REDIS_DSN", "redis://localhost:6379/0")


@dataclass
class TestDBInfo:
  dsn: str


test_db_info: Optional[TestDBInfo] = None


def pytest_sessionstart(session: pytest.Session) -> None:  # noqa: D401
    """
    Перед стартом всего тест-рана проверяем доступность тестовой БД.

    Если переменная окружения TEST_DB_DSN не задана
    или подключиться не удаётся — валим весь ран тестов с понятной ошибкой.
    """
    global test_db_info

    if not TEST_DB_DSN:
        raise RuntimeError(
            "TEST_DB_DSN is not set. "
            "Укажите строку подключения к тестовой БД в переменной окружения TEST_DB_DSN."
        )

    async def _check_db() -> None:
        conn: Optional[asyncpg.Connection] = None
        try:
            conn = await asyncpg.connect(TEST_DB_DSN)
            await conn.execute("SELECT 1;")
        finally:
            if conn is not None:
                await conn.close()

    try:
        asyncio.run(_check_db())
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Не удалось подключиться к тестовой БД по DSN={TEST_DB_DSN!r}") from exc

    test_db_info = TestDBInfo(dsn=TEST_DB_DSN)


# ============================================
# Event loop для pytest-asyncio
# ============================================


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """
    Общий event loop для всех async-тестов.

    Pytest-asyncio ожидает фикстуру event_loop с scope=session.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


# ============================================
# Фикстуры БД (Timescale/PostgreSQL через asyncpg)
# ============================================


@pytest_asyncio.fixture(scope="session")
async def db_pool(event_loop: asyncio.AbstractEventLoop) -> AsyncGenerator[asyncpg.Pool, None]:
    """
    Пул соединений к тестовой БД.

    Используется как общий ресурс session-уровня, а "чистота" состояния
    обеспечивается уже на уровне фикстур меньшего scope (см. db_conn).
    """
    if not test_db_info:
        raise RuntimeError("test_db_info is not initialised; pytest_sessionstart probably failed.")

    pool = await asyncpg.create_pool(dsn=test_db_info.dsn, min_size=1, max_size=5)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def db_conn(db_pool: asyncpg.Pool) -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Отдельное соединение к БД с транзакцией, которая откатывается после теста.

    Это гарантирует, что каждый тест видит "чистое" состояние БД
    (при условии, что schema уже развёрнута заранее).
    """
    conn = await db_pool.acquire()
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
        await db_pool.release(conn)


# ============================================
# Фикстуры Redis
# ============================================


@pytest_asyncio.fixture(scope="session")
async def redis_client() -> AsyncGenerator[aioredis.Redis, None]:
    """
    Клиент к тестовому Redis.

    URL берётся из TEST_REDIS_DSN (по умолчанию redis://localhost:6379/0).
    """
    client: aioredis.Redis = aioredis.from_url(TEST_REDIS_DSN, decode_responses=True)
    try:
        # Лёгкая проверка доступности
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Не удалось подключиться к тестовому Redis по DSN={TEST_REDIS_DSN!r}") from exc

    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def redis_flushed(redis_client: aioredis.Redis) -> AsyncGenerator[aioredis.Redis, None]:
    """
    Redis-клиент с очищенной БД.

    Перед тестом выполняется FLUSHDB, чтобы состояние не текло между тестами.
    """
    await redis_client.flushdb()
    yield redis_client


# ============================================
# Мок Bybit-клиента
# ============================================


@pytest.fixture
def bybit_client_mock() -> AsyncMock:
    """
    Универсальный мок клиента Bybit.

    Используется в тестах, где важно отслеживать какие методы дергаются и с какими аргументами.
    Конкретные методы (create_order, cancel_order и т.д.) задаются в самих тестах.
    """
    client = AsyncMock(name="BybitClientMock")

    # Примеры типичных async-методов — не навязываем поведение, только создаём заглушки.
    client.create_order = AsyncMock(name="create_order")
    client.cancel_order = AsyncMock(name="cancel_order")
    client.get_open_positions = AsyncMock(name="get_open_positions")
    client.get_position_risk = AsyncMock(name="get_position_risk")

    return client


# ============================================
# Сэмпловые доменные объекты (Signal, Candle)
# ============================================


@pytest.fixture
def sample_signal() -> Signal:
    """
    Типичный валидный сигнал AVI-5.

    Используется в unit-тестах как "золотой" пример корректного объекта.
    Важно, что он проходит все валидаторы модели Signal.
    """
    now = datetime.now(timezone.utc)
    return Signal(
        id=uuid4(),
        created_at=now,
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("50000"),
        stake_usd=Decimal("10"),
        probability=Decimal("0.65"),
        strategy="AVI-5",
        strategy_version="avi5-test",
        queued_until=now,
        tp1=Decimal("50500"),
        tp2=Decimal("51000"),
        tp3=Decimal("52000"),
        stop_loss=Decimal("49500"),
        error_code=None,         # <<< добавлено
        error_message=None,      # <<< добавлено
    )


@pytest.fixture
def sample_candle() -> ConfirmedCandle:
    """
    Подтверждённая 5-минутная свеча с валидными OHLCV-данными.

    Годится как базовый input для SignalEngine / любых расчётов, завязанных на ConfirmedCandle.
    """
    now = datetime.now(timezone.utc)
    open_time = now.replace(second=0, microsecond=0)
    close_time = open_time.replace(minute=open_time.minute + 5)

    return ConfirmedCandle(
        symbol="BTCUSDT",
        open_time=open_time,
        close_time=close_time,
        open=Decimal("50000"),
        high=Decimal("50100"),
        low=Decimal("49950"),
        close=Decimal("50050"),
        volume=Decimal("123.45"),
        confirmed=True,
    )
