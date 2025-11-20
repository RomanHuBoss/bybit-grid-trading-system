# tests/integration/test_order_lifecycle.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, List, Optional, Tuple, cast
from uuid import UUID, uuid4

import pytest
from unittest.mock import AsyncMock

from src.core.exceptions import OrderPlacementError
from src.core.models import Position, Signal
from src.db.repositories.position_repository import PositionRepository
from src.db.repositories.signal_repository import SignalRepository
from src.execution.order_manager import OrderManager, PartialFillPolicy
from src.risk.risk_manager import RiskManager

pytestmark = pytest.mark.asyncio


# ======================================================================
# Вспомогательные фабрики / фейки
# ======================================================================


def _make_signal(
    *,
    symbol: str = "BTCUSDT",
    direction: str = "long",
    entry_price: Decimal = Decimal("50000"),
    stake_usd: Decimal = Decimal("10"),
    created_at: Optional[datetime] = None,
) -> Signal:
    """
    Фабрика валидного сигнала для тестов.

    ВАЖНО: явно заполняем strategy / error_code / error_message,
    чтобы статический анализатор не поднимал предупреждения.
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc)

    return Signal(
        id=uuid4(),
        created_at=created_at,
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stake_usd=stake_usd,
        probability=Decimal("0.65"),
        strategy="AVI-5",
        strategy_version="avi5-test",
        queued_until=None,
        tp1=entry_price + Decimal("500"),
        tp2=entry_price + Decimal("1000"),
        tp3=entry_price + Decimal("2000"),
        stop_loss=entry_price - Decimal("500"),
        error_code=None,
        error_message=None,
    )


class InMemoryPositionRepository:
    """
    Минимальный in-memory репозиторий позиций.

    Нам нужно только create(); остальное для этого теста не важно.
    """

    def __init__(self) -> None:
        self.positions: List[Position] = []

    async def create(self, position: Position) -> Position:
        self.positions.append(position)
        return position


class InMemorySignalRepository:
    """
    Минимальный репозиторий сигналов для OrderManager.

    get_by_id — отдаёт ровно один сигнал (или None),
    update_error — пишет запись в журнал updated_errors.
    """

    def __init__(self, signal: Optional[Signal]) -> None:
        self._signal = signal
        self.updated_errors: List[Tuple[UUID, Optional[int], str]] = []

    async def get_by_id(self, signal_id: UUID) -> Optional[Signal]:
        if self._signal is not None and self._signal.id == signal_id:
            return self._signal
        return None

    async def update_error(
        self,
        signal_id: UUID,
        error_code: Optional[int] = None,
        error_message: str = "",
    ) -> None:
        self.updated_errors.append((signal_id, error_code, error_message))


class FakeRiskManager:
    """
    Управляемый RiskManager для интеграционных тестов OrderManager.

    check_limits() всегда возвращает заранее заданный результат.
    """

    def __init__(self, allowed: bool, reason: Optional[str]) -> None:
        self._allowed = allowed
        self._reason = reason
        self.calls: List[Signal] = []

    async def check_limits(
        self,
        signal: Signal,
        *,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[str]]:
        # слегка используем now, чтобы линтер не ругался на неиспользуемый параметр
        _ = now
        self.calls.append(signal)
        return self._allowed, self._reason


def _make_order_manager(
    *,
    signal_repo: InMemorySignalRepository,
    position_repo: InMemoryPositionRepository,
    risk_manager: FakeRiskManager,
) -> OrderManager:
    """
    Фабрика OrderManager с подменёнными инфраструктурными зависимостями.

    Здесь через typing.cast явно говорим анализатору типов, что наши
    in-memory реализации удовлетворяют интерфейсам PositionRepository /
    SignalRepository / RiskManager.
    """
    rest_client = AsyncMock()
    rest_client.request = AsyncMock()

    manager = OrderManager(
        bybit_rest_client=rest_client,
        position_repository=cast(PositionRepository, position_repo),
        signal_repository=cast(SignalRepository, signal_repo),
        risk_manager=cast(RiskManager, risk_manager),
        partial_fill_policy=PartialFillPolicy(
            min_fill_ratio_to_open=Decimal("0.5"),
            full_fill_ratio=Decimal("0.95"),
        ),
        order_timeout=30,
        poll_interval=0.01,  # быстрый опрос в тестах
        signal_grace_seconds=5,
    )
    return manager


# ======================================================================
# Тесты place_order: happy-path
# ======================================================================


async def test_place_order_happy_path_opens_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Полный happy-path:
      * сигнал свежий;
      * RiskManager пропускает;
      * Bybit отдаёт orderId;
      * wait_for_fills возвращает полное исполнение.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(created_at=now)

    signal_repo = InMemorySignalRepository(signal)
    position_repo = InMemoryPositionRepository()
    risk_manager = FakeRiskManager(allowed=True, reason=None)

    manager = _make_order_manager(
        signal_repo=signal_repo,
        position_repo=position_repo,
        risk_manager=risk_manager,
    )

    # Bybit create-order → успешный ответ с orderId
    manager._rest.request.return_value = {  # type: ignore[attr-defined]
        "result": {"orderId": "test-order-id"},
    }

    # Подменяем wait_for_fills на корутину, возвращающую полный fill.
    fake_wait = AsyncMock(return_value=(Decimal("1"), Decimal("50010"), "FILLED"))
    monkeypatch.setattr(manager, "wait_for_fills", fake_wait)

    position = await manager.place_order(signal.id, now=now)

    assert isinstance(position, Position)
    assert position.signal_id == signal.id
    assert position.fill_ratio == Decimal("1")
    assert position.entry_price == Decimal("50010")

    # Позиция действительно записана в репозиторий
    assert len(position_repo.positions) == 1
    assert position_repo.positions[0].id == position.id

    # REST-запрос на создание ордера был
    manager._rest.request.assert_awaited()  # type: ignore[attr-defined]
    kwargs: dict[str, Any] = manager._rest.request.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["method"] == "POST"
    assert kwargs["path"] == "/v5/order/create"


# ======================================================================
# Ошибки на ранних стадиях: сигнал / риск
# ======================================================================


async def test_place_order_signal_not_found_raises_without_update_error() -> None:
    """
    Если сигнал не найден — OrderPlacementError и никакого update_error,
    потому что обновлять нечего.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    missing_id = uuid4()

    signal_repo = InMemorySignalRepository(signal=None)
    position_repo = InMemoryPositionRepository()
    risk_manager = FakeRiskManager(allowed=True, reason=None)

    manager = _make_order_manager(
        signal_repo=signal_repo,
        position_repo=position_repo,
        risk_manager=risk_manager,
    )

    with pytest.raises(OrderPlacementError) as excinfo:
        await manager.place_order(missing_id, now=now)

    assert "not found" in str(excinfo.value).lower()
    assert signal_repo.updated_errors == []


