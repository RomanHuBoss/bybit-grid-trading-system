# tests/unit/test_indicators.py

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
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

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


# =====================================================================
# Вспомогательные утилиты
# =====================================================================

def _mk_candle(
    *,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal = Decimal("1"),
    ts: datetime | None = None,
) -> Candle:
    """Удобный конструктор простой свечи для тестов."""
    if ts is None:
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)

    return Candle(
        symbol="BTCUSDT",
        open_time=ts,
        close_time=ts + timedelta(minutes=5),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        confirmed=True,
    )


# =====================================================================
# EMA
# =====================================================================

def test_ema_basic_monotonic_series() -> None:
    values = [Decimal(x) for x in [1, 2, 3, 4, 5]]
    period = 3
    # подсчитаем ожидание тем же алгоритмом "в лоб"
    alpha = Decimal("2") / Decimal(period + 1)
    expected = values[0]
    for v in values[1:]:
        expected = alpha * v + (Decimal(1) - alpha) * expected

    result = ema(values, period)
    assert result == expected


def test_ema_raises_on_too_short_series() -> None:
    values = [Decimal("1"), Decimal("2")]
    with pytest.raises(ValueError):
        ema(values, period=3)


def test_ema_raises_on_non_positive_period() -> None:
    with pytest.raises(ValueError):
        ema([Decimal("1")] * 5, period=0)


# =====================================================================
# ATR
# =====================================================================

def test_atr_matches_ema_of_true_range() -> None:
    """
    Проверяем, что atr() считает TR по формуле Уайлдера и затем просто
    применяет ema(TR, period).
    """
    # Делаем 4 свечи и period=3 → TR будет 3 значения.
    candles: List[Candle] = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        _mk_candle(open_=Decimal("10"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11")),
        _mk_candle(open_=Decimal("11"), high=Decimal("13"), low=Decimal("10"), close=Decimal("12")),
        _mk_candle(open_=Decimal("12"), high=Decimal("14"), low=Decimal("11"), close=Decimal("13")),
    ]

    # Явно считаем TR по определению:
    trs: List[Decimal] = []
    for cur, prev in zip(candles[1:], candles[:-1]):
        tr1 = cur.high - cur.low
        tr2 = (cur.high - prev.close).copy_abs()
        tr3 = (cur.low - prev.close).copy_abs()
        trs.append(max(tr1, tr2, tr3))

    period = 3
    expected = ema(trs, period)
    result = atr(candles, period)

    assert result == expected


def test_atr_raises_on_insufficient_candles() -> None:
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        _mk_candle(open_=Decimal("10"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11")),
    ]
    # для period=3 нужно как минимум period+1=4 свечи
    with pytest.raises(ValueError):
        atr(candles, period=3)


def test_atr_raises_on_non_positive_period() -> None:
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        _mk_candle(open_=Decimal("10"), high=Decimal("12"), low=Decimal("9"), close=Decimal("11")),
    ]
    with pytest.raises(ValueError):
        atr(candles, period=0)


# =====================================================================
# VWAP
# =====================================================================

@pytest.fixture
def kline_snapshot() -> dict:
    path = FIXTURES_DIR / "sample_kline.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def kline_candle(kline_snapshot: dict) -> Candle:
    row = kline_snapshot["data"][0]
    ts = datetime.fromtimestamp(row["start"] / 1000, tz=timezone.utc)
    return Candle(
        symbol=row["symbol"],
        open_time=ts,
        close_time=ts + timedelta(minutes=5),
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=Decimal(row["volume"]),
        confirmed=row.get("confirm", True),
    )


def test_vwap_single_candle_equals_close(kline_candle: Candle) -> None:
    """
    При одной свече VWAP должен быть просто равен close (см. реализацию).
    """
    result = vwap([kline_candle])
    assert result == kline_candle.close


def test_vwap_weighted_average_multiple_candles() -> None:
    c1 = _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10"), volume=Decimal("1"))
    c2 = _mk_candle(open_=Decimal("20"), high=Decimal("21"), low=Decimal("19"), close=Decimal("20"), volume=Decimal("3"))

    # Ожидание: (10*1 + 20*3) / (1+3) = (10+60)/4 = 17.5
    expected = (c1.close * c1.volume + c2.close * c2.volume) / (c1.volume + c2.volume)
    result = vwap([c1, c2])

    assert result == expected


def test_vwap_raises_on_empty() -> None:
    with pytest.raises(ValueError):
        vwap([])


def test_vwap_raises_on_negative_volume() -> None:
    c = _mk_candle(
        open_=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=Decimal("-1"),
    )
    with pytest.raises(ValueError):
        vwap([c])


def test_vwap_raises_on_zero_total_volume() -> None:
    c1 = _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10"), volume=Decimal("0"))
    c2 = _mk_candle(open_=Decimal("11"), high=Decimal("12"), low=Decimal("10"), close=Decimal("11"), volume=Decimal("0"))
    with pytest.raises(ZeroDivisionError):
        vwap([c1, c2])


# =====================================================================
# Donchian
# =====================================================================

def test_donchian_basic_window() -> None:
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        _mk_candle(open_=Decimal("11"), high=Decimal("13"), low=Decimal("10"), close=Decimal("12")),
        _mk_candle(open_=Decimal("12"), high=Decimal("15"), low=Decimal("11"), close=Decimal("14")),
    ]
    upper, lower = donchian(candles, window=2)

    # Ожидание: берём последние 2 бара
    highs = [c.high for c in candles[-2:]]
    lows = [c.low for c in candles[-2:]]

    assert upper == max(highs)
    assert lower == min(lows)


