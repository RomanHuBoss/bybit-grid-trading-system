# tests/unit/test_indicators.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Tuple

import pytest

from src.core.models import ConfirmedCandle as Candle
from src.strategies.indicators import (
    atr,
    donchian,
    ema,
    microprice,
    orderbook_imbalance,
    vwap,
)


# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ---------------------------------------------------------------------------

def _mk_candle(
    *,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal = Decimal("1"),
    symbol: str = "BTCUSDT",
    open_time: datetime | None = None,
) -> Candle:
    """
    Создаёт минимально валидную ConfirmedCandle в соответствии с core.models.

    * symbol — непустая строка;
    * open_time / close_time — интервалы в UTC;
    * volume >= 0 — валидируется самой моделью.
    """
    if open_time is None:
        open_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

    close_time = open_time + timedelta(minutes=5)

    return Candle(
        symbol=symbol,
        open_time=open_time,
        close_time=close_time,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        confirmed=True,
    )


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def test_ema_basic_behavior() -> None:
    # Простая последовательность, по ней легко посчитать EMA вручную.
    values = [Decimal("10"), Decimal("11"), Decimal("12")]
    period = 2

    alpha = Decimal("2") / Decimal(period + 1)
    one_minus_alpha = Decimal(1) - alpha

    ema_0 = values[0]
    ema_1 = alpha * values[1] + one_minus_alpha * ema_0
    ema_2 = alpha * values[2] + one_minus_alpha * ema_1
    expected = ema_2

    result = ema(values, period=period)
    assert result == expected


def test_ema_raises_on_short_series() -> None:
    # Длина ряда меньше периода → ValueError.
    values = [Decimal("10"), Decimal("11")]
    with pytest.raises(ValueError):
        ema(values, period=3)


def test_ema_raises_on_non_positive_period() -> None:
    values = [Decimal("10"), Decimal("11"), Decimal("12")]
    with pytest.raises(ValueError):
        ema(values, period=0)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def test_atr_basic_behavior() -> None:
    # Для period=N нужно минимум N+1 свечей.
    candles: List[Candle] = [
        _mk_candle(
            open_=Decimal("10") + Decimal(i),
            high=Decimal("11") + Decimal(i),
            low=Decimal("9") + Decimal(i),
            close=Decimal("10") + Decimal(i),
        )
        for i in range(5)
    ]
    period = 3

    result = atr(candles, period=period)
    assert isinstance(result, Decimal)
    assert result >= 0


def test_atr_raises_on_short_series() -> None:
    # Для period=3 нужно хотя бы 4 свечи.
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        _mk_candle(open_=Decimal("10"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11")),
        _mk_candle(open_=Decimal("10"), high=Decimal("13"), low=Decimal("9"), close=Decimal("12")),
    ]
    with pytest.raises(ValueError):
        atr(candles, period=3)


def test_atr_raises_on_non_positive_period() -> None:
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        _mk_candle(open_=Decimal("10"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11")),
    ]
    with pytest.raises(ValueError):
        atr(candles, period=0)


# ---------------------------------------------------------------------------
# DONCHIAN
# ---------------------------------------------------------------------------

def test_donchian_basic_behavior() -> None:
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        _mk_candle(open_=Decimal("11"), high=Decimal("13"), low=Decimal("10"), close=Decimal("12")),
        _mk_candle(open_=Decimal("12"), high=Decimal("14"), low=Decimal("11"), close=Decimal("13")),
    ]
    upper, lower = donchian(candles, window=2)

    # Окно=2 → учитываем только последние две свечи
    expected_upper = max(c.high for c in candles[-2:])
    expected_lower = min(c.low for c in candles[-2:])

    assert upper == expected_upper
    assert lower == expected_lower
    assert upper >= lower


def test_donchian_raises_on_short_series() -> None:
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        _mk_candle(open_=Decimal("11"), high=Decimal("12"), low=Decimal("10"), close=Decimal("11")),
    ]
    with pytest.raises(ValueError):
        donchian(candles, window=3)


def test_donchian_raises_on_non_positive_window() -> None:
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        _mk_candle(open_=Decimal("11"), high=Decimal("12"), low=Decimal("10"), close=Decimal("11")),
    ]
    with pytest.raises(ValueError):
        donchian(candles, window=0)


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

