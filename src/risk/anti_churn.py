from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from redis.asyncio import Redis

from src.core.constants import CHURN_BLOCK_SEC


class AntiChurnGuard:
    """
    Anti-churn guard: блокировка повторного однонаправленного входа
    по символу на фиксированное время (CHURN_BLOCK_SEC, по умолчанию 15 минут).

    Логика:
    - при каждом подтверждённом входе вызывается record_signal(...) — пишет last_signal_time в Redis с TTL;
    - при появлении нового сигнала вызывается is_blocked(...):
        * если с момента last_signal_time прошло меньше CHURN_BLOCK_SEC, сигнал считается "зачёрненным";
        * вызывающий код может перевести такой сигнал в статус 'queued' и выставить queued_until;
    - clear_block(...) опционально используется при закрытии позиции,
      чтобы позволить немедленный повторный вход (например, при ручном управлении).
    """

    _KEY_TEMPLATE = "last_signal_time:{symbol}:{side}"

    @staticmethod
    def _make_key(symbol: str, side: str) -> str:
        """
        Собрать ключ Redis для хранения таймстемпа последнего сигнала.

        Symbol и side нормализуем к верхнему/нижнему регистру для предсказуемости.
        """
        return AntiChurnGuard._KEY_TEMPLATE.format(
            symbol=symbol.upper(),
            side=side.lower(),
        )

    @staticmethod
    async def is_blocked(
        redis: Redis,
        symbol: str,
        side: str,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[datetime]]:
        """
        Проверить, находится ли символ/направление в состоянии anti-churn блока.

        :param redis: Экземпляр Redis (redis.asyncio.Redis).
        :param symbol: Торговый символ (например, "BTCUSDT").
        :param side: Направление сигнала: "long" или "short".
        :param now: Текущий момент времени; если не указан — используется UTC now.
        :return: Кортеж (blocked, block_until):
                 - blocked == True, если с момента последнего сигнала прошло
                   меньше CHURN_BLOCK_SEC секунд;
                 - block_until — момент, когда блок истечёт, либо None, если блок не активен.

        Исключения:
        - Любые ошибки Redis пробрасываются наверх (RedisError и др.).
        """
        if now is None:
            now = datetime.now(timezone.utc)

        key = AntiChurnGuard._make_key(symbol, side)
        raw_ts = await redis.get(key)
        if raw_ts is None:
            # Нет записанного сигнала — блок не активен.
            return False, None

        try:
            last_ts = float(raw_ts)
        except (TypeError, ValueError):
            # Если в ключе неожиданно мусор — защитно считаем, что блок не активен,
            # а вызывающий код может переписать ключ через record_signal.
            return False, None

        last_time = datetime.fromtimestamp(last_ts, tz=timezone.utc)
        elapsed_sec = (now - last_time).total_seconds()

        if elapsed_sec >= CHURN_BLOCK_SEC:
            # Время блока уже прошло — можно считать, что блок не активен.
            return False, None

        block_until = last_time + timedelta(seconds=CHURN_BLOCK_SEC)
        return True, block_until

    @staticmethod
    async def record_signal(
        redis: Redis,
        symbol: str,
        side: str,
        now: Optional[datetime] = None,
    ) -> None:
        """
        Зафиксировать факт нового сигнала по символу/направлению.

        Записывает текущий таймстемп в Redis с TTL = CHURN_BLOCK_SEC.
        Используется при подтверждении сигнала, который прошёл все остальные проверки.

        :param redis: Экземпляр Redis.
        :param symbol: Торговый символ (например, "BTCUSDT").
        :param side: Направление сигнала: "long" или "short".
        :param now: Момент времени сигнала; если не указан — используется UTC now.

        Исключения:
        - Любые ошибки Redis пробрасываются наверх.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        key = AntiChurnGuard._make_key(symbol, side)
        ts = now.timestamp()

        # setex(key, ttl, value) — атомарная установка значения с TTL.
        await redis.setex(key, CHURN_BLOCK_SEC, str(ts))

    @staticmethod
    async def clear_block(
        redis: Redis,
        symbol: str,
        side: str,
    ) -> None:
        """
        Очистить anti-churn блок по символу/направлению.

        Используется, например, при ручном вмешательстве оператора или
        специальных сценариях, когда повторный вход нужно разрешить немедленно.

        :param redis: Экземпляр Redis.
        :param symbol: Торговый символ.
        :param side: Направление ("long"/"short").
        """
        key = AntiChurnGuard._make_key(symbol, side)
        await redis.delete(key)


__all__ = ["AntiChurnGuard"]