def test_donchian_raises_on_invalid_window() -> None:
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
    ]
    with pytest.raises(ValueError):
        donchian(candles, window=0)


def test_donchian_raises_on_not_enough_candles() -> None:
    candles = [
        _mk_candle(open_=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
    ]
    with pytest.raises(ValueError):
        donchian(candles, window=2)


# =====================================================================
# Microprice
# =====================================================================

def test_microprice_basic_formula() -> None:
    best_bid = Decimal("100")
    best_ask = Decimal("101")
    bid_qty = Decimal("2")
    ask_qty = Decimal("1")

    # (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty)
    expected = (best_ask * bid_qty + best_bid * ask_qty) / (bid_qty + ask_qty)
    result = microprice(best_bid, best_ask, bid_qty, ask_qty)

    assert result == expected


@pytest.mark.parametrize(
    "bid_qty, ask_qty",
    [
        (Decimal("0"), Decimal("1")),
        (Decimal("1"), Decimal("0")),
        (Decimal("-1"), Decimal("1")),
        (Decimal("1"), Decimal("-1")),
    ],
)
def test_microprice_raises_on_non_positive_qty(bid_qty: Decimal, ask_qty: Decimal) -> None:
    with pytest.raises(ValueError):
        microprice(Decimal("100"), Decimal("101"), bid_qty, ask_qty)


def test_microprice_raises_when_bid_above_ask() -> None:
    with pytest.raises(ValueError):
        microprice(Decimal("101"), Decimal("100"), Decimal("1"), Decimal("1"))


# =====================================================================
# Orderbook imbalance
# =====================================================================

@pytest.fixture
def orderbook_snapshot() -> dict:
    path = FIXTURES_DIR / "sample_ob.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def orderbook_levels(orderbook_snapshot: dict) -> Tuple[List[Tuple[Decimal, Decimal]], List[Tuple[Decimal, Decimal]]]:
    row = orderbook_snapshot["data"][0]
    bids = [(Decimal(p), Decimal(q)) for p, q in row["b"]]
    asks = [(Decimal(p), Decimal(q)) for p, q in row["a"]]
    return bids, asks


def test_orderbook_imbalance_uses_aggregated_volume(orderbook_levels) -> None:
    bids, asks = orderbook_levels
    depth = 3

    bid_slice = bids[:depth]
    ask_slice = asks[:depth]

    bid_volume = sum(q for _, q in bid_slice)
    ask_volume = sum(q for _, q in ask_slice)
    expected = bid_volume / (bid_volume + ask_volume)

    result = orderbook_imbalance(bid_slice, ask_slice, depth=depth)
    assert result == expected


def test_orderbook_imbalance_raises_on_invalid_depth(orderbook_levels) -> None:
    bids, asks = orderbook_levels
    with pytest.raises(ValueError):
        orderbook_imbalance(bids, asks, depth=0)


def test_orderbook_imbalance_raises_on_empty_sides() -> None:
    bids = [(Decimal("100"), Decimal("1"))]
    asks: list[tuple[Decimal, Decimal]] = []
    with pytest.raises(ValueError):
        orderbook_imbalance(bids, asks, depth=1)


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
