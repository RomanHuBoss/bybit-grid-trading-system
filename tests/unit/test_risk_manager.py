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
    return RiskLimits(
        max_concurrent=max_concurrent,
        max_total_risk_r=max_total_risk_r,
        max_positions_per_symbol=max_positions_per_symbol,
        per_symbol_risk_r=per_symbol_risk_r or {},
    )


def make_position(symbol: str = "BTCUSDT") -> Position:
    now = datetime.now(timezone.utc)
    return Position(
        id=uuid4(),
        signal_id=uuid4(),
        opened_at=now,
        closed_at=None,
        symbol=symbol,
        direction="long",
        entry_price=Decimal("100"),
        size_base=Decimal("1"),
        size_quote=Decimal("100"),
        fill_ratio=Decimal("1"),
        slippage=Decimal("0"),
        funding=Decimal("0"),
    )


def make_signal(symbol: str = "BTCUSDT", direction: str = "long") -> Signal:
    """
    Конструктор тестового сигнала.

    Специально заполняем поля strategy / error_code / error_message,
    чтобы IDE не подсвечивала их как "unfilled".
    """
    now = datetime.now(timezone.utc)
    return Signal(
        id=uuid4(),
        created_at=now,
        symbol=symbol,
        direction=direction,
        entry_price=Decimal("100"),
        stake_usd=Decimal("10"),
        probability=Decimal("0.5"),
        strategy="AVI-5",
        strategy_version="test",
        queued_until=None,
        tp1=None,
        tp2=None,
        tp3=None,
        stop_loss=None,
        error_code=None,
        error_message=None,
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
    """
    Мок репозитория позиций: все async-методы заменены на AsyncMock.
    """
    repo = AsyncMock(spec=PositionRepository)
    # По умолчанию считаем, что открытых позиций нет.
    repo.list_open = AsyncMock(return_value=[])
    return repo


def make_risk_manager(
    *,
    limits: RiskLimits | None = None,
    redis: AsyncMock,
    position_repo: AsyncMock,
) -> RiskManager:
    """
    Утилита для создания RiskManager с кастомными лимитами и моками.
    """
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

    async def fake_is_blocked(
        _redis,
        symbol: str,
        side: str,
        now: datetime | None = None,
    ):
        # немного используем параметры, чтобы не было warning-ов
        assert symbol == "BTCUSDT"
        assert side == "long"
        assert isinstance(now, (datetime, type(None)))
        return True, datetime.now(timezone.utc)

    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", fake_is_blocked)

    rm = make_risk_manager(redis=redis_mock, position_repo=position_repo_mock)
    sig = make_signal()

    allowed, reason = await rm.check_limits(sig)

    assert allowed is False
    assert reason == "anti_churn_block"
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

    async def fake_is_blocked(
        _redis,
        _symbol: str,
        _side: str,
        _now: datetime | None = None,
    ):
        return False, None

    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", fake_is_blocked)

    # max_concurrent=2, а открытых позиций уже 2
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
# per-base limit (position_limits.can_open_position_for_base)
# =====================================================================


@pytest.mark.asyncio
async def test_check_limits_rejects_when_per_base_limit_fails(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """
    Если can_open_position_for_base возвращает False,
    должен вернуться reason='per_base_limit'.
    """
    from src.risk import risk_manager as risk_module

    async def fake_is_blocked(
        _redis,
        _symbol: str,
        _side: str,
        _now: datetime | None = None,
    ):
        return False, None

    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", fake_is_blocked)

    async def fake_list_open(symbol: str | None = None):
        assert symbol is None  # RiskManager.list_open вызывает без фильтра
        return [make_position(), make_position()]

    position_repo_mock.list_open = AsyncMock(side_effect=fake_list_open)

    # Патчим функцию can_open_position_for_base, импортированную в risk_manager.
    def fake_can_open(
        positions: List[Position],
        symbol: str,
        direction: str,
        max_positions_per_base: int,
    ) -> bool:
        # Используем параметры, чтобы линтер был доволен.
        assert len(positions) == 2
        assert symbol == "BTCUSDT"
        assert direction == "long"
        assert isinstance(max_positions_per_base, int)
        # Имитация нарушения per-base лимита.
        return False

    monkeypatch.setattr(risk_module, "can_open_position_for_base", fake_can_open)

    limits = make_limits(
        max_concurrent=10,
        max_total_risk_r=Decimal("10"),
        max_positions_per_symbol=1,
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
    """
    _check_total_risk интерпретирует количество позиций как суммарный риск в R.
    proposed_total_risk_r = len(open_positions) + 1
    должен быть <= max_total_risk_r, иначе reason='max_total_risk_r'.
    """
    from src.risk import risk_manager as risk_module

    async def fake_is_blocked(
        _redis,
        _symbol: str,
        _side: str,
        _now: datetime | None = None,
    ):
        return False, None

    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", fake_is_blocked)

    # 2 уже открыты, max_total_risk_r=2 → новая позиция даст 3R > 2R
    existing_positions = [make_position(), make_position()]
    position_repo_mock.list_open = AsyncMock(return_value=existing_positions)

    # per-base лимит пропускает
    def fake_can_open(
        positions: List[Position],
        symbol: str,
        direction: str,
        max_positions_per_base: int,
    ) -> bool:
        # чтобы не было warning-ов, слегка трогаем параметры
        assert len(positions) == 2
        assert symbol == "BTCUSDT"
        assert direction == "long"
        assert isinstance(max_positions_per_base, int)
        return True

    monkeypatch.setattr(risk_module, "can_open_position_for_base", fake_can_open)

    limits = make_limits(
        max_concurrent=10,
        max_total_risk_r=Decimal("2"),
        max_positions_per_symbol=10,
    )
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
    """
    Если для символа есть отдельный лимит per_symbol_risk_r и
    текущие позиции уже исчерпали его, check_limits должен вернуть
    reason='per_symbol_risk_r'.
    """
    from src.risk import risk_manager as risk_module

    async def fake_is_blocked(
        _redis,
        _symbol: str,
        _side: str,
        _now: datetime | None = None,
    ):
        return False, None

    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", fake_is_blocked)

    # Одна открытая позиция по BTCUSDT, лимит per_symbol_risk_r=1R
    existing_positions = [make_position(symbol="BTCUSDT")]
    position_repo_mock.list_open = AsyncMock(return_value=existing_positions)

    # per-base лимит пропускает
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
        return True

    monkeypatch.setattr(risk_module, "can_open_position_for_base", fake_can_open)

    limits = make_limits(
        max_concurrent=10,
        max_total_risk_r=Decimal("10"),
        max_positions_per_symbol=10,
        per_symbol_risk_r={"btcusdt": Decimal("1")},
    )
    rm = make_risk_manager(limits=limits, redis=redis_mock, position_repo=position_repo_mock)
    sig = make_signal(symbol="BTCUSDT")

    allowed, reason = await rm.check_limits(sig)

    assert allowed is False
    assert reason == "per_symbol_risk_r"
    position_repo_mock.list_open.assert_awaited_once()


# =====================================================================
# Позитивный сценарий: все проверки пройдены
# =====================================================================


@pytest.mark.asyncio
async def test_check_limits_allows_when_all_constraints_pass(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """
    Если anti-churn не блокирует, лимиты по количеству, total_risk и per-symbol
    не нарушены — check_limits возвращает (True, None).
    """
    from src.risk import risk_manager as risk_module

    async def fake_is_blocked(
        _redis,
        _symbol: str,
        _side: str,
        _now: datetime | None = None,
    ):
        return False, None

    monkeypatch.setattr(risk_module.AntiChurnGuard, "is_blocked", fake_is_blocked)

    existing_positions = [make_position(symbol="ETHUSDT")]  # другая бумага
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

    # Лимиты достаточно широкие
    limits = make_limits(
        max_concurrent=10,
        max_total_risk_r=Decimal("10"),
        max_positions_per_symbol=5,
        per_symbol_risk_r={"ETHUSDT": Decimal("3")},  # отдельный лимит для ETH, но не для BTC
    )
    rm = make_risk_manager(limits=limits, redis=redis_mock, position_repo=position_repo_mock)
    sig = make_signal(symbol="BTCUSDT")  # по BTC отдельного пер-символьного лимита нет

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
    """
    on_position_opened должен вызвать AntiChurnGuard.record_signal
    с символом и направлением позиции.
    """
    from src.risk import risk_manager as risk_module

    async_record = AsyncMock()

    monkeypatch.setattr(risk_module.AntiChurnGuard, "record_signal", async_record)

    rm = make_risk_manager(redis=redis_mock, position_repo=position_repo_mock)
    pos = make_position(symbol="BTCUSDT")

    await rm.on_position_opened(pos)

    async_record.assert_awaited_once()
    _args, kwargs = async_record.call_args
    # record_signal(redis, symbol=..., side=..., now=...)
    assert kwargs["redis"] is redis_mock
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["side"] == pos.direction


@pytest.mark.asyncio
async def test_on_position_closed_does_not_touch_anti_churn(
    monkeypatch: pytest.MonkeyPatch,
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """
    on_position_closed по текущей реализации ничего не делает с AntiChurnGuard —
    только логирует событие. Проверяем, что AntiChurnGuard.* не вызывается.
    """
    from src.risk import risk_manager as risk_module

    async_clear = AsyncMock()
    monkeypatch.setattr(risk_module.AntiChurnGuard, "clear_block", async_clear)

    rm = make_risk_manager(redis=redis_mock, position_repo=position_repo_mock)
    pos = make_position(symbol="BTCUSDT")

    await rm.on_position_closed(pos)

    async_clear.assert_not_awaited()


# =====================================================================
# update_limits
# =====================================================================


def test_update_limits_replaces_snapshot(
    redis_mock: AsyncMock,
    position_repo_mock: AsyncMock,
) -> None:
    """
    update_limits должен заменить внутренний снимок RiskLimits
    и свойство .limits должно возвращать обновлённые значения.
    """
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
