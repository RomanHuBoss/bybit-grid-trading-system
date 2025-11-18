from __future__ import annotations

from decimal import Decimal, getcontext
from typing import Iterable, List, Sequence, Tuple

from src.core.models import ConfirmedCandle as Candle


# Индикаторы стратегии работают с Decimal с точностью не ниже 28.
getcontext().prec = 28


# ---------------------------------------------------------------------------
# EMA (Exponential Moving Average)
# ---------------------------------------------------------------------------

def ema(values: Sequence[Decimal], period: int) -> Decimal:
    """
    Классическая EMA.
    Требует не меньше `period` элементов.

    Формула:
        EMA_t = α * v_t + (1 - α) * EMA_{t-1}
        α = 2 / (period + 1)
    """
    if period <= 0:
        raise ValueError("EMA period must be positive")

    if len(values) < period:
        raise ValueError("Not enough values to compute EMA")

    alpha = Decimal("2") / Decimal(period + 1)
    e = values[0]

    for v in values[1:]:
        e = alpha * v + (Decimal(1) - alpha) * e

    return e


# ---------------------------------------------------------------------------
# ATR (Average True Range) через EMA TR
# ---------------------------------------------------------------------------

def atr(candles: Sequence[Candle], period: int) -> Decimal:
    """
    ATR по определению Уайлдера:
        TR_t = max(high_t - low_t, |high_t - close_{t-1}|, |low_t - close_{t-1}|)
        ATR_t = EMA(TR, period)

    Требования:
        • не менее period+1 баров (нужен предыдущий close).
        • цены и объёмы — Decimal.
    """
    if period <= 0:
        raise ValueError("ATR period must be positive")

    if len(candles) < period + 1:
        raise ValueError("Not enough candles to compute ATR")

    trs: List[Decimal] = []
    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]

        tr1 = cur.high - cur.low
        tr2 = (cur.high - prev.close).copy_abs()
        tr3 = (cur.low - prev.close).copy_abs()

        trs.append(max(tr1, tr2, tr3))

    return ema(trs, period)


# ---------------------------------------------------------------------------
# VWAP за окно последних N баров
# ---------------------------------------------------------------------------

def vwap(candles: Sequence[Candle]) -> Decimal:
    """
    VWAP = sum(price_i * volume_i) / sum(volume_i)
    где price_i = (high+low+close)/3 или close (вариант).
    Для простоты: price_i = close (как в vwap базовой реализации AVI-5).

    Требует:
        • >= 1 свечи,
        • volume >= 0.
    """
    if not candles:
        raise ValueError("VWAP requires at least one candle")

    total_volume = Decimal("0")
    total_price_volume = Decimal("0")

    for c in candles:
        v = c.volume
        if v < 0:
            raise ValueError("Volume must be non-negative")

        total_volume += v
        total_price_volume += c.close * v

    if total_volume == 0:
        raise ZeroDivisionError("VWAP total volume is zero")

    return total_price_volume / total_volume


# ---------------------------------------------------------------------------
# Donchian Channel (N-bar high/low)
# ---------------------------------------------------------------------------

def donchian(candles: Sequence[Candle], window: int) -> Tuple[Decimal, Decimal]:
    """
    Классический Donchian:
        upper = max(high_i), lower = min(low_i) за последний window баров.

    Требует:
        • window >= 1
        • len(candles) >= window
    """
    if window <= 0:
        raise ValueError("Donchian window must be positive")

    if len(candles) < window:
        raise ValueError("Not enough candles for Donchian channel")

    highs = [c.high for c in candles[-window:]]
    lows = [c.low for c in candles[-window:]]

    return max(highs), min(lows)


# ---------------------------------------------------------------------------
# Microprice, orderbook imbalance
# ---------------------------------------------------------------------------

def microprice(
    best_bid: Decimal,
    best_ask: Decimal,
    bid_qty: Decimal,
    ask_qty: Decimal,
) -> Decimal:
    """
    Microprice:
        mp = (best_ask * bid_qty + best_bid * ask_qty) / (bid_qty + ask_qty)

    Требует:
        • bid_qty > 0
        • ask_qty > 0
        • best_bid <= best_ask
    """
    if bid_qty <= 0 or ask_qty <= 0:
        raise ValueError("Bid/ask quantities must be positive")

    if best_bid > best_ask:
        raise ValueError("best_bid cannot exceed best_ask")

    return (best_ask * bid_qty + best_bid * ask_qty) / (bid_qty + ask_qty)


def orderbook_imbalance(
    bid_levels: Iterable[Tuple[Decimal, Decimal]],
    ask_levels: Iterable[Tuple[Decimal, Decimal]],
    depth: int = 5,
) -> Decimal:
    """
    Отношение агрегированного bid volume к суммарному объёму bid+ask.

    При depth=N берём первые N уровней стакана.

    Формула:
        I = (sum(bid_qty[0:N])) / (sum(bid_qty[0:N]) + sum(ask_qty[0:N]))

    Требует, чтобы оба списка уровней были non-empty и depth>=1.
    """
    if depth <= 0:
        raise ValueError("depth must be >= 1")

    bid_list = list(bid_levels)[:depth]
    ask_list = list(ask_levels)[:depth]

    if not bid_list or not ask_list:
        raise ValueError("Both bid and ask levels must be non-empty")

    bid_volume = sum(q for _, q in bid_list)
    ask_volume = sum(q for _, q in ask_list)

    if bid_volume < 0 or ask_volume < 0:
        raise ValueError("Volumes must be non-negative")

    total = bid_volume + ask_volume
    if total == 0:
        raise ZeroDivisionError("Total bid+ask volume is zero")

    return Decimal(bid_volume) / Decimal(total)
