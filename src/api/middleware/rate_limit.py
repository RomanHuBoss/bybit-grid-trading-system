from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable, DefaultDict, List

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

__all__ = ["RateLimitConfig", "IPRateLimitMiddleware"]

RequestResponseEndpoint = Callable[[Request], Awaitable[Response]]


@dataclass(frozen=True)
class RateLimitConfig:
    """
    Конфиг IP-rate-лимита для HTTP-API.

    Это лёгкий in-memory rate limiter на уровне процесса приложения.
    Вся «серьёзная» защита от биржевых лимитов реализуется в модуле
    `src/integration/bybit/rate_limiter.py`. Данный конфиг используется
    только для ограничения частоты запросов от одного IP (обычно UI).

    max_requests_per_window:
        Максимальное число запросов от одного IP в пределах окна.
        Если <= 0 — лимит выключен.
    window_seconds:
        Размер sliding-окна в секундах (по умолчанию 60 секунд = 1 минута).
    """

    max_requests_per_window: int = 100
    window_seconds: int = 60


class IPRateLimitMiddleware(BaseHTTPMiddleware):
    """
    Простейший IP-based rate limiter для HTTP-API (опциональный).

    Логика:
    - для каждого IP ведётся список timestamp'ов запросов (sliding window);
    - при каждом запросе:
        * чистим старые записи за пределами window_seconds;
        * если после этого количество запросов >= max_requests_per_window —
          отдаём 429;
        * иначе добавляем текущий timestamp и пропускаем запрос дальше.

    Для измерения времени используется time.monotonic(), чтобы избежать
    проблем с изменениями системных часов.

    Формат ошибки выровнен с общими правилами API: JSON с полем ``detail``.
    """

    def __init__(self, app, config: RateLimitConfig | None = None) -> None:  # type: ignore[override]
        super().__init__(app)
        self._config = config or RateLimitConfig()
        # ip -> [timestamps]
        self._requests: DefaultDict[str, List[float]] = defaultdict(list)

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """
        Применить rate limit к запросу и передать его дальше по цепочке.

        Если лимит отключён (max_requests_per_window <= 0), middleware
        ведёт себя прозрачно.
        """
        if self._config.max_requests_per_window <= 0:
            return await call_next(request)

        ip = self._extract_ip(request)
        now = time.monotonic()

        self._cleanup_window(ip, now)

        timestamps = self._requests[ip]
        if len(timestamps) >= self._config.max_requests_per_window:
            # Лимит превышен — 429 Too Many Requests.
            # Формат ответа приведён к JSON + заголовок Retry-After,
            # чтобы клиенты могли корректно реагировать.
            retry_after = 0
            if timestamps:
                # Когда «освободится» окно: через window_seconds с момента
                # самого старого запроса в списке.
                retry_after = max(
                    0,
                    int(self._config.window_seconds - (now - timestamps[0])),
                )

            return JSONResponse(
                {"detail": "Too Many Requests"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        # Запрос в пределах лимита — добавляем таймстемп и пропускаем.
        timestamps.append(now)
        return await call_next(request)

    # --------------------------------------------------------------------- #
    # Внутренние утилиты
    # --------------------------------------------------------------------- #

    def _cleanup_window(self, ip: str, now: float) -> None:
        """
        Очистить таймстемпы, вышедшие за пределы sliding-окна.
        """
        window_start = now - self._config.window_seconds
        timestamps = self._requests[ip]

        # Оставляем только запросы внутри окна.
        # Список обычно небольшой, поэтому линейный проход допустим.
        self._requests[ip] = [ts for ts in timestamps if ts >= window_start]

        # Небольшая оптимизация памяти: если список опустел — удаляем ключ.
        if not self._requests[ip]:
            del self._requests[ip]

    @staticmethod
    def _extract_ip(request: Request) -> str:
        """
        Вытащить IP-адрес клиента.

        По умолчанию берём request.client.host. При использовании reverse-proxy
        (nginx, traefik) в будущем можно расширить до X-Forwarded-For / X-Real-IP.
        """
        client = request.client
        if client is None:
            return "unknown"
        return client.host or "unknown"
