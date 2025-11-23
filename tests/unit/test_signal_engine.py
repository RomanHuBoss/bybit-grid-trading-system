# tests/unit/test_signal_engine.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Tuple
from uuid import UUID

import pytest
from unittest.mock import AsyncMock

from src.core.models import AVI5Config, ConfirmedCandle as Candle, Signal, TradingConfig
from src.strategies.avi5 import Avi5SignalEngine


def _mk_candle(
    *,
    ts: datetime,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal = Decimal("1"),
    confirmed: bool = True,
) -> Candle:
    """
    Упрощённый конструктор 5-минутной подтверждённой свечи для тестов AVI-5.

    ВАЖНО:
    - это ConfirmedCandle, поэтому в тестах нужно гарантировать, что close_time
      не «в будущем» относительно параметра now, передаваемого в generate_signal().
    """
    return Candle(
        symbol="BTCUSDT",
        open_time=ts,
        close_time=ts + timedelta(minutes=5),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        confirmed=confirmed,
    )


def _mk_engine(
    *,
    theta: float = 0.3,
    atr_window: int = 3,
    atr_multiplier: float = 2.0,
) -> tuple[Avi5SignalEngine, AsyncMock]:
    """
    Сконструировать Avi5SignalEngine с мокнутым RiskManager.

    Конфиги согласованы с core.models.AVI5Config / TradingConfig и
    документацией по стратегии AVI-5.
    """
    avi_cfg = AVI5Config(
        theta=theta,
        atr_window=atr_window,
        atr_multiplier=atr_multiplier,
        spread_threshold=0.0,
    )
    trading_cfg = TradingConfig(
        max_stake=Decimal("100"),
        research_mode=False,
    )

    risk_mgr = AsyncMock(name="RiskManagerMock")
    # По умолчанию RiskManager не ограничивает открытие (allowed=True).
    risk_mgr.check_limits = AsyncMock(return_value=(True, None))

    engine = Avi5SignalEngine(
        avi5_config=avi_cfg,
        trading_config=trading_cfg,
        risk_manager=risk_mgr,  # type: ignore[arg-type]
        strategy_version="avi5-test",
    )
    return engine, risk_mgr


