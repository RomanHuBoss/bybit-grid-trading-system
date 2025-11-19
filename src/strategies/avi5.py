from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Sequence

from src.core.logging_config import get_logger
from src.core.models import AVI5Config, ConfirmedCandle, Signal, TradingConfig
from src.risk.risk_manager import RiskManager
from src.strategies.indicators import atr, donchian

__all__ = ["Avi5SignalEngine"]

logger = get_logger("strategies.avi5")


class Avi5SignalEngine:
    """
    SignalEngine для стратегии AVI-5.

    Отвечает за:
    - применение боевых правил входа по 5-минутным свечам;
    - расчёт уровней SL/TP на основе ATR;
    - расчёт размера риска в USD (1R) с учётом theta и TradingConfig;
    - делегирование финальной проверки входа RiskManager'у.

    ВАЖНО:
    - этот класс реализует только "идейную" логику генерации сигнала;
      чтение из стримов, запись в БД, публикация в очереди делаются в других слоях.
    """

    def __init__(
        self,
        avi5_config: AVI5Config,
        trading_config: TradingConfig,
        risk_manager: RiskManager,
        *,
        strategy_version: str = "avi5-1.0.0",
    ) -> None:
        self._cfg = avi5_config
        self._trading = trading_config
        self._risk = risk_manager
        self._strategy_version = strategy_version

    async def generate_signal(
        self,
        candles: Sequence[ConfirmedCandle],
        *,
        spread_ok: bool = True,
        time_to_funding_minutes: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Optional[Signal]:
        """
        Сгенерировать сигнал по последней свече в последовательности.

        Предполагается, что:
        - candles упорядочены по времени по возрастанию;
        - последний элемент — это актуальная подтверждённая свеча по символу.

        :param candles: История свечей по инструменту (минимум atr_window+1 элементов).
        :param spread_ok: Флаг прохождения спред-фильтра (если False — сигнал не генерируется).
        :param time_to_funding_minutes: Сколько минут осталось до ближайшего funding-платежа.
                                        Если задано и меньше 15 — вход запрещён.
        :param now: Текущий момент времени (UTC). Если не задан, берётся datetime.now(UTC).
        :return: Готовый Signal или None, если условия входа не выполнены либо лимиты нарушены.
        """
        if not candles:
            return None

        if now is None:
            now = datetime.now(timezone.utc)

        last = candles[-1]

        # Используем только подтверждённые свечи.
        if not last.confirmed:
            logger.debug("Last candle is not confirmed, skipping", symbol=last.symbol)
            return None

        # Спред-фильтр.
        if not spread_ok:
            logger.debug("Spread filter failed, skipping signal", symbol=last.symbol)
            return None

        # Funding-фильтр: не входим, если до funding меньше 15 минут.
        if time_to_funding_minutes is not None and time_to_funding_minutes < 15:
            logger.debug(
                "Funding filter blocked signal",
                symbol=last.symbol,
                ttf_minutes=time_to_funding_minutes,
            )
            return None

        # Нужно достаточно истории для ATR и Donchian.
        atr_window = self._cfg.atr_window
        if len(candles) < atr_window + 1 or len(candles) < 2:
            logger.debug(
                "Not enough candles for ATR/Donchian",
                symbol=last.symbol,
                candles=len(candles),
                atr_window=atr_window,
            )
            return None

        prev = candles[-2]

        # --- Индикаторы: ATR и Donchian-канал ---
        try:
            atr_value = atr(candles[-(atr_window + 1) :], period=atr_window)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to compute ATR, skipping signal",
                symbol=last.symbol,
                error=str(exc),
            )
            return None

        try:
            upper, lower = donchian(candles[-atr_window:], window=atr_window)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to compute Donchian channel, skipping signal",
                symbol=last.symbol,
                error=str(exc),
            )
            return None

        # --- Триггер входа: пробой Donchian-канала ---
        direction: Optional[str] = None

        if last.close > upper >= prev.close:
            direction = "long"
        elif last.close < lower <= prev.close:
            direction = "short"

        if direction is None:
            logger.debug(
                "No Donchian breakout, no signal",
                symbol=last.symbol,
                last_close=str(last.close),
                upper=str(upper),
                lower=str(lower),
            )
            return None

        # --- Расчёт SL и TP на основе ATR ---
        atr_mult = Decimal(str(self._cfg.atr_multiplier))
        risk_per_unit = (atr_mult * atr_value).copy_abs()

        if risk_per_unit <= 0:
            logger.warning(
                "Non-positive risk_per_unit, skipping signal",
                symbol=last.symbol,
                atr=str(atr_value),
                atr_mult=str(atr_mult),
            )
            return None

        entry_price = last.close

        if direction == "long":
            stop_loss = entry_price - risk_per_unit
            if stop_loss <= 0:
                logger.warning(
                    "Computed SL <= 0 for long, skipping",
                    symbol=last.symbol,
                    entry=str(entry_price),
                    sl=str(stop_loss),
                )
                return None

            tp1 = entry_price + risk_per_unit
            tp2 = entry_price + risk_per_unit * 2
            tp3 = entry_price + risk_per_unit * 3
        else:  # short
            stop_loss = entry_price + risk_per_unit
            tp1 = entry_price - risk_per_unit
            tp2 = entry_price - risk_per_unit * 2
            tp3 = entry_price - risk_per_unit * 3

            if tp3 <= 0:
                # Для short важно, чтобы хотя бы TP3 был позитивным; в противном случае
                # геометрия уровней выглядит некорректно.
                logger.warning(
                    "Computed TP3 <= 0 for short, skipping",
                    symbol=last.symbol,
                    entry=str(entry_price),
                    tp3=str(tp3),
                )
                return None

        # --- Размер риска в USD (1R) и "вероятность" ---
        theta_dec = Decimal(str(self._cfg.theta))
        stake_usd = (self._trading.max_stake * theta_dec).copy_abs()

        if stake_usd <= 0:
            logger.warning(
                "Computed non-positive stake_usd, skipping signal",
                symbol=last.symbol,
                max_stake=str(self._trading.max_stake),
                theta=str(self._cfg.theta),
            )
            return None

        # В качестве оценки probability используем theta как прокси.
        # Детальная оценка p_win может доопределяться калибровочным сервисом.
        probability = theta_dec
        if probability < 0 or probability > 1:
            probability = Decimal("0.5")

        # --- Сборка кандидата-сигнала ---
        signal = Signal(
            symbol=last.symbol,
            direction=direction,
            entry_price=entry_price,
            stake_usd=stake_usd,
            probability=probability,
            strategy="AVI-5",
            strategy_version=self._strategy_version,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            stop_loss=stop_loss,
            queued_until=None,
            error_code=None,
            error_message=None,
        )

        # --- RiskManager: финальная проверка лимитов ---
        allowed, reason = await self._risk.check_limits(signal, now=now)
        if not allowed:
            logger.info(
                "Signal rejected by RiskManager",
                symbol=signal.symbol,
                direction=signal.direction,
                reason=reason,
                signal_id=str(signal.id),
            )
            return None

        logger.info(
            "Signal generated by AVI-5",
            symbol=signal.symbol,
            direction=signal.direction,
            entry=str(signal.entry_price),
            tp1=str(signal.tp1),
            tp2=str(signal.tp2),
            tp3=str(signal.tp3),
            sl=str(signal.stop_loss),
            stake_usd=str(signal.stake_usd),
        )
        return signal