async def test_place_order_expired_signal_updates_error_and_raises() -> None:
    """
    Просроченный сигнал:
      * не уходит в RiskManager/Bybit;
      * в signal.update_error пишется человеческое сообщение;
      * place_order поднимает OrderPlacementError.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    created_at = now - timedelta(seconds=100)
    signal = _make_signal(created_at=created_at)

    signal_repo = InMemorySignalRepository(signal)
    position_repo = InMemoryPositionRepository()
    risk_manager = FakeRiskManager(allowed=True, reason=None)

    manager = _make_order_manager(
        signal_repo=signal_repo,
        position_repo=position_repo,
        risk_manager=risk_manager,
    )

    with pytest.raises(OrderPlacementError) as excinfo:
        await manager.place_order(signal.id, now=now)

    msg = str(excinfo.value).lower()
    assert "expired" in msg

    # Ошибка записана в сигнал
    assert len(signal_repo.updated_errors) == 1
    err_signal_id, err_code, err_msg = signal_repo.updated_errors[0]
    assert err_signal_id == signal.id
    assert err_code is None
    assert "Signal expired for manual order placement" in err_msg

    # REST-клиент не должен был вызываться
    manager._rest.request.assert_not_awaited()  # type: ignore[attr-defined]


async def test_place_order_risk_rejected_updates_error_and_raises() -> None:
    """
    При отказе RiskManager:
      * не делаем REST-запрос;
      * пишем ошибку в сигнал;
      * бросаем OrderPlacementError.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(created_at=now)

    signal_repo = InMemorySignalRepository(signal)
    position_repo = InMemoryPositionRepository()
    risk_manager = FakeRiskManager(allowed=False, reason="too_many_positions")

    manager = _make_order_manager(
        signal_repo=signal_repo,
        position_repo=position_repo,
        risk_manager=risk_manager,
    )

    with pytest.raises(OrderPlacementError) as excinfo:
        await manager.place_order(signal.id, now=now)

    msg = str(excinfo.value)
    assert "Order rejected by RiskManager" in msg

    assert len(signal_repo.updated_errors) == 1
    err_signal_id, err_code, err_msg = signal_repo.updated_errors[0]
    assert err_signal_id == signal.id
    assert err_code is None
    assert "Order rejected by RiskManager: too_many_positions" in err_msg

    manager._rest.request.assert_not_awaited()  # type: ignore[attr-defined]


