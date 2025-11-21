from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from redis.asyncio import Redis

from src.core.logging_config import get_logger

__all__ = ["AntiChurnGuard"]

logger = get_logger("risk.anti_churn")


class AntiChurnGuard:
    """
    Простейший anti-churn guard поверх Redis.

    Назначение:
        - после входа по символу/направлению (symbol + direction)
          блокировать повторные входы на заданное окно времени;
        - при запросе нового сигнала отвечать, можно ли входить.

    Формат ключа:
        anti_churn:{SYMBOL_UPPER}:{DIRECTION_LOWER}

    Значение:
        ISO-строка времени истечения блокировки (UTC).

    TTL:
        - задаётся целым числом секунд;
        - может приходить извне (из конфига / RiskLimits) — но здесь есть
          дефолт и чтение из окружения (ANTI_CHURN_TTL_SECONDS), чтобы
          утилиты и тесты могли его переопределять.
    """

    # Дефолтное окно блокировки, секунд.
    # Нормальное значение задаётся конфигом, но этот дефолт безопасен и
    # используется только как запасной вариант.
    DEFAULT_TTL_SECONDS: int = 300

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """
        Нормализовать символ к верхнему регистру.

        Anti-churn должен быть инвариантен к регистру.
        """
        return symbol.upper()

    @staticmethod
    def _normalize_direction(direction: str) -> str:
        """
        Нормализовать направление к нижнему регистру.

        Ожидаемые значения: "long" / "short".
        """
        return direction.lower()

    @classmethod
    def _make_key(cls, symbol: str, direction: str) -> str:
        """
        Сформировать ключ Redis для пары (symbol, direction).
        """
        symbol_norm = cls._normalize_symbol(symbol)
        direction_norm = cls._normalize_direction(direction)
        return f"anti_churn:{symbol_norm}:{direction_norm}"

    @classmethod
    def _resolve_ttl_seconds(cls, ttl_seconds: Optional[int]) -> int:
        """
        Определить TTL блокировки.

        Приоритет:
            1) Явно переданное значение ttl_seconds (если > 0);
            2) Переменная окружения ANTI_CHURN_TTL_SECONDS (если > 0);
            3) DEFAULT_TTL_SECONDS.
        """
        if ttl_seconds is not None and ttl_seconds > 0:
            return ttl_seconds

        from os import getenv

        env_value = getenv("ANTI_CHURN_TTL_SECONDS")
        if env_value:
            try:
                value = int(env_value)
                if value > 0:
                    return value
            except ValueError:
                logger.warning(
                    "Invalid ANTI_CHURN_TTL_SECONDS value in environment",
                    raw_value=env_value,
                )

        return cls.DEFAULT_TTL_SECONDS

    # --------------------------------------------------------------------- #
    # Публичный async API                                                   #
    # --------------------------------------------------------------------- #

    @classmethod
    async def is_blocked(
        cls,
        redis: Redis,
        symbol: str,
        direction: str,
        *,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[datetime]]:
        """
        Проверить, заблокирован ли вход по символу/направлению.

        :param redis: Async Redis client.
        :param symbol: Символ инструмента.
        :param direction: Направление ("long" / "short").
        :param now: Текущее время (UTC). Если None — будет взято `datetime.now(timezone.utc)`.

        :return: (is_blocked, block_until). Если не заблокировано — (False, None).

        При сбоях Redis поведение — fail-closed: считаем, что вход заблокирован,
        чтобы не ослаблять защиту по рискам.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        key = cls._make_key(symbol, direction)

        try:
            value = await redis.get(key)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to read anti-churn key from Redis; failing closed",
                error=str(exc),
                symbol=symbol,
                direction=direction,
                redis_error=repr(exc),
            )
            # fail-closed: при проблемах Redis блокируем вход
            return True, None

        if value is None:
            # Блокировки нет.
            return False, None

        try:
            # В Redis храним ISO-строку UTC.
            block_until = datetime.fromisoformat(value.decode("utf-8"))
            if block_until.tzinfo is None:
                block_until = block_until.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            # Нечитаемое значение — лучше удалить ключ и считать, что блокировки нет.
            logger.warning(
                "Invalid anti-churn value in Redis, deleting key",
                key=key,
                raw_value=value,
            )
            try:
                await redis.delete(key)
            except Exception:
                logger.exception("Failed to delete invalid anti-churn key", key=key)
            return False, None

        # Если окно уже истекло — удаляем ключ и разрешаем вход.
        if block_until <= now:
            try:
                await redis.delete(key)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to delete expired anti-churn key", key=key)
            return False, None

        # Блокировка активна.
        return True, block_until

    @classmethod
    async def record_signal(
        cls,
        redis: Redis,
        *,
        symbol: str,
        side: str,
        now: Optional[datetime] = None,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """
        Зафиксировать факт входа по символу/направлению.

        Устанавливает Redis-ключ с TTL. Значение — время истечения окна блокировки.

        :param redis: Async Redis client.
        :param symbol: Символ инструмента (например, "BTCUSDT").
        :param side: Направление (long/short).
        :param now: Текущее время (UTC). Если None — будет взято системное.
        :param ttl_seconds: Явный TTL окна блокировки, если нужно переопределить
                            дефолт/конфиг (например, в тестах).
        """
        if now is None:
            now = datetime.now(timezone.utc)

        ttl = cls._resolve_ttl_seconds(ttl_seconds)
        block_until = now + timedelta(seconds=ttl)
        key = cls._make_key(symbol, side)

        value = block_until.isoformat()

        try:
            # setex: установить значение и TTL атомарно
            await redis.setex(key, ttl, value)
        except Exception as exc:  # noqa: BLE001
            # Ошибка Redis не должна ронять основной процесс, но мы её логируем.
            logger.error(
                "Failed to record anti-churn entry in Redis",
                error=str(exc),
                symbol=symbol,
                direction=side,
                ttl_seconds=ttl,
            )

    @classmethod
    async def clear_block(
        cls,
        redis: Redis,
        symbol: str,
        direction: str,
    ) -> None:
        """
        Снять блокировку по символу/направлению.

        Используется в административных сценариях и для ручного вмешательства
        (например, если позицию принудительно закрыли и хотим убрать cooldown).
        """
        key = cls._make_key(symbol, direction)
        try:
            await redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to clear anti-churn block",
                error=str(exc),
                symbol=symbol,
                direction=direction,
            )
