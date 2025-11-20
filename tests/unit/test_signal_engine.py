# tests/unit/test_signal_engine.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List
from uuid import UUID

import pytest
from unittest.mock import AsyncMock

from src.core.models import AVI5Config, ConfirmedCandle as Candle, Signal, TradingConfig
from src.strategies.avi5 import Avi5SignalEngine


# =====================================================================
# Вспомогательные фабрики
# =====================================================================


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
    """Конструктор простой 5-минутной свечи."""
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
    """Создать Avi5SignalEngine с мокнутым RiskManager."""
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
    # По умолчанию RiskManager пропускает сигнал
    risk_mgr.check_limits = AsyncMock(return_value=(True, None))

    engine = Avi5SignalEngine(
        avi5_config=avi_cfg,
        trading_config=trading_cfg,
        risk_manager=risk_mgr,
        strategy_version="avi5-test",
    )
    return engine, risk_mgr


# =====================================================================
# Базовые фильтры и ранние выходы
# =====================================================================


@pytest.mark.asyncio
async def test_generate_signal_returns_none_on_empty_candles() -> None:
    engine, risk_mgr = _mk_engine()
    result = await engine.generate_signal([])
    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_candle_must_be_confirmed() -> None:
    engine, risk_mgr = _mk_engine()

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles: List[Candle] = [
        _mk_candle(
            ts=now - timedelta(minutes=10),
            open_=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
        ),
        _mk_candle(
            ts=now - timedelta(minutes=5),
            open_=Decimal("11"),
            high=Decimal("12"),
            low=Decimal("10"),
            close=Decimal("11"),
            confirmed=False,  # последняя свеча не подтверждена
        ),
    ]

    result = await engine.generate_signal(candles)
    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_spread_filter_blocks_signal() -> None:
    engine, risk_mgr = _mk_engine()

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = [
        _mk_candle(
            ts=now - timedelta(minutes=10),
            open_=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
        ),
        _mk_candle(
            ts=now - timedelta(minutes=5),
            open_=Decimal("11"),
            high=Decimal("12"),
            low=Decimal("10"),
            close=Decimal("11"),
        ),
        _mk_candle(
            ts=now,
            open_=Decimal("12"),
            high=Decimal("13"),
            low=Decimal("11"),
            close=Decimal("12"),
        ),
    ]

    # spread_ok=False → должны выйти до RiskManager
    result = await engine.generate_signal(candles, spread_ok=False)
    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_funding_filter_blocks_signal_when_too_close() -> None:
    engine, risk_mgr = _mk_engine()

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = [
        _mk_candle(
            ts=now - timedelta(minutes=10),
            open_=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
        ),
        _mk_candle(
            ts=now - timedelta(minutes=5),
            open_=Decimal("11"),
            high=Decimal("12"),
            low=Decimal("10"),
            close=Decimal("11"),
        ),
        _mk_candle(
            ts=now,
            open_=Decimal("12"),
            high=Decimal("13"),
            low=Decimal("11"),
            close=Decimal("12"),
        ),
    ]

    # до funding меньше 15 минут → сигнал не генерируется
    result = await engine.generate_signal(candles, spread_ok=True, time_to_funding_minutes=10)
    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_enough_candles_for_atr_and_donchian() -> None:
    engine, risk_mgr = _mk_engine(atr_window=3)

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Нужно как минимум atr_window+1 свечей → даём меньше.
    candles = [
        _mk_candle(
            ts=now - timedelta(minutes=10),
            open_=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
        ),
        _mk_candle(
            ts=now - timedelta(minutes=5),
            open_=Decimal("11"),
            high=Decimal("12"),
            low=Decimal("10"),
            close=Decimal("11"),
        ),
    ]

    result = await engine.generate_signal(candles)
    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


# =====================================================================
# Ошибки индикаторов (atr / donchian)
# =====================================================================


