from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Mapping, Optional

from redis.asyncio import Redis

from src.core.distributed_lock import acquire_lock
from src.core.logging_config import get_logger
from src.db.connection import get_pool

__all__ = ["ArchiverConfig", "ArchiverService"]

logger = get_logger("core.archiver")


@dataclass(frozen=True)
class ArchiverConfig:
    """
    Конфигурация ретеншена и архивации исторических данных.

    По R-08:
      - сигналы (`signals`) храним в основной БД 90 дней;
      - позиции (`positions`) храним 180 дней;
    затем записи выгружаются в S3 и удаляются из основной БД.
    """

    signals_retention_days: int = 90
    positions_retention_days: int = 180
    batch_size: int = 1000

    s3_bucket: Optional[str] = None
    s3_prefix: str = "bybit-algo-grid/archive"

    enabled: bool = True


class ArchiverService:
    """
    Сервис архивации исторических сигналов и позиций.

    Задачи:
    - запускаться внешним планировщиком;
    - под Redis-lock'ом выбирать записи старше retention-порогов;
    - пачками выгружать их в S3 (NDJSON + gzip);
    - после успешной выгрузки удалять записи из основной БД.
    """

    def __init__(
        self,
        *,
        redis: Redis,
        s3_client: Any,
        config: Optional[ArchiverConfig] = None,
        lock_name: str = "archiver",
    ) -> None:
        """
        :param redis: Экземпляр Redis для распределённой блокировки.
        :param s3_client: Клиент S3 (обычно boto3.client("s3")) с методом
                          put_object(Bucket=..., Key=..., Body=..., ...).
        :param config: Конфигурация ретеншена/архивации.
        :param lock_name: Имя lock'а в Redis.
        """
        self._redis = redis
        self._s3 = s3_client
        self._cfg = config or ArchiverConfig()
        self._lock_name = lock_name

    # ------------------------------------------------------------------ #
    # Публичный API
    # ------------------------------------------------------------------ #

    async def run_once(self, *, now: Optional[datetime] = None) -> None:
        """
        Выполнить один проход архивации.

        Если:
          - archiver выключен (enabled=False), или
          - не задан s3_bucket,
        то метод ничего не делает, только пишет в лог.
        """
        if not self._cfg.enabled:
            logger.debug("Archiver is disabled in config, skipping run")
            return

        if not self._cfg.s3_bucket:
            logger.warning("Archiver has no S3 bucket configured, skipping run")
            return

        if now is None:
            now = datetime.now(timezone.utc)

        async with acquire_lock(self._redis, self._lock_name) as lock:
            if not lock.locked:
                logger.info(
                    "Archiver run skipped: lock is held by another worker",
                    lock_name=self._lock_name,
                )
                return

            await self._do_run(now=now)

    # ------------------------------------------------------------------ #
    # Основная логика
    # ------------------------------------------------------------------ #

    async def _do_run(self, *, now: datetime) -> None:
        signals_cutoff = now - timedelta(days=self._cfg.signals_retention_days)
        positions_cutoff = now - timedelta(days=self._cfg.positions_retention_days)

        logger.info(
            "Starting archiver run",
            signals_cutoff=signals_cutoff.isoformat(),
            positions_cutoff=positions_cutoff.isoformat(),
        )

        total_signals = 0
        total_positions = 0

        # Архивируем пачками, пока есть что архивировать.
        while True:
            sig_batch = await self._archive_signals_batch(cutoff=signals_cutoff, now=now)
            pos_batch = await self._archive_positions_batch(cutoff=positions_cutoff, now=now)

            total_signals += sig_batch
            total_positions += pos_batch

            if sig_batch == 0 and pos_batch == 0:
                break

        logger.info(
            "Archiver run completed",
            signals_archived=total_signals,
            positions_archived=total_positions,
        )

    # ------------------------------------------------------------------ #
    # Архивация signals
    # ------------------------------------------------------------------ #

    async def _archive_signals_batch(self, *, cutoff: datetime, now: datetime) -> int:
        """
        Архивировать одну пачку сигналов старше cutoff.

        Возвращает количество архивированных (и удалённых) строк.
        """
        pool = get_pool()
        async with pool.acquire() as conn:
            rows: List[Mapping[str, object]] = await conn.fetch(
                """
                SELECT *
                FROM signals
                WHERE created_at < $1
                ORDER BY created_at
                LIMIT $2
                """,
                cutoff,
                self._cfg.batch_size,
            )

        if not rows:
            return 0

        await self._archive_rows_to_s3(
            table="signals",
            rows=rows,
            now=now,
        )

        ids = [row["id"] for row in rows]

        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signals WHERE id = ANY($1::uuid[])",
                ids,
            )

        logger.info(
            "Archived and deleted signals batch",
            count=len(rows),
        )

        return len(rows)

    # ------------------------------------------------------------------ #
    # Архивация positions
    # ------------------------------------------------------------------ #

    async def _archive_positions_batch(self, *, cutoff: datetime, now: datetime) -> int:
        """
        Архивировать одну пачку позиций старше cutoff.

        Возраст позиции считаем по COALESCE(closed_at, opened_at).
        """
        pool = get_pool()
        async with pool.acquire() as conn:
            rows: List[Mapping[str, object]] = await conn.fetch(
                """
                SELECT *
                FROM positions
                WHERE COALESCE(closed_at, opened_at) < $1
                ORDER BY COALESCE(closed_at, opened_at)
                LIMIT $2
                """,
                cutoff,
                self._cfg.batch_size,
            )

        if not rows:
            return 0

        await self._archive_rows_to_s3(
            table="positions",
            rows=rows,
            now=now,
        )

        ids = [row["id"] for row in rows]

        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM positions WHERE id = ANY($1::uuid[])",
                ids,
            )

        logger.info(
            "Archived and deleted positions batch",
            count=len(rows),
        )

        return len(rows)

    # ------------------------------------------------------------------ #
    # S3-архивация
    # ------------------------------------------------------------------ #

    async def _archive_rows_to_s3(
        self,
        *,
        table: str,
        rows: Iterable[Mapping[str, object]],
        now: datetime,
    ) -> str:
        """
        Выгрузить пачку строк в S3 в формате NDJSON (gzip) и вернуть ключ объекта.

        Формат ключа:
          {s3_prefix}/{table}/yyyy/mm/dd/{table}-{timestamp}.ndjson.gz
        """
        line_iter = (
            json.dumps(dict(row), default=str, ensure_ascii=False)
            for row in rows
        )
        payload = "\n".join(line_iter).encode("utf-8")

        gz_payload = gzip.compress(payload)

        date = now.date()
        timestamp = now.strftime("%Y%m%dT%H%M%S")
        key = (
            f"{self._cfg.s3_prefix.rstrip('/')}/"
            f"{table}/"
            f"{date.year:04d}/{date.month:02d}/{date.day:02d}/"
            f"{table}-{timestamp}.ndjson.gz"
        )

        # Вызов S3-клиента выносим в отдельный поток, чтобы не блокировать event loop.
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self._cfg.s3_bucket,
            Key=key,
            Body=gz_payload,
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
        )

        logger.info(
            "Archived batch to S3",
            table=table,
            key=key,
            size_bytes=len(gz_payload),
        )

        return key
