# tests/unit/test_rate_limiter.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.core.constants import CHURN_BLOCK_SEC
from src.risk.anti_churn import AntiChurnGuard


def make_redis_mock() -> AsyncMock:
    """
    Простейший AsyncMock, который имитирует интерфейс redis.asyncio.Redis.

    Мы явно объявляем методы get/setex/delete, чтобы IDE и type-checker
    не ругались и чтобы в тестах было видно ожидаемый контракт.
    """
    redis = AsyncMock(name="RedisMock")
    redis.get = AsyncMock(name="get")
    redis.setex = AsyncMock(name="setex")
    redis.delete = AsyncMock(name="delete")
    return redis


# =====================================================================
# _make_key: нормализация symbol / direction и формат ключа
# =====================================================================


def test_make_key_normalizes_symbol_and_direction() -> None:
    """
    Приватный _make_key должен приводить символ к UPPERCASE,
    направление к lowercase и собирать ключ по актуальному шаблону
    anti_churn:{SYMBOL_UPPER}:{DIRECTION_LOWER}.
    """
    key = AntiChurnGuard._make_key(symbol="btcusdt", direction="Long")
    assert key == "anti_churn:BTCUSDT:long"


# =====================================================================
# is_blocked: базовые сценарии
# =====================================================================


@pytest.mark.asyncio
async def test_is_blocked_returns_false_when_no_key() -> None:
    """
    Если в Redis нет ключа с временем истечения блокировки (get -> None),
    блок должен считаться неактивным.
    """
    redis = make_redis_mock()
    redis.get.return_value = None

    blocked, block_until = await AntiChurnGuard.is_blocked(
        redis=redis,
        symbol="BTCUSDT",
        direction="long",
        now=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    redis.get.assert_awaited_once()
    assert blocked is False
    assert block_until is None


@pytest.mark.asyncio
async def test_is_blocked_returns_false_on_garbage_value_and_deletes_key() -> None:
    """
    Если в ключе лежит мусор, который нельзя распарсить как ISO-дату,
    guard должен:
      * защитно считать блок неактивным;
      * удалить ключ из Redis.
    """
    redis = make_redis_mock()
    redis.get.return_value = b"not-an-iso-timestamp"

    blocked, block_until = await AntiChurnGuard.is_blocked(
        redis=redis,
        symbol="BTCUSDT",
        direction="short",
        now=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    redis.get.assert_awaited_once()
    redis.delete.assert_awaited_once()
    assert blocked is False
    assert block_until is None


@pytest.mark.asyncio
async def test_is_blocked_fails_closed_on_redis_error() -> None:
    """
    При ошибке Redis (исключение при get) guard должен вести себя fail-closed:
    считать вход заблокированным, чтобы не ослаблять риск-контроль.
    """
    redis = make_redis_mock()
    redis.get.side_effect = RuntimeError("redis boom")

    blocked, block_until = await AntiChurnGuard.is_blocked(
        redis=redis,
        symbol="BTCUSDT",
        direction="long",
        now=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    redis.get.assert_awaited_once()
    assert blocked is True
    assert block_until is None


# =====================================================================
# is_blocked: логика окна CHURN_BLOCK_SEC через block_until
# =====================================================================


@pytest.mark.asyncio
async def test_is_blocked_true_when_within_churn_window() -> None:
    """
    Если с момента последнего сигнала прошло меньше CHURN_BLOCK_SEC секунд,
    is_blocked возвращает blocked=True и корректный block_until.

    В новой реализации длина окна кодируется в самом block_until, а не
    через "сырые" таймстемпы — поэтому тест подготавливает ISO-дату,
    соответствующую ещё не истёкшей блокировке.
    """
    redis = make_redis_mock()

    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    last_time = now - timedelta(seconds=CHURN_BLOCK_SEC // 2)
    block_until = last_time + timedelta(seconds=CHURN_BLOCK_SEC)
    redis.get.return_value = block_until.isoformat().encode("utf-8")

    blocked, returned_until = await AntiChurnGuard.is_blocked(
        redis=redis,
        symbol="BTCUSDT",
        direction="long",
        now=now,
    )

    assert blocked is True
    assert isinstance(returned_until, datetime)
    assert returned_until == block_until


@pytest.mark.asyncio
async def test_is_blocked_false_when_window_expired() -> None:
    """
    Если block_until в прошлом или ровно "сейчас", блок должен считаться истёкшим
    (blocked=False, block_until=None), а ключ в Redis должен быть удалён.
    """
    redis = make_redis_mock()

    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Считаем, что окно длиной CHURN_BLOCK_SEC уже закончилось.
    block_until = now - timedelta(seconds=1)
    redis.get.return_value = block_until.isoformat().encode("utf-8")

    blocked, block_until_result = await AntiChurnGuard.is_blocked(
        redis=redis,
        symbol="BTCUSDT",
        direction="short",
        now=now,
    )

    assert blocked is False
    assert block_until_result is None
    redis.delete.assert_awaited_once()


# =====================================================================
# record_signal: запись времени истечения блокировки с TTL
# =====================================================================


@pytest.mark.asyncio
async def test_record_signal_sets_block_until_with_ttl() -> None:
    """
    record_signal должен вызвать redis.setex(...) с ключом,
    TTL=CHURN_BLOCK_SEC (когда ttl_seconds задан явно) и ISO-строкой
    времени истечения блокировки.
    """
    redis = make_redis_mock()

    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    await AntiChurnGuard.record_signal(
        redis=redis,
        symbol="btcusdt",  # проверяем, что нормализуется в ключе
        side="Long",
        now=now,
        ttl_seconds=CHURN_BLOCK_SEC,
    )

    redis.setex.assert_awaited_once()
    key_arg, ttl_arg, value_arg = redis.setex.call_args.args

    assert key_arg == "anti_churn:BTCUSDT:long"
    assert ttl_arg == CHURN_BLOCK_SEC

    # value_arg должен быть ISO-строкой времени истечения окна блокировки:
    # now + CHURN_BLOCK_SEC секунд.
    parsed_block_until = datetime.fromisoformat(value_arg)
    expected_until = now + timedelta(seconds=CHURN_BLOCK_SEC)
    assert parsed_block_until == expected_until


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
        direction="Short",
    )

    redis.delete.assert_awaited_once()
    (key_arg,) = redis.delete.call_args.args
    assert key_arg == "anti_churn:BTCUSDT:short"
