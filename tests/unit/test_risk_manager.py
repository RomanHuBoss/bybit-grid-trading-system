# tests/unit/test_risk_manager.py

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from src.core.models import Position, RiskLimits, Signal
from src.db.repositories.position_repository import PositionRepository
from src.risk.risk_manager import RiskManager


# =====================================================================
# Вспомогательные фабрики доменных моделей
# =====================================================================


def make_limits(
    *,
    max_concurrent: int = 3,
    max_total_risk_r: Decimal = Decimal("5"),
    max_positions_per_symbol: int = 2,
    per_symbol_risk_r: dict[str, Decimal] | None = None,
) -> RiskLimits:
    """Создаёт объект RiskLimits с адекватными значениями по умолчанию."""
    return RiskLimits(
        max_concurrent=max_concurrent,
        max_total_risk_r=max_total_risk_r,
        max_positions_per_symbol=max_positions_per_symbol,
        per_symbol_risk_r=per_symbol_risk_r or {},
    )


def make_signal(
    symbol: str = "BTCUSDT",
    direction: str = "long",
    *,
    entry_price: Decimal = Decimal("100"),
    stake_usd: Decimal = Decimal("10"),
    probability: Decimal = Decimal("0.5"),
    strategy_version: str = "avi5-1.0.0",
) -> Signal:
    """Конструктор тестового сигнала, совместимый с актуальной моделью Signal."""
    now = datetime.now(timezone.utc)
    return Signal(
        id=uuid4(),
        created_at=now,
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stake_usd=stake_usd,
        probability=probability,
        strategy="AVI-5",
        strategy_version=strategy_version,
        queued_until=None,
        tp1=None,
        tp2=None,
        tp3=None,
        stop_loss=None,
        error_code=None,
        error_message=None,
    )


def make_position(
    symbol: str = "BTCUSDT",
    direction: str = "long",
    *,
    signal_id=None,
    entry_price: Decimal = Decimal("100"),
    size_base: Decimal = Decimal("1"),
    size_quote: Decimal | None = None,
    opened_at: datetime | None = None,
) -> Position:
    """Конструктор тестовой позиции, совместимый с актуальной моделью Position."""
    if signal_id is None:
        signal_id = uuid4()
    if opened_at is None:
        opened_at = datetime.now(timezone.utc)
    if size_quote is None:
        size_quote = entry_price * size_base

    return Position(
        id=uuid4(),
        signal_id=signal_id,
        symbol=symbol,
        side=direction,
        entry_price=entry_price,
        size_base=size_base,
        size_quote=size_quote,
        status="open",
        opened_at=opened_at,
        closed_at=None,
        pnl_usd=None,
        fill_ratio=Decimal("1"),
        slippage=Decimal("0"),
        funding=Decimal("0"),
    )


# =====================================================================
# Фикстуры для моков Redis и PositionRepository
# =====================================================================


@pytest.fixture
def redis_mock() -> AsyncMock:
    """AsyncMock вместо redis.asyncio.Redis."""
    return AsyncMock(name="RedisMock")


@pytest.fixture
def position_repo_mock() -> AsyncMock:
    """Мок репозитория позиций: все async-методы заменены на AsyncMock."""
    repo = AsyncMock(spec=PositionRepository)
    repo.list_open = AsyncMock(return_value=[])
    return repo


def make_risk_manager(
    *,
    limits: RiskLimits | None = None,
    redis: AsyncMock,
    position_repo: AsyncMock,
) -> RiskManager:
    """Утилита для создания RiskManager с кастомными лимитами и моками."""
    if limits is None:
        limits = make_limits()
    return RiskManager(
        limits=limits,
        redis=redis,
        position_repository=position_repo,
    )


# =====================================================================
# Anti-churn guard
# =====================================================================


