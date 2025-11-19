from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, Mapping

from src.core.models import Position


# Список типичных суффиксов для линейных фьючерсов Bybit.
# Логика специально простая и предсказуемая, без "магии".
_SYMBOL_SUFFIXES = (
    "USDT",
    "USDC",
    "USD",
)


def extract_base_symbol(symbol: str) -> str:
    """
    Выделить базовый актив из торгового символа.

    Примеры:
        BTCUSDT -> BTC
        ETHUSDC -> ETH
        XYZUSD  -> XYZ

    Если символ не заканчивается известным суффиксом, возвращаем его целиком.
    Это безопасное поведение по умолчанию: per-base лимит тогда фактически
    превращается в per-symbol.
    """
    if not symbol:
        return symbol

    upper = symbol.upper()
    for suffix in _SYMBOL_SUFFIXES:
        if upper.endswith(suffix):
            return upper[: -len(suffix)] or upper

    return upper


def count_open_positions_by_base(
    positions: Iterable[Position],
) -> Dict[str, Dict[str, int]]:
    """
    Подсчитать количество ОТКРЫТЫХ позиций по базовому активу и направлению.

    Возвращает словарь вида:
        {
            "BTC": {"long": 1, "short": 0},
            "ETH": {"long": 0, "short": 1},
            ...
        }

    Закрытые позиции (closed_at != None) игнорируются.
    """
    result: Dict[str, Dict[str, int]] = defaultdict(lambda: {"long": 0, "short": 0})

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

        result[base][direction] += 1

    return dict(result)


def can_open_position_for_base(
    positions: Iterable[Position],
    symbol: str,
    direction: str,
    max_positions_per_base: int = 2,
) -> bool:
    """
    Проверить, можно ли открыть ещё одну позицию по базовому активу.

    Правила (соответствуют per-base лимиту из спецификации):
      * по базовому активу допустимо не более max_positions_per_base открытых позиций;
      * по каждому направлению (long/short) допускается максимум одна открытая позиция:
        - нельзя иметь две long по BTC (long + long),
        - нельзя иметь две short по BTC (short + short),
        - комбинация long + short по тому же базовому активу разрешена.

    :param positions: Текущий список позиций (как минимум открытых,
                      закрытые будут отфильтрованы).
    :param symbol: Символ инструмента для новой позиции (например, "BTCUSDT").
    :param direction: Направление новой позиции: "long" или "short".
    :param max_positions_per_base: Максимум позиций на один базовый актив.
                                   По умолчанию 2 (одна long + одна short).
    :return: True, если позицию открывать можно, иначе False.
    """
    if max_positions_per_base < 1:
        # Конфигурировать такой лимит бессмысленно, но не ломаемся —
        # считаем, что открывать позицию нельзя.
        return False

    normalized_direction = direction.lower()
    if normalized_direction not in ("long", "short"):
        raise ValueError(f"Unsupported direction: {direction!r}. Expected 'long' or 'short'.")

    base = extract_base_symbol(symbol)
    counts_by_base: Mapping[str, Dict[str, int]] = count_open_positions_by_base(positions)
    base_counts = counts_by_base.get(base, {"long": 0, "short": 0})

    total_open_for_base = base_counts["long"] + base_counts["short"]

    # 1) Проверка общего per-base лимита
    if total_open_for_base >= max_positions_per_base:
        return False

    # 2) Проверка "по одному направлению"
    if base_counts[normalized_direction] >= 1:
        return False

    return True


__all__ = [
    "extract_base_symbol",
    "count_open_positions_by_base",
    "can_open_position_for_base",
]