@pytest.mark.asyncio
async def test_signal_engine_skips_on_atr_error(monkeypatch) -> None:
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(atr_window=2)

    async def fake_generate(*_args, **_kwargs):
        # сюда не должны дойти
        assert False, "RiskManager.check_limits should not be called"

    risk_mgr.check_limits = AsyncMock(side_effect=fake_generate)

    # Патчим atr так, чтобы он падал.
    def fake_atr(_candles, _period):
        raise RuntimeError("boom")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = [
        _mk_candle(
            ts=now - timedelta(minutes=10),
            open_=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
        ),
        _mk_candle(
            ts=now - timedelta(minutes=5),
            open_=Decimal("11"),
            high=Decimal("12"),
            low=Decimal("10"),
            close=Decimal("11"),
        ),
        _mk_candle(
            ts=now,
            open_=Decimal("12"),
            high=Decimal("13"),
            low=Decimal("11"),
            close=Decimal("12"),
        ),
    ]

    result = await engine.generate_signal(candles)
    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_signal_engine_skips_on_donchian_error(monkeypatch) -> None:
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(atr_window=2)

    def fake_atr(_candles, _period) -> Decimal:
        return Decimal("1")

    def fake_donchian(_candles, _window):
        raise RuntimeError("boom")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = [
        _mk_candle(
            ts=now - timedelta(minutes=10),
            open_=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
        ),
        _mk_candle(
            ts=now - timedelta(minutes=5),
            open_=Decimal("11"),
            high=Decimal("12"),
            low=Decimal("10"),
            close=Decimal("11"),
        ),
        _mk_candle(
            ts=now,
            open_=Decimal("12"),
            high=Decimal("13"),
            low=Decimal("11"),
            close=Decimal("12"),
        ),
    ]

    result = await engine.generate_signal(candles)
    assert result is None
    risk_mgr.check_limits.assert_not_awaited()


# =====================================================================
# Позитивный путь: успешная генерация сигнала
# =====================================================================


@pytest.mark.asyncio
async def test_generate_long_signal_and_passed_risk_manager(monkeypatch) -> None:
    """
    Позитивный сценарий:
      * atr и donchian дают корректные значения;
      * RiskManager разрешает сигнал;
      * на выходе — объект Signal с ожидаемыми полями.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    # Контролируем значения индикаторов.
    def fake_atr(_candles, _period) -> Decimal:
        return Decimal("10")  # 1R = 2 * 10 = 20

    def fake_donchian(_candles, _window):
        # upper/lover подобраны так, чтобы сработал long-триггер:
        # last.close > upper >= prev.close
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
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
        close=Decimal("106"),  # > upper=105
    )
    candles = [prev, last]

    result = await engine.generate_signal(candles, now=now, spread_ok=True)
    assert isinstance(result, Signal)

    # Проверяем базовые параметры сигнала.
    assert result.symbol == "BTCUSDT"
    assert result.direction == "long"
    assert result.entry_price == last.close

    # stake_usd = max_stake * theta = 100 * 0.3 = 30
    assert result.stake_usd == Decimal("30")

    # probability == theta (см. реализацию)
    assert result.probability == Decimal("0.3")

    # ATR=10, atr_multiplier=2 → risk_per_unit=20
    # long: SL = entry - 20, TP1/2/3 = entry + 20/40/60
    assert result.stop_loss == last.close - Decimal("20")
    assert result.tp1 == last.close + Decimal("20")
    assert result.tp2 == last.close + Decimal("40")
    assert result.tp3 == last.close + Decimal("60")

    # RiskManager.check_limits должен быть вызван один раз с нашим сигналом.
    risk_mgr.check_limits.assert_awaited_once()
    called_signal = risk_mgr.check_limits.call_args.kwargs.get("signal") or risk_mgr.check_limits.call_args.args[0]
    assert isinstance(called_signal, Signal)
    assert called_signal.id == result.id
    assert isinstance(result.id, UUID)


@pytest.mark.asyncio
async def test_signal_rejected_by_risk_manager(monkeypatch) -> None:
    """
    Если RiskManager возвращает allowed=False, generate_signal()
    должен вернуть None.
    """
    from src.strategies import avi5 as avi5_module

    engine, risk_mgr = _mk_engine(theta=0.3, atr_window=2, atr_multiplier=2.0)

    def fake_atr(_candles, _period) -> Decimal:
        return Decimal("5")

    def fake_donchian(_candles, _window):
        return Decimal("105"), Decimal("95")

    monkeypatch.setattr(avi5_module, "atr", fake_atr)
    monkeypatch.setattr(avi5_module, "donchian", fake_donchian)

    # RiskManager отклоняет сигнал
    risk_mgr.check_limits = AsyncMock(return_value=(False, "limit-exceeded"))

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
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
    candles = [prev, last]

    result = await engine.generate_signal(candles, now=now, spread_ok=True)
    assert result is None
    risk_mgr.check_limits.assert_awaited_once()
