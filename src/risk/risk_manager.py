from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from redis.asyncio import Redis

from src.core.logging_config import get_logger
from src.core.models import Position, RiskLimits, Signal
from src.db.repositories.position_repository import PositionRepository
from src.risk.anti_churn import AntiChurnGuard
from src.risk.position_limits import can_open_position_for_base

__all__ = ["RiskManager"]

logger = get_logger("risk.risk_manager")


class RiskManager:
    """
    Централизованный риск-менеджер.

    Отвечает за:
    - проверку глобальных лимитов перед открытием новой позиции:
        * max_concurrent        — максимум одновременно открытых позиций;
        * max_total_risk_r      — максимум суммарного риска в R;
        * max_positions_per_symbol (per-base) — лимит на базовый актив;
        * per_symbol_risk_r     — дополнительные лимиты по отдельным символам;
    - применение anti-churn guard (через AntiChurnGuard);
    - работу поверх снимка RiskLimits, который может обновляться по мере ребалансировки.
    """

    def __init__(
        self,
        *,
        limits: RiskLimits,
        redis: Redis,
        position_repository: PositionRepository,
    ) -> None:
        """
        :param limits: Снимок актуальных риск-лимитов.
        :param redis: Экземпляр Redis для anti-churn guard.
        :param position_repository: Репозиторий позиций для получения открытых позиций.
        """
        self._limits = limits
        self._redis = redis
        self._positions = position_repository

    @property
    def limits(self) -> RiskLimits:
        """Текущий снимок риск-лимитов."""
        return self._limits

    def update_limits(self, limits: RiskLimits) -> None:
        """
        Обновить снимок риск-лимитов.

        Метод предполагается вызывать при изменении конфигурации/лимитов
        (например, после ручной правки конфигов или пересчёта risk budget).
        """
        self._limits = limits
        logger.info(
            "Risk limits updated",
            max_concurrent=self._limits.max_concurrent,
            max_total_risk_r=str(self._limits.max_total_risk_r),
            max_positions_per_symbol=self._limits.max_positions_per_symbol,
            per_symbol_risk_r={k: str(v) for k, v in self._limits.per_symbol_risk_r.items()},
        )

    async def check_limits(
        self,
        signal: Signal,
        *,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Проверить, можно ли открыть НОВУЮ позицию по данному сигналу.

        Возвращает (allowed, reason), где:
          * allowed == True  — все лимиты соблюдены, вход разрешён (anti-churn тоже прошёл);
          * allowed == False — сработало одно из ограничений, reason содержит краткий код причины:

                "anti_churn_block"     — символ/направление заблокированы по времени;
                "max_concurrent"       — достигнут лимит max_concurrent;
                "per_base_limit"       — нарушен per-base лимит по базовому активу;
                "max_total_risk_r"     — превышен лимит суммарного риска;
                "per_symbol_risk_r"    — превышен лимит риска по конкретному символу.

        Исключения:
        - Ошибки Redis / БД пробрасываются наверх (RedisError, DatabaseError и пр.).
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # 0) Anti-churn guard — первым делом проверяем, не пытаемся ли войти повторно в ту же сторону.
        blocked, block_until = await AntiChurnGuard.is_blocked(
            self._redis,
            signal.symbol,
            signal.direction,
            now=now,
        )
        if blocked:
            logger.info(
                "Signal blocked by anti-churn",
                symbol=signal.symbol,
                direction=signal.direction,
                block_until=block_until.isoformat() if block_until else None,
                signal_id=str(signal.id),
            )
            return False, "anti_churn_block"

        # 1) Забираем все открытые позиции.
        open_positions: List[Position] = await self._positions.list_open()

        # 2) Глобальный лимит по количеству одновременных позиций.
        if not self._check_max_concurrent(open_positions):
            logger.info(
                "Signal rejected: max_concurrent limit",
                symbol=signal.symbol,
                direction=signal.direction,
                signal_id=str(signal.id),
                open_positions=len(open_positions),
                max_concurrent=self._limits.max_concurrent,
            )
            return False, "max_concurrent"

        # 3) Per-base лимит (через вспомогательный модуль position_limits).
        if not self._check_per_base_limit(open_positions, signal):
            logger.info(
                "Signal rejected: per-base limit",
                symbol=signal.symbol,
                direction=signal.direction,
                signal_id=str(signal.id),
                max_positions_per_symbol=self._limits.max_positions_per_symbol,
            )
            return False, "per_base_limit"

        # 4) Лимит на суммарный риск в R.
        if not self._check_total_risk(open_positions):
            logger.info(
                "Signal rejected: max_total_risk_r limit",
                symbol=signal.symbol,
                direction=signal.direction,
                signal_id=str(signal.id),
                max_total_risk_r=str(self._limits.max_total_risk_r),
            )
            return False, "max_total_risk_r"

        # 5) Пер-символьные лимиты риска (per_symbol_risk_r).
        if not self._check_per_symbol_risk(open_positions, signal):
            logger.info(
                "Signal rejected: per-symbol risk limit",
                symbol=signal.symbol,
                direction=signal.direction,
                signal_id=str(signal.id),
            )
            return False, "per_symbol_risk_r"

        # Если дошли до сюда — все ограничения соблюдены.
        return True, None

    async def on_position_opened(
        self,
        position: Position,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """
        Хук, вызываемый после ФАКТИЧЕСКОГО открытия позиции.

        Основная задача — записать факт входа в anti-churn guard.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        await AntiChurnGuard.record_signal(
            self._redis,
            symbol=position.symbol,
            side=position.direction,
            now=now,
        )

    async def on_position_closed(self, position: Position) -> None:
        """
        Хук, вызываемый после закрытия позиции.

        По умолчанию ничего не делает с anti-churn (cooldown остаётся действовать),
        но оставлен для расширений (например, для явного clear_block при ручном вмешательстве).
        """
        # Здесь можно было бы вызывать AntiChurnGuard.clear_block(...) в особых сценариях.
        logger.debug(
            "Position closed, risk_manager hook called",
            position_id=str(position.id),
            symbol=position.symbol,
            direction=position.direction,
        )

    # === Внутренние проверки ===

    def _check_max_concurrent(self, open_positions: List[Position]) -> bool:
        """
        Проверка глобального лимита по количеству открытых позиций.
        """
        return len(open_positions) < self._limits.max_concurrent

    def _check_per_base_limit(
        self,
        open_positions: List[Position],
        signal: Signal,
    ) -> bool:
        """
        Проверка per-base лимита по базовому активу и направлению.

        Использует src.risk.position_limits.can_open_position_for_base.
        """
        return can_open_position_for_base(
            open_positions,
            symbol=signal.symbol,
            direction=signal.direction,
            max_positions_per_base=self._limits.max_positions_per_symbol,
        )

    def _check_total_risk(self, open_positions: List[Position]) -> bool:
        """
        Проверка лимита по суммарному риску в R.

        Предположение (соответствует типичному кейсу AVI-5):
          * каждая открытая позиция занимает 1R;
          * max_total_risk_r задаётся в тех же единицах (например, 3R, 4R, 5R).

        Тогда суммарный риск в R ~ количеству открытых позиций.
        Если спецификация/реализация изменится (например, появится явный размер R
        на уровне Position), эту логику можно будет скорректировать.
        """
        current_total_risk_r = Decimal(len(open_positions))
        proposed_total_risk_r = current_total_risk_r + Decimal("1")

        return proposed_total_risk_r <= self._limits.max_total_risk_r

    def _check_per_symbol_risk(
        self,
        open_positions: List[Position],
        signal: Signal,
    ) -> bool:
        """
        Проверка дополнительных лимитов риска per_symbol_risk_r.

        Интерпретация (консервативная и простая):
          * per_symbol_risk_r[symbol] задаёт максимальный риск в R по этому символу;
          * каждая позиция ~ 1R;
          * значит, per_symbol_risk_r[symbol] по сути задаёт максимум позиций по символу.

        Ключи в per_symbol_risk_r считаем регистронезависимыми и нормализуем к upper().
        """
        if not self._limits.per_symbol_risk_r:
            return True

        symbol_upper = signal.symbol.upper()

        # Пытаемся найти лимит по точному ключу, затем по upper() — на случай разной конвенции.
        per_symbol_limits: Dict[str, Decimal] = {
            k.upper(): v for k, v in self._limits.per_symbol_risk_r.items()
        }
        limit_for_symbol = per_symbol_limits.get(symbol_upper)
        if limit_for_symbol is None:
            # Для символа нет отдельного лимита — значит, достаточно глобальных ограничений.
            return True

        current_count = sum(1 for p in open_positions if p.symbol.upper() == symbol_upper)
        proposed_count_r = Decimal(current_count + 1)

        return proposed_count_r <= limit_for_symbol
