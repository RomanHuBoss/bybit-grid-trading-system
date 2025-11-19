from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.core.logging_config import get_logger
from src.core.models import Position, Signal, SlippageRecord
from src.db.repositories.position_repository import PositionRepository

__all__ = ["SlippageConfig", "SlippageMonitor"]

logger = get_logger("execution.slippage_monitor")


@dataclass(frozen=True)
class SlippageConfig:
    """
    Конфигурация для расчёта проскальзывания.

    Значения по умолчанию совпадают со спецификацией:
    - ATR > p80 даёт +0.15% (15 bps);
    - depth < 1_000_000 USD даёт +0.25% (25 bps).
    """

    atr_percentile_threshold: Decimal = Decimal("0.8")
    depth_threshold_usd: Decimal = Decimal("1000000")

    atr_penalty_bps: Decimal = Decimal("15")   # 0.15%
    depth_penalty_bps: Decimal = Decimal("25")  # 0.25%


class SlippageMonitor:
    """
    Монитор проскальзывания.

    Задачи:
    - посчитать базовый slippage_bps = (actual / expected - 1) * 10_000;
    - скорректировать его с учётом ATR-percentile и глубины стакана;
    - сохранить результат в позиции (поле Position.slippage) через PositionRepository;
    - вернуть SlippageRecord как DTO измерения.

    ВАЖНО: здесь мы работаем на уровне доменных моделей, а не сырых ответов биржи.
    """

    def __init__(self, position_repository: PositionRepository, config: SlippageConfig) -> None:
        self._positions = position_repository
        self._config = config

    # -------------------------------------------------------------------------
    # Публичные методы записи проскальзывания
    # -------------------------------------------------------------------------

    async def record_entry_slippage(
        self,
        *,
        signal: Signal,
        position: Position,
        actual_price: Decimal,
        atr_percentile: Optional[Decimal] = None,
        depth_usd: Optional[Decimal] = None,
        executed_at: Optional[datetime] = None,
    ) -> SlippageRecord:
        """
        Зафиксировать проскальзывание при входе в позицию.

        requested_price для entry по спецификации:
            requested_price = signal.entry_price

        :param signal: Исходный сигнал, от которого открыта позиция.
        :param position: Позиция, к которой относится fill.
        :param actual_price: Средняя фактическая цена исполнения.
        :param atr_percentile: Процентиль ATR на момент входа (0..1), если известен.
        :param depth_usd: Оценка глубины стакана в USD, если известна.
        :param executed_at: Время исполнения; по умолчанию — текущее UTC.
        :return: SlippageRecord как DTO измерения.
        :raises ValueError: при некорректных atr_percentile / depth_usd.
        :raises DatabaseError: прокидывается из PositionRepository.update.
        """
        if executed_at is None:
            executed_at = datetime.now(timezone.utc)

        expected_price = signal.entry_price
        base_bps = self._compute_slippage_bps(expected_price, actual_price)
        adjusted_bps = self._apply_adjustments(
            base_slippage_bps=base_bps,
            atr_percentile=atr_percentile,
            depth_usd=depth_usd,
        )

        # Обновляем позицию (для агрегаций по positions.slippage)
        position.slippage = adjusted_bps

        logger.info(
            "Recording entry slippage",
            position_id=str(position.id),
            symbol=position.symbol,
            direction=position.direction,
            expected_price=str(expected_price),
            actual_price=str(actual_price),
            base_bps=str(base_bps),
            adjusted_bps=str(adjusted_bps),
        )

        updated = await self._positions.update(position)

        # DTO измерения — в терминах expected/actual цен
        record = SlippageRecord(
            position_id=updated.id,
            symbol=updated.symbol,
            direction=updated.direction,
            expected_price=expected_price,
            actual_price=actual_price,
            executed_at=executed_at,
        )
        return record

    async def record_exit_slippage(
        self,
        *,
        position: Position,
        requested_price: Decimal,
        actual_price: Decimal,
        atr_percentile: Optional[Decimal] = None,
        depth_usd: Optional[Decimal] = None,
        executed_at: Optional[datetime] = None,
    ) -> SlippageRecord:
        """
        Зафиксировать проскальзывание при выходе из позиции.

        По спецификации:
        - requested_price для exit — это соответствующая TP/SL цена, с которой сравниваем фактический fill.

        :param position: Позиция, по которой закрытие считается.
        :param requested_price: Ожидаемая цена (TP/SL), по которой хотели исполниться.
        :param actual_price: Фактическая средняя цена выхода.
        :param atr_percentile: Процентиль ATR (0..1) на момент выхода, если известен.
        :param depth_usd: Оценка глубины стакана в USD, если известна.
        :param executed_at: Время исполнения; по умолчанию — текущее UTC.
        :return: SlippageRecord как DTO измерения.
        :raises ValueError: при некорректных atr_percentile / depth_usd.
        :raises DatabaseError: прокидывается из PositionRepository.update.
        """
        if executed_at is None:
            executed_at = datetime.now(timezone.utc)

        expected_price = requested_price
        base_bps = self._compute_slippage_bps(expected_price, actual_price)
        adjusted_bps = self._apply_adjustments(
            base_slippage_bps=base_bps,
            atr_percentile=atr_percentile,
            depth_usd=depth_usd,
        )

        position.slippage = adjusted_bps

        logger.info(
            "Recording exit slippage",
            position_id=str(position.id),
            symbol=position.symbol,
            direction=position.direction,
            expected_price=str(expected_price),
            actual_price=str(actual_price),
            base_bps=str(base_bps),
            adjusted_bps=str(adjusted_bps),
        )

        updated = await self._positions.update(position)

        record = SlippageRecord(
            position_id=updated.id,
            symbol=updated.symbol,
            direction=updated.direction,
            expected_price=expected_price,
            actual_price=actual_price,
            executed_at=executed_at,
        )
        return record

    # -------------------------------------------------------------------------
    # Регулировки по ATR и глубине рынка
    # -------------------------------------------------------------------------

    def adjust_for_atr(self, base_slippage_bps: Decimal, atr_percentile: Decimal) -> Decimal:
        """
        Учесть высокий ATR (волатильность) при расчёте проскальзывания.

        Если atr_percentile >= atr_percentile_threshold, добавляем +atr_penalty_bps.

        :param base_slippage_bps: Базовое проскальзывание в bps.
        :param atr_percentile: Процентиль ATR в диапазоне [0, 1].
        :return: Скорректированное значение проскальзывания.
        :raises ValueError: если atr_percentile не в диапазоне [0, 1].
        """
        if atr_percentile < 0 or atr_percentile > 1:
            raise ValueError(f"atr_percentile must be in [0, 1], got {atr_percentile!r}")

        if atr_percentile >= self._config.atr_percentile_threshold:
            return base_slippage_bps + self._config.atr_penalty_bps

        return base_slippage_bps

    def adjust_for_depth(self, base_slippage_bps: Decimal, depth_usd: Decimal) -> Decimal:
        """
        Учесть недостаток ликвидности (глубины стакана) при расчёте проскальзывания.

        Если depth_usd < depth_threshold_usd, добавляем +depth_penalty_bps.

        :param base_slippage_bps: Базовое проскальзывание в bps.
        :param depth_usd: Глубина рынка в USD. Ожидается значение >= 0.
        :return: Скорректированное значение проскальзывания.
        :raises ValueError: если depth_usd < 0.
        """
        if depth_usd < 0:
            raise ValueError(f"depth_usd must be non-negative, got {depth_usd!r}")

        if depth_usd < self._config.depth_threshold_usd:
            return base_slippage_bps + self._config.depth_penalty_bps

        return base_slippage_bps

    # -------------------------------------------------------------------------
    # Внутренние утилиты
    # -------------------------------------------------------------------------

    @staticmethod
    def _compute_slippage_bps(expected_price: Decimal, actual_price: Decimal) -> Decimal:
        """
        Базовый расчёт проскальзывания в bps:

            slippage_bps = (actual / expected - 1) * 10_000

        Оба аргумента должны быть > 0 (валидируется моделью/БД).
        """
        # Безопасная защита от деления на ноль, даже если кто-то обошёл валидацию.
        if expected_price <= 0:
            raise ValueError(f"expected_price must be > 0, got {expected_price!r}")
        if actual_price <= 0:
            raise ValueError(f"actual_price must be > 0, got {actual_price!r}")

        return (actual_price / expected_price - Decimal("1")) * Decimal("10000")

    def _apply_adjustments(
        self,
        *,
        base_slippage_bps: Decimal,
        atr_percentile: Optional[Decimal],
        depth_usd: Optional[Decimal],
    ) -> Decimal:
        """
        Последовательно применить корректировки по ATR и глубине рынка.
        """
        adjusted = base_slippage_bps

        if atr_percentile is not None:
            adjusted = self.adjust_for_atr(adjusted, atr_percentile)

        if depth_usd is not None:
            adjusted = self.adjust_for_depth(adjusted, depth_usd)

        return adjusted
