from __future__ import annotations

from decimal import Decimal, getcontext
from typing import List, Sequence, Tuple

from src.core.models import ConfirmedCandle as Candle

# Индикаторы стратегии работают с Decimal с точностью не ниже 28.
getcontext().prec = 28


# Тип для уровней стакана: (price, quantity)
OrderbookLevel = Tuple[Decimal, Decimal]


# ---------------------------------------------------------------------------
# EMA (Exponential Moving Average)
# ---------------------------------------------------------------------------

def ema(values: Sequence[Decimal], period: int) -> Decimal:
    """
    Экспоненциальное скользящее среднее по стандартной формуле.

    Алгоритм совпадает с тем, что используется в тестах:

        alpha = 2 / (period + 1)
        ema_0 = values[0]
        ema_t = alpha * value_t + (1 - alpha) * ema_{t-1}

    :param values: Последовательность значений (Decimal).
    :param period: Период усреднения, > 0.
    :raises ValueError: если period <= 0 или длина ряда < period.
    """
    if period <= 0:
        raise ValueError("EMA period must be positive")

    n = len(values)
    if n < period:
        raise ValueError("Series is too short for the given EMA period")

    alpha = Decimal("2") / Decimal(period + 1)
    one_minus_alpha = Decimal(1) - alpha

    result = values[0]
    for v in values[1:]:
        result = alpha * v + one_minus_alpha * result

    return result


# ---------------------------------------------------------------------------
# ATR (Average True Range)
# ---------------------------------------------------------------------------

def atr(candles: Sequence[Candle], period: int) -> Decimal:
    """
    ATR по формуле Уайлдера.

    Тесты ожидают, что мы:
      1) считаем TR (True Range) как:
            TR = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
         для каждой пары соседних свечей (cur, prev);
      2) применяем ema(TR, period).

    То есть длина ряда TR = len(candles) - 1.
    Для заданного `period` нужно минимум `period + 1` свечей.
    """
    if period <= 0:
        raise ValueError("ATR period must be positive")

    if len(candles) < period + 1:
        raise ValueError("Not enough candles for ATR: require at least period+1")

    trs: List[Decimal] = []
    for cur, prev in zip(candles[1:], candles[:-1]):
        tr1 = cur.high - cur.low
        tr2 = (cur.high - prev.close).copy_abs()
        tr3 = (cur.low - prev.close).copy_abs()
        trs.append(max(tr1, tr2, tr3))

    return ema(trs, period)


# ---------------------------------------------------------------------------
# VWAP (Volume-Weighted Average Price)
# ---------------------------------------------------------------------------

def vwap(candles: Sequence[Candle]) -> Decimal:
    """
    VWAP по закрытию свечей.

    Контракт по тестам:

    * при одной свече результат должен быть ровно равен close;
    * при нескольких свечах используется объёмное среднее:
        sum(close_i * volume_i) / sum(volume_i);
    * vwap([]) → ValueError;
    * отрицательный объём хотя бы у одной свечи → ValueError;
    * при нескольких свечах и суммарном объёме == 0 → ZeroDivisionError.

    Для одиночной свечи допускаем volume == 0 и всё равно возвращаем close,
    как описано в docstring теста.
    """
    if not candles:
        raise ValueError("VWAP requires at least one candle")

    # Проверяем объёмы на отрицательные значения.
    for c in candles:
        if c.volume < 0:
            raise ValueError("Volume must be non-negative for VWAP")

    if len(candles) == 1:
        # Особый контракт: VWAP одной свечи = её close,
        # независимо от объёма (кроме отрицательного, который мы уже отсеяли).
        return candles[0].close

    total_volume = sum(c.volume for c in candles)
    if total_volume == 0:
        # Тест ожидает ZeroDivisionError при нулевой сумме объёмов.
        raise ZeroDivisionError("Total volume is zero in VWAP")

    weighted_sum = sum(c.close * c.volume for c in candles)
    return Decimal(weighted_sum / total_volume)


# ---------------------------------------------------------------------------
# Donchian Channel
# ---------------------------------------------------------------------------

def donchian(candles: Sequence[Candle], window: int) -> Tuple[Decimal, Decimal]:
    """
    Дончиан-канал по high/low.

    По тестам:

    * окно должно быть > 0, иначе ValueError;
    * длина ряда должна быть не меньше окна, иначе ValueError;
    * для `window` берутся последние `window` свечей;
    * upper = max(high_i), lower = min(low_i) по этим свечам.
    """
    if window <= 0:
        raise ValueError("Donchian window must be positive")

    if len(candles) < window:
        raise ValueError("Not enough candles for Donchian window")

    window_slice = candles[-window:]
    highs = [c.high for c in window_slice]
    lows = [c.low for c in window_slice]

    upper = max(highs)
    lower = min(lows)
    return upper, lower


# ---------------------------------------------------------------------------
# Microprice
# ---------------------------------------------------------------------------

def microprice(
    best_bid: Decimal,
    best_ask: Decimal,
    bid_qty: Decimal,
    ask_qty: Decimal,
) -> Decimal:
    """
    Микропрайс на основе лучшего бида/аска и их объёмов.

    Тесты явно задают формулу:

        (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty)

    и проверяют следующие инварианты:

    * bid_qty > 0 и ask_qty > 0, иначе ValueError;
    * best_bid < best_ask, иначе ValueError.
    """
    if bid_qty <= 0 or ask_qty <= 0:
        raise ValueError("Bid/ask quantities must be positive for microprice")

    if best_bid >= best_ask:
        raise ValueError("Best bid must be strictly below best ask")

    denominator = bid_qty + ask_qty
    if denominator == 0:
        # Теоретически невозможен при наших проверках, но оставим защиту.
        raise ZeroDivisionError("Total quantity is zero in microprice")

    numerator = best_ask * bid_qty + best_bid * ask_qty
    return numerator / denominator


# ---------------------------------------------------------------------------
# Orderbook imbalance
# ---------------------------------------------------------------------------

def orderbook_imbalance(
    bids: Sequence[OrderbookLevel],
    asks: Sequence[OrderbookLevel],
    *,
    depth: int,
) -> Decimal:
    """
    Имбаланс стакана по агрегированным объёмам.

    Контракт по тестам:

    * используется суммарный объём по первым `depth` уровням каждой стороны;
    * результат = bid_volume / (bid_volume + ask_volume);
    * depth должен быть > 0, иначе ValueError;
    * обе стороны после среза должны быть непустыми, иначе ValueError;
    * отрицательные объёмы → ValueError;
    * при суммарном объёме == 0 → ZeroDivisionError.
    """
    if depth <= 0:
        raise ValueError("Depth must be positive")

    bid_slice = list(bids[:depth])
    ask_slice = list(asks[:depth])

    if not bid_slice or not ask_slice:
        raise ValueError("Both bid and ask levels must be non-empty")

    bid_volume = sum(q for _, q in bid_slice)
    ask_volume = sum(q for _, q in ask_slice)

    if bid_volume < 0 or ask_volume < 0:
        raise ValueError("Volumes must be non-negative")

    total = bid_volume + ask_volume
    if total == 0:
        raise ZeroDivisionError("Total bid+ask volume is zero")

    return Decimal(bid_volume) / Decimal(total)
