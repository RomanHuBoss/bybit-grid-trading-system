# tests/unit/test_rate_limiter.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.core.constants import CHURN_BLOCK_SEC
from src.risk.anti_churn import AntiChurnGuard


def make_redis_mock() -> AsyncMock:
    """
    Простейший AsyncMock, который имитирует интерфейс redis.asyncio.Redis.
    """
    redis = AsyncMock(name="RedisMock")
    # Явно объявляем методы, чтобы IDE видела их существование.
    redis.get = AsyncMock(name="get")
    redis.setex = AsyncMock(name="setex")
    redis.delete = AsyncMock(name="delete")
    return redis


# =====================================================================
# _make_key: нормализация symbol / side и формат ключа
# =====================================================================


def test_make_key_normalizes_symbol_and_side() -> None:
    """
    Приватный _make_key должен приводить символ к UPPERCASE,
    направление к lowercase и собирать ключ по шаблону.
    """
    key = AntiChurnGuard._make_key(symbol="btcusdt", side="Long")
    assert key == "last_signal_time:BTCUSDT:long"


# =====================================================================
# is_blocked: базовые сценарии
# =====================================================================


@pytest.mark.asyncio
async def test_is_blocked_returns_false_when_no_key() -> None:
    """
    Если в Redis нет ключа с таймстемпом последнего сигнала (get -> None),
    блок должен считаться неактивным.
    """
    redis = make_redis_mock()
    redis.get.return_value = None

    blocked, block_until = await AntiChurnGuard.is_blocked(
        redis=redis,
        symbol="BTCUSDT",
        side="long",
        now=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    redis.get.assert_awaited_once()
    assert blocked is False
    assert block_until is None


@pytest.mark.asyncio
async def test_is_blocked_returns_false_on_garbage_value() -> None:
    """
    Если в ключе лежит мусор, который нельзя привести к float,
    guard защитно считает блок неактивным.
    """
    redis = make_redis_mock()
    redis.get.return_value = "not-a-float"

    blocked, block_until = await AntiChurnGuard.is_blocked(
        redis=redis,
        symbol="BTCUSDT",
        side="short",
        now=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    redis.get.assert_awaited_once()
    assert blocked is False
    assert block_until is None


# =====================================================================
# is_blocked: логика окна CHURN_BLOCK_SEC
# =====================================================================


@pytest.mark.asyncio
async def test_is_blocked_true_when_within_churn_window() -> None:
    """
    Если с момента последнего сигнала прошло меньше CHURN_BLOCK_SEC секунд,
    is_blocked возвращает blocked=True и корректный block_until.
    """
    redis = make_redis_mock()

    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Последний сигнал был 60 секунд назад — явно внутри окна 900 сек.
    last_time = now - timedelta(seconds=60)
    redis.get.return_value = str(last_time.timestamp())

    blocked, block_until = await AntiChurnGuard.is_blocked(
        redis=redis,
        symbol="BTCUSDT",
        side="long",
        now=now,
    )

    assert blocked is True
    assert isinstance(block_until, datetime)

    # Ожидаемое block_until = last_time + CHURN_BLOCK_SEC
    expected_until = last_time + timedelta(seconds=CHURN_BLOCK_SEC)
    # Сравниваем с небольшой дельтой, чтобы не упасть на округлении float → datetime.
    assert abs((block_until - expected_until).total_seconds()) < 1e-6


@pytest.mark.asyncio
async def test_is_blocked_false_when_window_expired() -> None:
    """
    Если прошло >= CHURN_BLOCK_SEC секунд, блок должен считаться
    уже истёкшим (blocked=False, block_until=None).
    """
    redis = make_redis_mock()

    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Последний сигнал был ровно CHURN_BLOCK_SEC секунд назад.
    last_time = now - timedelta(seconds=CHURN_BLOCK_SEC)
    redis.get.return_value = str(last_time.timestamp())

    blocked, block_until = await AntiChurnGuard.is_blocked(
        redis=redis,
        symbol="BTCUSDT",
        side="short",
        now=now,
    )

    assert blocked is False
    assert block_until is None


# =====================================================================
# record_signal: запись таймстемпа с TTL
# =====================================================================


@pytest.mark.asyncio
async def test_record_signal_sets_timestamp_with_ttl() -> None:
    """
    record_signal должен вызвать redis.setex(...) с ключом,
    TTL=CHURN_BLOCK_SEC и строковым таймстемпом now.timestamp().
    """
    redis = make_redis_mock()

    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    await AntiChurnGuard.record_signal(
        redis=redis,
        symbol="btcusdt",  # проверяем, что нормализуется в ключе
        side="Long",
        now=now,
    )

    redis.setex.assert_awaited_once()
    key_arg, ttl_arg, value_arg = redis.setex.call_args.args

    assert key_arg == "last_signal_time:BTCUSDT:long"
    assert ttl_arg == CHURN_BLOCK_SEC
    # str(now.timestamp()) — именно так формирует функцию record_signal
    assert Decimal(value_arg) == Decimal(str(now.timestamp()))


# =====================================================================
# clear_block: удаление ключа
# =====================================================================


@pytest.mark.asyncio
async def test_clear_block_deletes_key() -> None:
    """
    clear_block должен вызвать redis.delete с тем же ключом,
    который использует record_signal / is_blocked.
    """
    redis = make_redis_mock()

    await AntiChurnGuard.clear_block(
        redis=redis,
        symbol="btcusdt",
        side="Short",
    )

    redis.delete.assert_awaited_once()
    (key_arg,) = redis.delete.call_args.args
    assert key_arg == "last_signal_time:BTCUSDT:short"