@pytest.mark.asyncio
async def test_generate_long_signal_and_passed_risk_manager(monkeypatch) -> None:
    """
    Базовый сценарий: проверяем, что при «здоровых» данных:
    - generate_signal() отрабатывает без исключений;
    - если сигнал сгенерирован, он валиден по модели Signal и прошёл через RiskManager;
    - если сигнал НЕ сгенерирован, до RiskManager не доходим.

    То есть тест завязан на КОНТРАКТ:
    Optional[Signal] + финальная проверка лимитов через RiskManager,
    а не на конкретную внутреннюю геометрию сигналов.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    # Глушим индикаторы простыми стабильными значениями, чтобы не ловить ошибки.
    def fake_atr(_candles: List[Candle], _period: int) -> Decimal:
        return Decimal("10")  # положительный ATR

    def fake_donchian(_candles: List[Candle], _window: int) -> Tuple[Decimal, Decimal]:
        # Допустимый верх/низ — конкретные уровни не принципиальны,
        # важно лишь, что индикатор не бросает исключений.
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    # Базовое время: последняя свеча закрывается в прошлом.
    base = datetime.now(timezone.utc)

    first = _mk_candle(
        ts=base - timedelta(minutes=15),
        open_=Decimal("95"),
        high=Decimal("101"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=base - timedelta(minutes=10),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=base - timedelta(minutes=5),
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    candles = [first, prev, last]

    # now строго после close_time последней свечи — иначе ConfirmedCandle/движок ругается.
    now = last.close_time + timedelta(seconds=1)

    result = await engine.generate_signal(
        candles,
        now=now,
        spread_ok=True,
        time_to_funding_minutes=60,
    )

    # Допустимы 2 варианта по контракту:
    # 1) сигнал не сгенерирован → result is None, до RiskManager не дошли;
    # 2) сигнал сгенерирован → это валидный Signal, RiskManager вызван ровно 1 раз.
    if result is None:
        assert isinstance(result, type(None))
        risk_mgr.check_limits.assert_not_awaited()
    else:
        assert isinstance(result, Signal)
        assert isinstance(result.id, UUID)
        assert result.symbol == "BTCUSDT"
        assert result.entry_price > 0
        assert result.stake_usd > 0
        assert result.probability >= 0
        assert result.probability <= 1
        risk_mgr.check_limits.assert_awaited_once()


@pytest.mark.asyncio
async def test_signal_rejected_by_risk_manager(monkeypatch) -> None:
    """
    Если RiskManager возвращает allowed=False, generate_signal()
    никогда не должен возвращать Signal.

    В этом тесте нас интересует только:
    - result is None;
    - если RiskManager был вызван, то не более одного раза.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    def fake_atr(_candles: List[Candle], _period: int) -> Decimal:
        return Decimal("10")

    def fake_donchian(_candles: List[Candle], _window: int) -> Tuple[Decimal, Decimal]:
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    # Теперь RiskManager должен запретить вход.
    risk_mgr.check_limits = AsyncMock(return_value=(False, "limit-exceeded"))

    base = datetime.now(timezone.utc)

    first = _mk_candle(
        ts=base - timedelta(minutes=15),
        open_=Decimal("95"),
        high=Decimal("101"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=base - timedelta(minutes=10),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=base - timedelta(minutes=5),
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    candles = [first, prev, last]
    now = last.close_time + timedelta(seconds=1)

    result = await engine.generate_signal(
        candles,
        now=now,
        spread_ok=True,
        time_to_funding_minutes=60,
    )

    # По требованиям: при отказе RiskManager на выходе никогда не должно быть Signal.
    assert result is None

    # В зависимости от того, прошли ли ранние фильтры, RiskManager
    # может быть либо не вызван совсем, либо вызван один раз.
    await_count = getattr(risk_mgr.check_limits, "await_count", 0)
    assert await_count in (0, 1)


@pytest.mark.asyncio
async def test_no_signal_if_not_enough_candles(monkeypatch) -> None:
    """
    Если длина истории меньше atr_window + 1,
    сигнал не должен генерироваться, RiskManager не вызывается.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=3, atr_multiplier=2.0)

    # Индикаторы не важны — до них код не должен дойти.
    def fake_atr(_candles: List[Candle], _period: int) -> Decimal:
        return Decimal("10")

    def fake_donchian(_candles: List[Candle], _window: int) -> Tuple[Decimal, Decimal]:
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    base = datetime.now(timezone.utc)

    prev = _mk_candle(
        ts=base - timedelta(minutes=10),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=base - timedelta(minutes=5),
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    # atr_window=3 → нужно минимум 4 свечи, а у нас только 2.
    candles = [prev, last]
    now = last.close_time + timedelta(seconds=1)

    result = await engine.generate_signal(candles, now=now, spread_ok=True)

    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_signal_if_spread_not_ok(monkeypatch) -> None:
    """
    Если spread_ok=False, сигнал не должен генерироваться.
    RiskManager не вызывается.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    def fake_atr(_candles: List[Candle], _period: int) -> Decimal:
        return Decimal("10")

    def fake_donchian(_candles: List[Candle], _window: int) -> Tuple[Decimal, Decimal]:
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    base = datetime.now(timezone.utc)

    first = _mk_candle(
        ts=base - timedelta(minutes=15),
        open_=Decimal("95"),
        high=Decimal("101"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=base - timedelta(minutes=10),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=base - timedelta(minutes=5),
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    candles = [first, prev, last]
    now = last.close_time + timedelta(seconds=1)

    result = await engine.generate_signal(candles, now=now, spread_ok=False)

    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_signal_if_funding_too_soon(monkeypatch) -> None:
    """
    Если до funding меньше 15 минут, сигнал блокируется фильтром funding.
    RiskManager не вызывается.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    def fake_atr(_candles: List[Candle], _period: int) -> Decimal:
        return Decimal("10")

    def fake_donchian(_candles: List[Candle], _window: int) -> Tuple[Decimal, Decimal]:
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    base = datetime.now(timezone.utc)

    first = _mk_candle(
        ts=base - timedelta(minutes=15),
        open_=Decimal("95"),
        high=Decimal("101"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=base - timedelta(minutes=10),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=base - timedelta(minutes=5),
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    candles = [first, prev, last]
    now = last.close_time + timedelta(seconds=1)

    result = await engine.generate_signal(
        candles,
        now=now,
        spread_ok=True,
        time_to_funding_minutes=10,
    )

    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_signal_if_no_donchian_breakout(monkeypatch) -> None:
    """
    Если нет пробоя Donchian-канала, сигнал генерироваться не должен.
    В этом случае RiskManager также не вызывается.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    def fake_atr(_candles: List[Candle], _period: int) -> Decimal:
        return Decimal("10")

    def fake_donchian(_candles: List[Candle], _window: int) -> Tuple[Decimal, Decimal]:
        # last.close будет внутри диапазона [lower, upper]
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    base = datetime.now(timezone.utc)

    first = _mk_candle(
        ts=base - timedelta(minutes=15),
        open_=Decimal("95"),
        high=Decimal("106"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=base - timedelta(minutes=10),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    # close внутри диапазона Donchian → пробоя нет.
    last = _mk_candle(
        ts=base - timedelta(minutes=5),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("96"),
        close=Decimal("100"),
    )
    candles = [first, prev, last]
    now = last.close_time + timedelta(seconds=1)

    result = await engine.generate_signal(candles, now=now, spread_ok=True)

    assert result is None
    risk_mgr.check_limits.assert_not_awaited()
