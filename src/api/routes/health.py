from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from redis.asyncio import Redis

from src.db.connection import get_pool

__all__ = ["router"]

router = APIRouter(prefix="/health", tags=["health"])


class HealthComponents(BaseModel):
    """Состояние основных компонент системы для health-checkа.

    Поля и значения выровнены с документацией `docs/api.md`, раздел 9.1.
    Каждая компонента может находиться в одном из состояний:
    - ``"up"`` — компонент доступен и отвечает;
    - ``"down"`` — компонент недоступен;
    - ``"degraded"`` — компонент работает, но с проблемами (резерва на будущее).
    """

    db: Literal["up", "down", "degraded"]
    redis: Literal["up", "down", "degraded"]
    bybit_ws: Literal["up", "down", "degraded"]
    bybit_rest: Literal["up", "down", "degraded"]


class HealthResponse(BaseModel):
    """Ответ для GET `/health`.

    Соответствует контракту из `docs/api.md` (§9.1):

        {
          "status": "ok",
          "components": {
            "db": "up",
            "redis": "up",
            "bybit_ws": "up",
            "bybit_rest": "up"
          }
        }

    При частичной деградации статус может быть ``"degraded"``.
    """

    status: Literal["ok", "degraded"]
    components: HealthComponents


class LiveResponse(BaseModel):
    """Ответ для liveness-проверки.

    Минимальный контракт, достаточный для Kubernetes livenessProbe.
    Отдельно описан в `project_overview.md` (функция `health_check`).

    Liveness **не** проверяет внешние зависимости и отражает только факт,
    что процесс жив и способен обрабатывать HTTP-запросы.
    """

    status: Literal["alive"]
    ts: datetime


class ReadyResponse(BaseModel):
    """Ответ для readiness-проверки.

    Используется для Kubernetes readinessProbe. Здесь нас интересуют
    только "готовность" ядра приложения к обработке запросов:
    - успешный ping Redis;
    - успешный запрос к PostgreSQL.

    Поле ``checks`` позволяет быстро понять, какая зависимость не готова.
    """

    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]
    ts: datetime


async def _get_redis(request: Request) -> Redis | None:
    """Получить Redis-клиент из состояния приложения.

    Ожидается, что в `src/main.py` в процессе старта будет выполнено:

        app.state.redis = Redis.from_url(...)

    Если Redis не инициализирован или имеет неподходящий тип, мы
    интерпретируем это как недоступность зависимости и возвращаем None.
    Конкретный HTTP-статус будет сформирован на уровне эндпоинта.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None or not isinstance(redis, Redis):
        return None
    return redis


async def _check_db() -> Literal["up", "down"]:
    """Проверка доступности PostgreSQL.

    - Получаем пул соединений через `get_pool()`;
    - выполняем простой запрос `SELECT 1`;
    - в случае любого исключения считаем базу недоступной.

    Детальная диагностика ошибок остаётся в логах уровня `src/db/connection.py`.
    """
    try:
        pool = get_pool()
    except Exception:  # noqa: BLE001
        return "down"

    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception:  # noqa: BLE001
        return "down"

    return "up"


async def _check_redis(redis: Redis | None) -> Literal["up", "down"]:
    """Проверка доступности Redis через команду PING.

    Если Redis-клиент отсутствует (None), считаем зависимость недоступной.
    """
    if redis is None:
        return "down"

    try:
        await redis.ping()
    except Exception:  # noqa: BLE001
        return "down"
    return "up"


def _check_bybit_ws() -> Literal["up", "degraded"]:
    """Проверка состояния Bybit WebSocket.

    На текущем этапе проект не хранит явного глобального состояния
    WS-коннектора (см. `src/integration/bybit/ws_client.py`), поэтому
    считаем его ``"up"`` по умолчанию. При внедрении явных health-метрик
    сюда можно добавить чтение соответствующих показателей.
    """
    return "up"


def _check_bybit_rest() -> Literal["up", "degraded"]:
    """Проверка состояния REST-клиента Bybit.

    Аналогично WebSocket-клиенту, здесь оставлен задел под использование
    агрегированных метрик/счетчиков ошибок, когда они будут реализованы.
    Пока считаем состояние ``"up"``.
    """
    return "up"


@router.get("", response_model=HealthResponse)
@router.get("/", response_model=HealthResponse, include_in_schema=False)
async def health(redis: Redis | None = Depends(_get_redis)) -> HealthResponse:
    """Интегральный health-check приложения.

    Контракт выровнен с `docs/api.md` (§9.1):
    - `status: "ok"` когда все зависимости "up";
    - `status: "degraded"` при частичной деградации;
    - HTTP 503 при падении критичных зависимостей (БД или Redis).

    Поле `components` позволяет понять состояние каждой отдельной зависимости.
    """
    db_status = await _check_db()
    redis_status = await _check_redis(redis)
    bybit_ws_status = _check_bybit_ws()
    bybit_rest_status = _check_bybit_rest()

    components = HealthComponents(
        db=db_status,
        redis=redis_status,
        bybit_ws=bybit_ws_status,
        bybit_rest=bybit_rest_status,
    )

    critical_down = db_status == "down" or redis_status == "down"

    if critical_down:
        # В соответствии с документацией, при недоступности критичных
        # зависимостей возвращаем 503. Тело при этом сохраняет форму
        # HealthResponse для удобства клиентов.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "degraded",
                "components": components.model_dump(),
            },
        )

    overall_status: Literal["ok", "degraded"]
    if (
        bybit_ws_status == "degraded"
        or bybit_rest_status == "degraded"
    ):
        overall_status = "degraded"
    else:
        overall_status = "ok"

    return HealthResponse(status=overall_status, components=components)


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    """Liveness-проверка процесса приложения.

    Ничего не проверяет, кроме самого факта, कि процесс жив и способен
    отвечать на HTTP-запросы. Подходит для Kubernetes livenessProbe и
    простых ping-проверок со стороны внешних систем.
    """
    return LiveResponse(status="alive", ts=datetime.now(timezone.utc))


@router.get("/ready", response_model=ReadyResponse)
async def ready(redis: Redis | None = Depends(_get_redis)) -> ReadyResponse:
    """Readiness-проверка.

    В отличие от liveness, здесь проверяются критичные внешние зависимости:
    - Redis (через PING);
    - PostgreSQL (через `SELECT 1`).

    При любых проблемах эндпоинт возвращает HTTP 503, что даёт
    оркестратору (Kubernetes / docker-compose healthcheck) сигнал
    о неготовности инстанса принимать трафик.
    """
    db_ok = (await _check_db()) == "up"
    redis_ok = (await _check_redis(redis)) == "up"

    checks = {
        "redis": redis_ok,
        "db": db_ok,
    }

    all_ok = db_ok and redis_ok
    status_value: Literal["ready", "not_ready"] = "ready" if all_ok else "not_ready"

    if not all_ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": status_value,
                "checks": checks,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )

    return ReadyResponse(
        status=status_value,
        checks=checks,
        ts=datetime.now(timezone.utc),
    )
