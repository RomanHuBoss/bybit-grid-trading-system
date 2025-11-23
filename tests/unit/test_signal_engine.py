# tests/unit/test_signal_engine.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from typing import List, Tuple

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
    Упрощённый конструктор 5-минутной свечи для тестов AVI-5.
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

    Важно: конфиг по умолчанию согласован с core.models.AVI5Config и
    текущей реализацией Avi5SignalEngine.
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
    # По умолчанию RiskManager пропускает сигнал.
    risk_mgr.check_limits = AsyncMock(return_value=(True, None))

    engine = Avi5SignalEngine(
        avi5_config=avi_cfg,
        trading_config=trading_cfg,
        risk_manager=risk_mgr,
        strategy_version="avi5-test",
    )
    return engine, risk_mgr


@pytest.mark.asyncio
async def test_generate_long_signal_and_passed_risk_manager(monkeypatch) -> None:
    """
    Позитивный сценарий:
    * есть достаточная история свечей (atr_window + 1);
    * есть пробой верхней границы Donchian;
    * RiskManager разрешает сигнал;
    * на выходе получаем корректный объект Signal.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    # Контролируем значения индикаторов.
    def fake_atr(_candles: List[Candle], _period: int) -> Decimal:
        return Decimal("10")  # 1R = 2 * 10 = 20

    def fake_donchian(_candles: List[Candle], _window: int) -> Tuple[Decimal, Decimal]:
        # upper/lower подобраны так, чтобы сработал long-триггер:
        # last.close > upper >= prev.close
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    now = datetime.now(timezone.utc)

    # Нужно минимум atr_window + 1 свечей → 3 штуки при atr_window=2.
    first = _mk_candle(
        ts=now - timedelta(minutes=10),
        open_=Decimal("95"),
        high=Decimal("101"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=now - timedelta(minutes=5),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=now,
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    candles = [first, prev, last]

    result = await engine.generate_signal(candles, now=now, spread_ok=True)

    # Убедимся, что сигнал действительно сгенерирован.
    assert isinstance(result, Signal)

    # Базовые параметры сигнала.
    assert result.symbol == "BTCUSDT"
    assert result.direction == "long"
    assert result.entry_price == last.close

    # stake_usd = max_stake * theta = 100 * 0.3 = 30
    assert result.stake_usd == Decimal("30")

    # probability == theta (см. реализацию Avi5SignalEngine).
    assert result.probability == Decimal("0.3")

    # ATR = 10, atr_multiplier = 2 → risk_per_unit = 20
    # long: SL = entry - 20, TP1/2/3 = entry + 20/40/60
    assert result.stop_loss == last.close - Decimal("20")
    assert result.tp1 == last.close + Decimal("20")
    assert result.tp2 == last.close + Decimal("40")
    assert result.tp3 == last.close + Decimal("60")

    # RiskManager.check_limits должен быть вызван один раз с нашим сигналом.
    risk_mgr.check_limits.assert_awaited_once()
    called_signal = (
        risk_mgr.check_limits.call_args.kwargs.get("signal")
        or risk_mgr.check_limits.call_args.args[0]
    )
    assert isinstance(called_signal, Signal)
    assert called_signal.id == result.id
    assert isinstance(result.id, UUID)


@pytest.mark.asyncio
async def test_signal_rejected_by_risk_manager(monkeypatch) -> None:
    """
    Если RiskManager возвращает allowed=False, generate_signal()
    должен вернуть None, при этом RiskManager обязательно вызывается.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    def fake_atr(_candles: List[Candle], _period: int) -> Decimal:
        return Decimal("5")

    def fake_donchian(_candles: List[Candle], _window: int) -> Tuple[Decimal, Decimal]:
        # Аналогичный сценарий пробоя вверх, как в позитивном тесте.
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    # Теперь RiskManager должен отклонить сигнал.
    risk_mgr.check_limits = AsyncMock(return_value=(False, "limit-exceeded"))

    now = datetime.now(timezone.utc)

    first = _mk_candle(
        ts=now - timedelta(minutes=10),
        open_=Decimal("95"),
        high=Decimal("101"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=now - timedelta(minutes=5),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=now,
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    candles = [first, prev, last]

    result = await engine.generate_signal(candles, now=now, spread_ok=True)

    # Сигнал должен быть отклонён после проверки RiskManager.
    assert result is None
    risk_mgr.check_limits.assert_awaited_once()


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

    now = datetime.now(timezone.utc)

    prev = _mk_candle(
        ts=now - timedelta(minutes=5),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=now,
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    # atr_window=3 → нужно минимум 4 свечи, а у нас только 2.
    candles = [prev, last]

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

    now = datetime.now(timezone.utc)

    first = _mk_candle(
        ts=now - timedelta(minutes=10),
        open_=Decimal("95"),
        high=Decimal("101"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=now - timedelta(minutes=5),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=now,
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    candles = [first, prev, last]

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

    now = datetime.now(timezone.utc)

    first = _mk_candle(
        ts=now - timedelta(minutes=10),
        open_=Decimal("95"),
        high=Decimal("101"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=now - timedelta(minutes=5),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    last = _mk_candle(
        ts=now,
        open_=Decimal("104"),
        high=Decimal("110"),
        low=Decimal("103"),
        close=Decimal("106"),
    )
    candles = [first, prev, last]

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
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    def fake_atr(_candles: List[Candle], _period: int) -> Decimal:
        return Decimal("10")

    def fake_donchian(_candles: List[Candle], _window: int) -> Tuple[Decimal, Decimal]:
        # last.close находится внутри канала → пробоя нет.
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    now = datetime.now(timezone.utc)

    first = _mk_candle(
        ts=now - timedelta(minutes=10),
        open_=Decimal("95"),
        high=Decimal("106"),
        low=Decimal("94"),
        close=Decimal("100"),
    )
    prev = _mk_candle(
        ts=now - timedelta(minutes=5),
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    # close внутри диапазона [lower, upper]
    last = _mk_candle(
        ts=now,
        open_=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("96"),
        close=Decimal("100"),
    )
    candles = [first, prev, last]

    result = await engine.generate_signal(candles, now=now, spread_ok=True)

    assert result is None
    risk_mgr.check_limits.assert_not_awaited()
