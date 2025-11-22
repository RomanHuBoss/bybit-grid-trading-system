from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional, cast
from uuid import uuid4

import pytest
from unittest.mock import AsyncMock

from src.core.exceptions import ExecutionError
from src.core.models import Position, Signal
from src.db.repositories.position_repository import PositionRepository
from src.execution.order_manager import OrderManager
from src.integration.bybit.rest_client import BybitRESTClient

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
    чтобы статический анализатор не поднимал предупреждения и
    модель соответствовала схемам проекта.
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

    Нам нужен только create(); остальное для этих интеграционных тестов
    несущественно.
    """

    def __init__(self) -> None:
        self.positions: List[Position] = []

    async def create(self, position: Position) -> Position:
        self.positions.append(position)
        return position


def _make_order_manager(
    *,
    position_repo: InMemoryPositionRepository,
) -> OrderManager:
    """
    Фабрика OrderManager с подменённым REST-клиентом и in-memory репозиторием.

    Через typing.cast явно говорим анализатору типов, что наши in-memory
    реализации удовлетворяют интерфейсу PositionRepository.
    """
    rest_client = AsyncMock(spec=BybitRESTClient)
    # По позиционным аргументам, чтобы не зависеть от имён параметров __init__.
    manager = OrderManager(
        rest_client,
        cast(PositionRepository, position_repo),
    )
    # Делаем REST-клиент доступным из теста (для assert’ов).
    manager._rest = rest_client  # type: ignore[attr-defined]
    return manager


# ======================================================================
# Тесты open_position: happy-path
# ======================================================================


async def test_open_position_happy_path_creates_position() -> None:
    """
    Полный happy-path:
      * есть валидный сигнал;
      * Bybit возвращает orderId, avgPrice и cumExecQty;
      * OrderManager создаёт Position и сохраняет её в репозиторий.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(created_at=now)

    position_repo = InMemoryPositionRepository()
    manager = _make_order_manager(position_repo=position_repo)

    # Bybit create-order → успешный ответ с orderId, avgPrice и cumExecQty.
    manager._rest.request.return_value = {  # type: ignore[attr-defined]
        "result": {
            "orderId": "test-order-id",
            "avgPrice": "50010",
            "cumExecQty": "0.0002",
        },
    }

    position = await manager.open_position(signal)

    assert isinstance(position, Position)
    # Базовые инварианты позиции
    assert position.symbol == signal.symbol
    assert position.direction == signal.direction
    assert position.entry_price == Decimal("50010")
    assert position.size_base > 0
    assert position.size_quote > 0

    # Позиция действительно записана в репозиторий
    assert len(position_repo.positions) == 1
    assert position_repo.positions[0].id == position.id

    # REST-запрос на создание ордера был ровно один раз
    manager._rest.request.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs: dict[str, Any] = manager._rest.request.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["method"] == "POST"
    # Не завязываемся на начальный слеш — только на то, что это create-order.
    assert "order/create" in kwargs["path"]


# ======================================================================
# Ошибки на этапе create_order в Bybit
# ======================================================================


async def test_open_position_missing_order_id_raises_execution_error() -> None:
    """
    Если в ответе Bybit нет result.orderId — это ошибка,
    оформляемая через ExecutionError.
    """
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal(created_at=now)

    position_repo = InMemoryPositionRepository()
    manager = _make_order_manager(position_repo=position_repo)

    # Ответ Bybit без orderId.
    manager._rest.request.return_value = {  # type: ignore[attr-defined]
        "result": {},
    }

    with pytest.raises(ExecutionError) as excinfo:
        await manager.open_position(signal)

    msg = str(excinfo.value).lower()
    assert "orderid" in msg or "order id" in msg

    # Позиция не должна быть создана
    assert position_repo.positions == []
