from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable

from src.core.models import Position


# Список типичных суффиксов для линейных фьючерсов Bybit.
# Логика специально простая и предсказуемая, без "магии".
_SYMBOL_SUFFIXES: tuple[str, ...] = (
    "USDT",
    "USDC",
    "USD",
)


def extract_base_symbol(symbol: str) -> str:
    """
    Выделить базовый актив из линейного фьючерса Bybit по его тикеру.

    Правила:
      * если тикер оканчивается на один из известных суффиксов (_SYMBOL_SUFFIXES),
        мы воспринимаем эту часть как котируемую валюту и отбрасываем её;
      * оставшаяся часть (prefix) возвращается в верхнем регистре;
      * если ни один суффикс не совпал, возвращаем исходный тикер в верхнем
        регистре целиком.

    Примеры:
        "BTCUSDT" -> "BTC"
        "ETHUSD"  -> "ETH"
        "SOLUSDC" -> "SOL"
        "XRP"     -> "XRP"  (суффикс не найден)
    """
    if not symbol:
        return symbol

    upper = symbol.upper()
    for suffix in _SYMBOL_SUFFIXES:
        if upper.endswith(suffix):
            # Если суффикс занимает весь символ (теоретически), возвращаем как есть.
            base = upper[: -len(suffix)]
            return base or upper

    return upper


def count_open_positions_by_base(
    positions: Iterable[Position],
) -> Dict[str, Dict[str, int]]:
    """
    Подсчитать количество открытых позиций по базовому активу и направлению.

    Возвращает словарь вида:
        {
            "BTC": {"long": 1, "short": 0},
            "ETH": {"long": 0, "short": 2},
        }

    Учитываются только действительно открытые позиции (closed_at is None).
    Любые позиции с направлением, отличным от "long"/"short", мягко игнорируются.
    """
    result: defaultdict[str, Dict[str, int]] = defaultdict(
        lambda: {"long": 0, "short": 0},
    )

    for position in positions:
        # Берём только действительно открытые позиции
        if position.closed_at is not None:
            continue

        base = extract_base_symbol(position.symbol)
        direction = position.direction.lower()

        # direction валидируется самой моделью Position (long|short),
        # но на всякий случай делаем мягкую защиту.
        if direction not in ("long", "short"):
            continue

        counts = result[base]
        counts[direction] = counts.get(direction, 0) + 1

    # Преобразуем к обычному dict, чтобы не засвечивать наружу defaultdict.
    return {base: dict(counts) for base, counts in result.items()}


def can_open_position_for_base(
    positions: Iterable[Position],
    symbol: str,
    direction: str,
    max_positions_per_base: int = 2,
) -> bool:
    """
    Проверить, можно ли открыть ещё одну позицию по базовому активу.

    Правила (соответствуют per-base лимиту из спецификации):
      * по базовому активу допустимо не более ``max_positions_per_base`` открытых
        позиций суммарно (long + short);
      * по одному направлению (long/short) в базовом активе допускается не более
        одной открытой позиции;
      * некорректные значения ``direction`` приводят к безопасному отказу
        (функция возвращает False).

    Функция не занимается проверкой общих аккаунтных лимитов
    (max_concurrent, max_total_risk_r и т.п.) — только per-base ограничениями.
    """
    if max_positions_per_base <= 0:
        # Конфигурация с нулевым лимитом не имеет смысла — безопаснее запретить вход.
        return False

    base_counts = count_open_positions_by_base(positions)
    base = extract_base_symbol(symbol)
    normalized_direction = direction.lower()

    if normalized_direction not in ("long", "short"):
        # Неподдерживаемое направление — безопаснее запретить вход.
        return False

    counts_for_base = base_counts.get(base, {"long": 0, "short": 0})

    # 1) Проверка общего количества позиций по базовому активу
    total_open_for_base = (
        counts_for_base.get("long", 0) + counts_for_base.get("short", 0)
    )
    if total_open_for_base >= max_positions_per_base:
        return False

    # 2) Проверка "по одному направлению"
    if counts_for_base.get(normalized_direction, 0) >= 1:
        return False

    return True


__all__ = [
    "extract_base_symbol",
    "count_open_positions_by_base",
    "can_open_position_for_base",
]
