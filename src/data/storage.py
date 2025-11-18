from __future__ import annotations

from typing import Any, Dict

import asyncpg

from src.core.exceptions import DatabaseError


async def save_kline(pool: asyncpg.Pool, kline: Dict[str, Any]) -> None:
    """
    Сохранить одну OHLCV-свечу 5m в таблицу `klines_5m`.

    Ожидается, что kline содержит ключи:
        - ts: datetime (UTC)
        - symbol: str
        - open, high, low, close: число (Decimal/float)
        - volume: число (Decimal/float)

    При ошибках БД выбрасывает DatabaseError, в т.ч. при дубликате
    по уникальному ключу (ts, symbol).
    """
    sql = (
        "INSERT INTO klines_5m "
        "(ts, symbol, open, high, low, close, volume) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)"
    )

    try:
        await pool.execute(
            sql,
            kline["ts"],
            kline["symbol"],
            kline["open"],
            kline["high"],
            kline["low"],
            kline["close"],
            kline["volume"],
        )
    except asyncpg.PostgresError as exc:
        # SQLSTATE 23505 — unique_violation (дубликат по ts+symbol).
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate == "23505":
            message = "Duplicate kline for (ts, symbol) in klines_5m"
        else:
            message = "Database error while inserting kline into klines_5m"

        raise DatabaseError(
            message,
            details={
                "sqlstate": sqlstate or "",
                "symbol": kline.get("symbol"),
                "ts": str(kline.get("ts")),
            },
        ) from exc
