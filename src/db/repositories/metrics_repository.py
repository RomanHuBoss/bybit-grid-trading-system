from __future__ import annotations

from typing import Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core.exceptions import DatabaseError
from src.core.logging_config import get_logger
from src.db.connection import get_pool

__all__ = ["MetricsRepository"]

logger = get_logger("db.repositories.metrics_repository")

# TTL кэша метрик в Redis, секунд
DEFAULT_CACHE_TTL_SECONDS: int = 60


class MetricsRepository:
    """
    Репозиторий для чтения агрегированных метрик стратегий для Grafana.

    Источник данных — PostgreSQL (таблица `positions` и связанные сущности),
    поверх которых могут быть настроены materialized view / TimescaleDB
    continuous aggregates.

    Метрики кэшируются в Redis на короткий срок (по умолчанию 60 секунд),
    чтобы не нагружать БД при частых запросах со стороны Grafana / API.
    """

    def __init__(self, redis: Redis, cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        """
        :param redis: Асинхронный клиент Redis.
        :param cache_ttl_seconds: TTL для значений метрик в кэше, по умолчанию 60 секунд.
        """
        self._redis: Redis = redis
        self._cache_ttl_seconds: int = cache_ttl_seconds

    async def get_win_rate_last_30d(self) -> float:
        """
        Вернуть Win Rate за последние 30 дней.

        Win Rate определяется как:
            wins / total_closed

        где:
            wins  — число закрытых позиций с pnl_usd > 0,
            total_closed — общее число закрытых позиций
                           за последние 30 дней (по closed_at).

        :return: Значение Win Rate в диапазоне [0.0, 1.0]. При отсутствии данных — 0.0.
        :raises DatabaseError: при ошибках доступа к БД.
        """
        cache_key = "metrics:win_rate:last_30d"

        sql = """
            SELECT
                CASE
                    WHEN COUNT(*) = 0 THEN 0.0
                    ELSE COUNT(CASE WHEN pnl_usd > 0 THEN 1 END)::float
                         / COUNT(*)::float
                END AS win_rate
            FROM positions
            WHERE closed_at IS NOT NULL
              AND closed_at >= (NOW() - INTERVAL '30 days')
        """

        return await self._get_metric_from_db_with_cache(cache_key, sql, default=0.0)

    async def get_profit_factor_last_30d(self) -> float:
        """
        Вернуть Profit Factor за последние 30 дней.

        Profit Factor определяется как:
            gross_profit / ABS(gross_loss)

        где:
            gross_profit — сумма pnl_usd по прибыльным позициям (> 0),
            gross_loss   — сумма pnl_usd по убыточным позициям (< 0).

        При отсутствии убыточных сделок (gross_loss = 0) возвращается 0.0,
        чтобы избежать деления на ноль и странных значений PF.

        :return: Profit Factor (>= 0). При отсутствии данных — 0.0.
        :raises DatabaseError: при ошибках доступа к БД.
        """
        cache_key = "metrics:profit_factor:last_30d"

        sql = """
            WITH closed_positions AS (
                SELECT pnl_usd
                FROM positions
                WHERE closed_at IS NOT NULL
                  AND closed_at >= (NOW() - INTERVAL '30 days')
            ),
            agg AS (
                SELECT
                    COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END), 0) AS gross_profit,
                    COALESCE(SUM(CASE WHEN pnl_usd < 0 THEN pnl_usd ELSE 0 END), 0) AS gross_loss
                FROM closed_positions
            )
            SELECT
                CASE
                    WHEN gross_profit = 0 THEN 0.0
                    WHEN gross_loss = 0 THEN 0.0
                    ELSE gross_profit / ABS(gross_loss)
                END AS profit_factor
            FROM agg
        """

        return await self._get_metric_from_db_with_cache(cache_key, sql, default=0.0)

    async def get_max_drawdown_last_30d(self) -> float:
        """
        Вернуть Max Drawdown (просадку) за последние 30 дней в процентах.

        Здесь предполагается, что MaxDD считается по equity-кривой стратегий
        на основе таблицы `positions` / агрегированных данных.

        Упрощённый вариант:
        - строим equity как накопленный PnL от начала окна 30 дней;
        - считаем максимальную просадку от текущего пика.

        Итоговое значение — в процентах (0.0 означает отсутствие просадки).

        :return: Max Drawdown в процентах (>= 0). При отсутствии данных — 0.0.
        :raises DatabaseError: при ошибках доступа к БД.
        """
        cache_key = "metrics:max_drawdown:last_30d"

        # Упрощённый расчёт MaxDD: на уровне SQL считаем equity и максимальную просадку.
        # В реальной схеме это может быть вынесено в materialized view / continuous aggregate.
        sql = """
            WITH closes AS (
                SELECT
                    closed_at::date AS dt,
                    COALESCE(SUM(pnl_usd), 0) AS pnl_sum
                FROM positions
                WHERE closed_at IS NOT NULL
                  AND closed_at >= (NOW() - INTERVAL '30 days')
                GROUP BY closed_at::date
            ),
            equity_curve AS (
                SELECT
                    dt,
                    SUM(pnl_sum) OVER (ORDER BY dt ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS equity
                FROM closes
            ),
            equity_with_peak AS (
                SELECT
                    dt,
                    equity,
                    MAX(equity) OVER (ORDER BY dt ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS peak_equity
                FROM equity_curve
            ),
            drawdowns AS (
                SELECT
                    dt,
                    CASE
                        WHEN peak_equity <= 0 THEN 0.0
                        ELSE (peak_equity - equity) / NULLIF(peak_equity, 0) * 100.0
                    END AS dd_pct
                FROM equity_with_peak
            )
            SELECT COALESCE(MAX(dd_pct), 0.0) AS max_drawdown_pct
            FROM drawdowns
        """

        return await self._get_metric_from_db_with_cache(cache_key, sql, default=0.0)

    async def get_median_slippage_last_24h(self) -> float:
        """
        Вернуть медиану входного slippage (slippage_entry_bps) за последние 24 часа.

        Используются позиции, у которых:
            - есть slippage_entry_bps,
            - позиция закрыта (closed_at IS NOT NULL),
            - closed_at попадает в окно последних 24 часов.

        :return: Медиана slippage_entry_bps в bps. При отсутствии данных — 0.0.
        :raises DatabaseError: при ошибках доступа к БД.
        """
        cache_key = "metrics:median_slippage_entry:last_24h"

        sql = """
            SELECT
                COALESCE(
                    percentile_disc(0.5) WITHIN GROUP (ORDER BY slippage_entry_bps),
                    0.0
                ) AS median_slippage_entry_bps
            FROM positions
            WHERE closed_at IS NOT NULL
              AND closed_at >= (NOW() - INTERVAL '24 hours')
              AND slippage_entry_bps IS NOT NULL
        """

        return await self._get_metric_from_db_with_cache(cache_key, sql, default=0.0)

    async def refresh_cache(self) -> None:
        """
        Полностью очистить кэш метрик в Redis.

        Удаляются все ключи, начинающиеся с префикса `metrics:`.

        :raises RedisError: при ошибках доступа к Redis.
        """
        try:
            cursor: int = 0
            pattern = "metrics:*"

            # SCAN по ключам с указанным паттерном
            while True:
                cursor, keys = await self._redis.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    # Redis возвращает bytes/str; delete принимает *keys без преобразования
                    await self._redis.delete(*keys)

                if cursor == 0:
                    break

            logger.info("Metrics cache successfully refreshed", prefix=pattern)
        except RedisError as exc:
            logger.error("Failed to refresh metrics cache in Redis", error=str(exc))
            # по спецификации метод пробрасывает RedisError наружу
            raise

    async def _get_metric_from_db_with_cache(
        self,
        cache_key: str,
        sql: str,
        *,
        default: float = 0.0,
    ) -> float:
        """
        Универсальный помощник для чтения метрики с кэшем в Redis.

        Алгоритм:
        1. Попытаться прочитать значение из Redis.
        2. Если кэш отсутствует/битый — выполнить запрос к БД.
        3. Сохранить результат в Redis с TTL.
        4. Вернуть значение как float.

        :param cache_key: Ключ в Redis для данной метрики.
        :param sql: SQL-запрос, возвращающий одно скалярное значение.
        :param default: Значение по умолчанию, если SQL вернул NULL или не нашёл строк.
        :return: Значение метрики.
        :raises DatabaseError: при ошибках уровня БД.
        """
        # 1. Попытка чтения из Redis
        try:
            cached: Optional[bytes] = await self._redis.get(cache_key)
            if cached is not None:
                try:
                    value = float(cached.decode("utf-8"))
                    logger.debug("Metric loaded from Redis cache", key=cache_key, value=value)
                    return value
                except (ValueError, UnicodeDecodeError):
                    logger.warning(
                        "Invalid value in Redis cache for metric, will recompute",
                        key=cache_key,
                        raw_value=repr(cached),
                    )
        except RedisError as exc:
            # Ошибки Redis для чтения кэша не фатальны: логируем и идём в БД.
            logger.error(
                "Failed to read metric from Redis cache",
                key=cache_key,
                error=str(exc),
            )

        # 2. Чтение из БД
        pool = get_pool()
        try:
            async with pool.acquire() as conn:
                raw_value = await conn.fetchval(sql)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to fetch metric from database",
                key=cache_key,
                error=str(exc),
            )
            raise DatabaseError(
                "Failed to fetch metric from database",
                details={"cache_key": cache_key},
            ) from exc

        if raw_value is None:
            value = float(default)
        else:
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                logger.error(
                    "Metric query returned non-numeric value",
                    key=cache_key,
                    raw_value=repr(raw_value),
                    error=str(exc),
                )
                # В случае некорректного значения считаем метрику как default
                value = float(default)

        # 3. Запись в Redis (best-effort)
        try:
            await self._redis.set(cache_key, str(value), ex=self._cache_ttl_seconds)
            logger.debug(
                "Metric value stored in Redis cache",
                key=cache_key,
                value=value,
                ttl=self._cache_ttl_seconds,
            )
        except RedisError as exc:
            logger.error(
                "Failed to store metric in Redis cache",
                key=cache_key,
                error=str(exc),
            )

        # 4. Возвращаем результат
        return value