@pytest.mark.asyncio
async def test_check_limits_blocked_by_anti_churn(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """
    Если AntiChurnGuard.is_blocked возвращает blocked=True,
    check_limits должен немедленно вернуть (False, 'anti_churn_block')
    и не ходить за открытыми позициями.
    """
    from src.risk import risk_manager as risk_module

    is_blocked_mock = AsyncMock(return_value=(True, datetime.now(timezone.utc)))
    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", is_blocked_mock)

    rm = make_risk_manager(redis=redis_mock, position_repo=position_repo_mock)
    sig = make_signal()

    allowed, reason = await rm.check_limits(sig)

    assert allowed is False
    assert reason == "anti_churn_block"

    is_blocked_mock.assert_awaited_once()
    args, kwargs = is_blocked_mock.call_args
    # AntiChurnGuard.is_blocked(self._redis, signal.symbol, signal.direction, now=...)
    assert args[0] is redis_mock
    assert args[1] == sig.symbol
    assert args[2] == sig.direction
    assert "now" in kwargs

    position_repo_mock.list_open.assert_not_awaited()


# =====================================================================
# max_concurrent
# =====================================================================


@pytest.mark.asyncio
async def test_check_limits_rejects_when_max_concurrent_reached(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """
    Если количество открытых позиций уже равно max_concurrent,
    новый сигнал должен быть отклонён с reason='max_concurrent'.
    """
    from src.risk import risk_manager as risk_module

    is_blocked_mock = AsyncMock(return_value=(False, None))
    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", is_blocked_mock)

    limits = make_limits(max_concurrent=2, max_total_risk_r=Decimal("10"))
    open_positions: List[Position] = [make_position(), make_position()]
    position_repo_mock.list_open = AsyncMock(return_value=open_positions)

    rm = make_risk_manager(limits=limits, redis=redis_mock, position_repo=position_repo_mock)
    sig = make_signal()

    allowed, reason = await rm.check_limits(sig)

    assert allowed is False
    assert reason == "max_concurrent"
    position_repo_mock.list_open.assert_awaited_once()


# =====================================================================
# per-base limit
# =====================================================================


@pytest.mark.asyncio
async def test_check_limits_rejects_when_per_base_limit_fails(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """Если can_open_position_for_base возвращает False, reason='per_base_limit'."""
    from src.risk import risk_manager as risk_module

    is_blocked_mock = AsyncMock(return_value=(False, None))
    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", is_blocked_mock)

    async def fake_list_open(symbol: str | None = None):
        assert symbol is None
        return [make_position(), make_position()]

    position_repo_mock.list_open = AsyncMock(side_effect=fake_list_open)

    def fake_can_open(
        positions: List[Position],
        symbol: str,
        direction: str,
        max_positions_per_base: int,
    ) -> bool:
        # Чуть-чуть трогаем аргументы, чтобы PyCharm не ругался на неиспользуемые.
        assert len(positions) == 2
        assert symbol == "BTCUSDT"
        assert direction == "long"
        assert isinstance(max_positions_per_base, int)
        return False

    monkeypatch.setattr(risk_module, "can_open_position_for_base", fake_can_open)

    limits = make_limits(
        max_concurrent=10,
        max_total_risk_r=Decimal("10"),
        max_positions_per_symbol=10,
    )
    rm = make_risk_manager(limits=limits, redis=redis_mock, position_repo=position_repo_mock)
    sig = make_signal()

    allowed, reason = await rm.check_limits(sig)

    assert allowed is False
    assert reason == "per_base_limit"
    position_repo_mock.list_open.assert_awaited_once()


# =====================================================================
# max_total_risk_r
# =====================================================================


@pytest.mark.asyncio
async def test_check_limits_respects_max_total_risk_r(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """Если суммарный риск превышает max_total_risk_r, reason='max_total_risk_r'."""
    from src.risk import risk_manager as risk_module

    is_blocked_mock = AsyncMock(return_value=(False, None))
    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", is_blocked_mock)

    limits = make_limits(
        max_concurrent=10,
        max_total_risk_r=Decimal("2"),
        max_positions_per_symbol=10,
    )
    open_positions: List[Position] = [make_position(), make_position()]
    position_repo_mock.list_open = AsyncMock(return_value=open_positions)

    def fake_can_open(
        positions: List[Position],
        symbol: str,
        direction: str,
        max_positions_per_base: int,
    ) -> bool:
        assert len(positions) == 2
        assert symbol == "BTCUSDT"
        assert direction == "long"
        assert isinstance(max_positions_per_base, int)
        # per-base лимит пропускает
        return True

    monkeypatch.setattr(risk_module, "can_open_position_for_base", fake_can_open)

    rm = make_risk_manager(limits=limits, redis=redis_mock, position_repo=position_repo_mock)
    sig = make_signal()

    allowed, reason = await rm.check_limits(sig)

    assert allowed is False
    assert reason == "max_total_risk_r"
    position_repo_mock.list_open.assert_awaited_once()


# =====================================================================
# per_symbol_risk_r
# =====================================================================


@pytest.mark.asyncio
async def test_check_limits_respects_per_symbol_risk(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """Если per_symbol_risk_r для символа исчерпан, reason='per_symbol_risk_r'."""
    from src.risk import risk_manager as risk_module

    is_blocked_mock = AsyncMock(return_value=(False, None))
    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", is_blocked_mock)

    existing_positions = [make_position(symbol="BTCUSDT")]
    position_repo_mock.list_open = AsyncMock(return_value=existing_positions)

    def fake_can_open(
        positions: List[Position],
        symbol: str,
        direction: str,
        max_positions_per_base: int,
    ) -> bool:
        assert len(positions) == 1
        assert symbol == "BTCUSDT"
        assert direction == "long"
        assert isinstance(max_positions_per_base, int)
        # per-base лимит пропускает
        return True

    monkeypatch.setattr(risk_module, "can_open_position_for_base", fake_can_open)

    limits = make_limits(
        max_concurrent=10,
        max_total_risk_r=Decimal("10"),
        max_positions_per_symbol=10,
        per_symbol_risk_r={"BTCUSDT": Decimal("1")},
    )
    rm = make_risk_manager(limits=limits, redis=redis_mock, position_repo=position_repo_mock)
    sig = make_signal(symbol="BTCUSDT")

    allowed, reason = await rm.check_limits(sig)

    assert allowed is False
    assert reason == "per_symbol_risk_r"
    position_repo_mock.list_open.assert_awaited_once()


# =====================================================================
# happy path
# =====================================================================


@pytest.mark.asyncio
async def test_check_limits_allows_when_all_constraints_pass(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """Если все ограничения пройдены, check_limits должен разрешить вход."""
    from src.risk import risk_manager as risk_module

    is_blocked_mock = AsyncMock(return_value=(False, None))
    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", is_blocked_mock)

    existing_positions = [make_position(symbol="BTCUSDT")]
    position_repo_mock.list_open = AsyncMock(return_value=existing_positions)

    def fake_can_open(
        positions: List[Position],
        symbol: str,
        direction: str,
        max_positions_per_base: int,
    ) -> bool:
        assert positions == existing_positions
        assert symbol == "BTCUSDT"
        assert direction == "long"
        assert isinstance(max_positions_per_base, int)
        return True

    monkeypatch.setattr(risk_module, "can_open_position_for_base", fake_can_open)

    limits = make_limits(
        max_concurrent=10,
        max_total_risk_r=Decimal("10"),
        max_positions_per_symbol=5,
        per_symbol_risk_r={"ETHUSDT": Decimal("3")},
    )
    rm = make_risk_manager(limits=limits, redis=redis_mock, position_repo=position_repo_mock)
    sig = make_signal(symbol="BTCUSDT")

    allowed, reason = await rm.check_limits(sig)

    assert allowed is True
    assert reason is None
    position_repo_mock.list_open.assert_awaited_once()


# =====================================================================
# on_position_opened / on_position_closed
# =====================================================================


@pytest.mark.asyncio
async def test_on_position_opened_records_signal_in_anti_churn(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """on_position_opened должен вызвать AntiChurnGuard.record_signal."""
    from src.risk import risk_manager as risk_module

    record_mock = AsyncMock()
    monkeypatch.setattr(risk_module.AntiChurnGuard, "record_signal", record_mock)

    rm = make_risk_manager(redis=redis_mock, position_repo=position_repo_mock)
    pos = make_position(symbol="BTCUSDT")

    await rm.on_position_opened(pos)

    record_mock.assert_awaited_once()
    args, kwargs = record_mock.call_args
    # record_signal(self._redis, symbol=..., side=..., now=...)
    assert args[0] is redis_mock
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["side"] == pos.direction
    assert "now" in kwargs


@pytest.mark.asyncio
async def test_on_position_closed_does_not_touch_anti_churn(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """on_position_closed по умолчанию не вызывает AntiChurnGuard.clear_block."""
    from src.risk import risk_manager as risk_module

    clear_mock = AsyncMock()
    monkeypatch.setattr(risk_module.AntiChurnGuard, "clear_block", clear_mock)

    rm = make_risk_manager(redis=redis_mock, position_repo=position_repo_mock)
    pos = make_position(symbol="BTCUSDT")

    await rm.on_position_closed(pos)

    clear_mock.assert_not_awaited()


# =====================================================================
# update_limits
# =====================================================================


def test_update_limits_replaces_limits_object(
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """update_limits должен просто заменить self.limits на новый объект."""
    initial_limits = make_limits(
        max_concurrent=1,
        max_total_risk_r=Decimal("2"),
        max_positions_per_symbol=1,
    )
    rm = make_risk_manager(limits=initial_limits, redis=redis_mock, position_repo=position_repo_mock)

    new_limits = make_limits(
        max_concurrent=5,
        max_total_risk_r=Decimal("10"),
        max_positions_per_symbol=3,
        per_symbol_risk_r={"BTCUSDT": Decimal("2")},
    )

    rm.update_limits(new_limits)

    assert rm.limits is new_limits
    assert rm.limits.max_concurrent == 5
    assert rm.limits.max_total_risk_r == Decimal("10")
    assert rm.limits.max_positions_per_symbol == 3
    assert rm.limits.per_symbol_risk_r["BTCUSDT"] == Decimal("2")