# ======================================================================
# Ошибки на этапе create_order в Bybit
# ======================================================================


async def test_place_order_rest_error_on_create_updates_error_and_raises() -> None:
    """
    Любая ошибка при create_order (RuntimeError и пр.) должна быть
    обёрнута в OrderPlacementError + записана в сигнал.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(created_at=now)

    signal_repo = InMemorySignalRepository(signal)
    position_repo = InMemoryPositionRepository()
    risk_manager = FakeRiskManager(allowed=True, reason=None)

    manager = _make_order_manager(
        signal_repo=signal_repo,
        position_repo=position_repo,
        risk_manager=risk_manager,
    )

    manager._rest.request.side_effect = RuntimeError("boom")  # type: ignore[attr-defined]

    with pytest.raises(OrderPlacementError) as excinfo:
        await manager.place_order(signal.id, now=now)

    msg = str(excinfo.value)
    assert "Failed to create Bybit order" in msg

    assert len(signal_repo.updated_errors) == 1
    err_signal_id, err_code, err_msg = signal_repo.updated_errors[0]
    assert err_signal_id == signal.id
    assert err_code is None
    assert "Failed to create Bybit order:" in err_msg


async def test_place_order_missing_order_id_updates_error_and_raises() -> None:
    """
    Если в ответе Bybit нет result.orderId — это ошибка,
    оформляемая через OrderPlacementError и update_error.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(created_at=now)

    signal_repo = InMemorySignalRepository(signal)
    position_repo = InMemoryPositionRepository()
    risk_manager = FakeRiskManager(allowed=True, reason=None)

    manager = _make_order_manager(
        signal_repo=signal_repo,
        position_repo=position_repo,
        risk_manager=risk_manager,
    )

    manager._rest.request.return_value = {"result": {}}  # type: ignore[attr-defined]

    with pytest.raises(OrderPlacementError) as excinfo:
        await manager.place_order(signal.id, now=now)

    msg = str(excinfo.value).lower()
    assert "missing orderid" in msg

    assert len(signal_repo.updated_errors) == 1
    err_signal_id, err_code, err_msg = signal_repo.updated_errors[0]
    assert err_signal_id == signal.id
    assert err_code is None
    assert "Bybit create_order response missing orderId" in err_msg


# ======================================================================
# Underfill: частичное исполнение ниже порога открытия
# ======================================================================


async def test_place_order_underfill_cancels_order_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Если fill_ratio < min_fill_ratio_to_open:
      * вызываем _cancel_order;
      * ошибку пишем в сигнал;
      * позиция не создаётся;
      * place_order поднимает OrderPlacementError.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(created_at=now)

    signal_repo = InMemorySignalRepository(signal)
    position_repo = InMemoryPositionRepository()
    risk_manager = FakeRiskManager(allowed=True, reason=None)

    manager = _make_order_manager(
        signal_repo=signal_repo,
        position_repo=position_repo,
        risk_manager=risk_manager,
    )

    manager._rest.request.return_value = {  # type: ignore[attr-defined]
        "result": {"orderId": "test-order-id"},
    }

    # wait_for_fills → PARTIALLY_FILLED с низким fill_ratio
    fake_wait = AsyncMock(
        return_value=(Decimal("0.1"), signal.entry_price, "PARTIALLY_FILLED")
    )
    monkeypatch.setattr(manager, "wait_for_fills", fake_wait)

    # _cancel_order тоже подменяем, чтобы не дёргать реальный REST
    fake_cancel = AsyncMock()
    monkeypatch.setattr(manager, "_cancel_order", fake_cancel)

    with pytest.raises(OrderPlacementError) as excinfo:
        await manager.place_order(signal.id, now=now)

    msg = str(excinfo.value)
    assert "Order underfilled" in msg

    fake_cancel.assert_awaited_once()
    assert position_repo.positions == []

    assert len(signal_repo.updated_errors) == 1
    err_signal_id, err_code, err_msg = signal_repo.updated_errors[0]
    assert err_signal_id == signal.id
    assert err_code is None
    assert "Order underfilled: fill_ratio=" in err_msg
