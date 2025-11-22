from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Optional
from uuid import uuid4

from redis.asyncio import Redis

from src.core.logging_config import get_logger

__all__ = ["RedisDistributedLock", "acquire_lock"]

logger = get_logger("core.distributed_lock")


_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


@dataclass(frozen=True)
class LockParams:
    """
    Параметры распределённой блокировки.

    ttl_sec:
        Время жизни блокировки в секундах. После истечения TTL ключ в Redis
        будет автоматически удалён, даже если владелец "забыл" его освободить.
    retry_interval_sec:
        Интервал между попытками захвата блокировки при режиме wait=True.
    max_wait_sec:
        Максимальное время ожидания блокировки. Если None — ждать бесконечно.
    """

    ttl_sec: float = 30.0
    retry_interval_sec: float = 0.1
    max_wait_sec: Optional[float] = 10.0


class RedisDistributedLock:
    """
    Примитив распределённой блокировки на Redis.

    Особенности реализации:

    - Используется команда SET key value NX PX ttl:
        * NX — только если ключ не существует;
        * PX — TTL в миллисекундах.
    - Значение value — случайный UUID; это позволяет безопасно освобождать
      только свою блокировку, даже если TTL истёк и ключ был перехвачен.
    - release() выполняется через Lua-скрипт (_RELEASE_SCRIPT), который
      атомарно сравнивает значение ключа и удаляет его только при совпадении.

    Использование:

        lock = RedisDistributedLock(redis, "reconciliation", LockParams(...))
        async with lock:
            # критическая секция

    или через хелпер acquire_lock(...).
    """

    def __init__(
        self,
        redis: Redis,
        name: str,
        params: Optional[LockParams] = None,
        *,
        wait: bool = True,
        key_prefix: str = "lock:",
    ) -> None:
        """
        :param redis: Экземпляр Redis (redis.asyncio.Redis).
        :param name: Логическое имя блокировки (будет частью ключа в Redis).
        :param params: Параметры TTL/ретраев.
        :param wait: Если False — делаем одну попытку захвата и сразу возвращаемся.
        :param key_prefix: Префикс для ключей блокировок в Redis.
        """
        self._redis = redis
        self._name = name
        self._params = params or LockParams()
        self._wait = wait

        self._key = f"{key_prefix}{name}"
        self._value = str(uuid4())
        self._locked = False

    # ------------------------------------------------------------------ #
    # Контекстный менеджер
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "RedisDistributedLock":
        acquired = await self.acquire()
        if not acquired:
            # Не бросаем исключение, чтобы можно было проверять lock.locked
            logger.info(
                "Failed to acquire distributed lock",
                key=self._key,
                name=self._name,
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._locked:
            try:
                await self.release()
            except Exception as release_exc:  # noqa: BLE001
                # На этом уровне мы не хотим ронять приложение из-за неудачной
                # попытки release — логируем и отпускаем дальше.
                logger.error(
                    "Failed to release distributed lock",
                    key=self._key,
                    name=self._name,
                    error=str(release_exc),
                )

    # ------------------------------------------------------------------ #
    # Публичные методы acquire / release
    # ------------------------------------------------------------------ #

    @property
    def locked(self) -> bool:
        """Признак того, что блокировка захвачена текущим инстансом."""
        return self._locked

    async def acquire(self) -> bool:
        """
        Попытаться захватить блокировку.

        :return: True, если блокировка захвачена текущим инстансом.
                 False, если захват не удался (при wait=False или по таймауту).
        :raises RedisError: ошибки Redis пробрасываются наверх.
        """
        ttl_ms = int(self._params.ttl_sec * 1000)
        retry_interval = self._params.retry_interval_sec
        deadline: Optional[float] = None

        if self._params.max_wait_sec is not None:
            deadline = time.monotonic() + self._params.max_wait_sec

        while True:
            # SET key value NX PX ttl
            ok = await self._redis.set(
                self._key,
                self._value,
                nx=True,
                px=ttl_ms,
            )
            if ok:
                self._locked = True
                logger.debug(
                    "Distributed lock acquired",
                    key=self._key,
                    name=self._name,
                    ttl_ms=ttl_ms,
                )
                return True

            if not self._wait:
                return False

            if deadline is not None and time.monotonic() >= deadline:
                logger.debug(
                    "Distributed lock acquire timeout",
                    key=self._key,
                    name=self._name,
                )
                return False

            await asyncio.sleep(retry_interval)

    async def release(self) -> None:
        """
        Освободить блокировку, если она ещё принадлежит текущему инстансу.

        Использует Lua-скрипт, который удаляет ключ только если значение совпадает
        с self._value. Это защищает от ситуации, когда TTL истёк, другой процесс
        захватил lock, а мы всё ещё пытаемся его "освободить".
        """
        if not self._locked:
            return

        try:
            # Тип Redis.eval в redis-py описан единым образом для sync/async API,
            # поэтому mypy видит здесь union вроде "Awaitable[Any] | str".
            # В нашем контексте eval всегда возвращает awaitable, поэтому
            # явно приводим тип к Awaitable[Any] перед await.
            eval_call: Awaitable[Any] = self._redis.eval(  # type: ignore[assignment]
                _RELEASE_SCRIPT,
                1,  # numkeys
                self._key,  # KEYS[1]
                self._value,  # ARGV[1]
            )
            res = await eval_call

            logger.debug(
                "Distributed lock released",
                key=self._key,
                name=self._name,
                result=int(res) if res is not None else None,
            )
        finally:
            self._locked = False


# ---------------------------------------------------------------------- #
# Удобный хелпер для единичного использования
# ---------------------------------------------------------------------- #


def acquire_lock(
    redis: Redis,
    name: str,
    *,
    params: Optional[LockParams] = None,
    wait: bool = True,
    key_prefix: str = "lock:",
) -> RedisDistributedLock:
    """
    Удобный фабричный хелпер:

        async with acquire_lock(redis, "reconciliation") as lock:
            if not lock.locked:
                return  # lock уже у кого-то другого
            # критическая секция

    :return: Экземпляр RedisDistributedLock, реализующий async context manager.
    """
    return RedisDistributedLock(
        redis=redis,
        name=name,
        params=params,
        wait=wait,
        key_prefix=key_prefix,
    )