def test_vwap_basic_behavior() -> None:
    candles = [
        _mk_candle(
            open_=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
            volume=Decimal("2"),
        ),
        _mk_candle(
            open_=Decimal("11"),
            high=Decimal("12"),
            low=Decimal("10"),
            close=Decimal("12"),
            volume=Decimal("1"),
        ),
    ]
    # Формула из docstring индикатора:
    # sum(close_i * volume_i) / sum(volume_i)
    expected = (Decimal("10") * Decimal("2") + Decimal("12") * Decimal("1")) / Decimal("3")

    result = vwap(candles)
    assert isinstance(result, Decimal)
    assert result == expected


def test_vwap_one_candle_zero_volume_returns_close() -> None:
    # Для одиночной свечи volume == 0 допускается и возвращается её close.
    candle = _mk_candle(
        open_=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=Decimal("0"),
    )
    result = vwap([candle])
    assert result == candle.close


def test_vwap_raises_on_empty_candles() -> None:
    with pytest.raises(ValueError):
        vwap([])


def test_vwap_raises_on_zero_total_volume_multiple_candles() -> None:
    c1 = _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10"), volume=Decimal("0"))
    c2 = _mk_candle(open_=Decimal("11"), high=Decimal("12"), low=Decimal("10"), close=Decimal("11"), volume=Decimal("0"))
    with pytest.raises(ZeroDivisionError):
        vwap([c1, c2])


# ---------------------------------------------------------------------------
# MICROPRICE
# ---------------------------------------------------------------------------

def test_microprice_basic_behavior() -> None:
    best_bid = Decimal("100")
    best_ask = Decimal("101")
    bid_qty = Decimal("2")
    ask_qty = Decimal("1")

    expected = (best_ask * bid_qty + best_bid * ask_qty) / (bid_qty + ask_qty)
    result = microprice(best_bid, best_ask, bid_qty, ask_qty)

    assert result == expected
    assert best_bid < result < best_ask


def test_microprice_raises_on_non_positive_qty() -> None:
    with pytest.raises(ValueError):
        microprice(Decimal("100"), Decimal("101"), Decimal("0"), Decimal("1"))
    with pytest.raises(ValueError):
        microprice(Decimal("100"), Decimal("101"), Decimal("1"), Decimal("-1"))


def test_microprice_raises_on_invalid_spread() -> None:
    with pytest.raises(ValueError):
        microprice(Decimal("101"), Decimal("101"), Decimal("1"), Decimal("1"))
    with pytest.raises(ValueError):
        microprice(Decimal("102"), Decimal("101"), Decimal("1"), Decimal("1"))


# ---------------------------------------------------------------------------
# ORDERBOOK IMBALANCE
# ---------------------------------------------------------------------------

def test_orderbook_imbalance_basic_behavior() -> None:
    bids: List[Tuple[Decimal, Decimal]] = [
        (Decimal("100"), Decimal("1")),
        (Decimal("99"), Decimal("3")),
    ]
    asks: List[Tuple[Decimal, Decimal]] = [
        (Decimal("101"), Decimal("2")),
        (Decimal("102"), Decimal("2")),
    ]
    imbalance = orderbook_imbalance(bids, asks, depth=2)

    expected_bid_volume = Decimal("4")
    expected_ask_volume = Decimal("4")
    expected = expected_bid_volume / (expected_bid_volume + expected_ask_volume)

    assert imbalance == expected
    assert Decimal("0") <= imbalance <= Decimal("1")


def test_orderbook_imbalance_raises_on_non_positive_depth() -> None:
    bids = [(Decimal("100"), Decimal("1"))]
    asks = [(Decimal("101"), Decimal("1"))]
    with pytest.raises(ValueError):
        orderbook_imbalance(bids, asks, depth=0)


def test_orderbook_imbalance_raises_on_negative_volume() -> None:
    bids = [(Decimal("100"), Decimal("-1"))]
    asks = [(Decimal("101"), Decimal("1"))]
    with pytest.raises(ValueError):
        orderbook_imbalance(bids, asks, depth=1)


def test_orderbook_imbalance_raises_on_zero_total_volume() -> None:
    bids = [(Decimal("100"), Decimal("0"))]
    asks = [(Decimal("101"), Decimal("0"))]
    with pytest.raises(ZeroDivisionError):
        orderbook_imbalance(bids, asks, depth=1)
