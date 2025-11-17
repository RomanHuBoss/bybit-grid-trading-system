# src/integration/bybit/error_handler.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Union

from src.core.logging_config import get_logger
from src.monitoring.metrics import Metrics


__all__ = ["BybitErrorCode", "ErrorAction", "handle_api_error", "raise_for_bybit_rest_error"]

logger = get_logger("integration.bybit.error_handler")
_metrics = Metrics()


class BybitErrorCode(str, Enum):
    """
    Нормализованные категории ошибок Bybit.

    Перечисление отражает *тип* ошибки, а не конкретный числовой ret_code.
    Сырые ret_code (int) маппятся в это перечисление внутри `_classify_error`.
    """

    SUCCESS = "success"
    UNKNOWN = "unknown"

    PARAM_ERROR = "param_error"
    AUTH_ERROR = "auth_error"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    ORDER_ERROR = "order_error"
    INSUFFICIENT_BALANCE = "insufficient_balance"


@dataclass(frozen=True)
class ErrorAction:
    """
    Описание того, что делать вызывающему коду при ошибке Bybit.

    Поля соответствуют спецификации:
        retry        — стоит ли повторить запрос;
        log_level    — уровень логирования (`debug`/`info`/`warning`/`error`);
        user_message — человекочитаемое сообщение для UI/клиента (может быть None);
        alert_ops    — нужно ли поднимать алерт для ops/SRE.

    Дополнительно:
        code           — нормализованный тип ошибки (`BybitErrorCode`);
        retry_delay_ms — рекомендуемая задержка перед ретраем (опционально).
    """

    code: BybitErrorCode
    retry: bool
    log_level: str
    user_message: Optional[str] = None
    alert_ops: bool = False
    retry_delay_ms: Optional[int] = None


# Базовая таблица действий по категориям ошибок.
_BASE_ACTIONS: Dict[BybitErrorCode, ErrorAction] = {
    BybitErrorCode.SUCCESS: ErrorAction(
        code=BybitErrorCode.SUCCESS,
        retry=False,
        log_level="debug",
        user_message=None,
        alert_ops=False,
    ),
    BybitErrorCode.PARAM_ERROR: ErrorAction(
        code=BybitErrorCode.PARAM_ERROR,
        retry=False,
        log_level="error",
        user_message="Запрос к Bybit отклонён из-за некорректных параметров.",
        alert_ops=True,  # логическая ошибка приложения — желательно обратить внимание.
    ),
    BybitErrorCode.AUTH_ERROR: ErrorAction(
        code=BybitErrorCode.AUTH_ERROR,
        retry=False,
        log_level="error",
        user_message="Ошибка аутентификации/авторизации на стороне Bybit.",
        alert_ops=True,
    ),
    BybitErrorCode.RATE_LIMIT: ErrorAction(
        code=BybitErrorCode.RATE_LIMIT,
        retry=True,
        log_level="warning",
        user_message="Превышен лимит запросов к Bybit. Попробуем повторить позже.",
        alert_ops=False,
        retry_delay_ms=1000,
    ),
    BybitErrorCode.SERVER_ERROR: ErrorAction(
        code=BybitErrorCode.SERVER_ERROR,
        retry=True,
        log_level="error",
        user_message="Временные проблемы на стороне Bybit. Попробуем повторить запрос.",
        alert_ops=True,
        retry_delay_ms=1000,
    ),
    BybitErrorCode.ORDER_ERROR: ErrorAction(
        code=BybitErrorCode.ORDER_ERROR,
        retry=False,
        log_level="warning",
        user_message="Ошибка при обработке ордера на Bybit.",
        alert_ops=False,
    ),
    BybitErrorCode.INSUFFICIENT_BALANCE: ErrorAction(
        code=BybitErrorCode.INSUFFICIENT_BALANCE,
        retry=False,
        log_level="warning",
        user_message="Недостаточно баланса на счёте Bybit для выполнения операции.",
        alert_ops=False,
    ),
    BybitErrorCode.UNKNOWN: ErrorAction(
        code=BybitErrorCode.UNKNOWN,
        retry=False,
        log_level="error",
        user_message="Неизвестная ошибка Bybit.",
        alert_ops=True,
    ),
}


def handle_api_error(
    error_code: Union[int, str, None],
    error_msg: str,
    context: Optional[Mapping[str, Any]] = None,
) -> ErrorAction:
    """
    Централизованная обработка ошибок Bybit API.

    :param error_code: Значение `ret_code`/`retCode` из ответа Bybit
                       (может быть строкой, числом или None).
    :param error_msg:  Текст ошибки (`ret_msg`/`retMsg`) или локальное описание.
    :param context:    Дополнительный контекст для логов/метрик, например:
                       {
                           "http_status": int,
                           "endpoint": "GET /v5/...",
                           "method": "GET" | "POST",
                           "payload": {...},
                           "attempt": int,
                           "symbol": "BTCUSDT",
                           ...
                       }

    Обязанности:
        * нормализовать и классифицировать ошибку Bybit;
        * обновить связанные метрики (например, счётчик rate-limit);
        * залогировать событие с единым форматом;
        * вернуть `ErrorAction`, описывающий дальнейшие действия
          для `OrderManager` / `BybitRESTClient`.
    """
    ctx: Mapping[str, Any] = context or {}

    http_status = _extract_http_status(ctx)
    bybit_code = _classify_error(error_code, http_status, error_msg)

    action = _BASE_ACTIONS.get(bybit_code, _BASE_ACTIONS[BybitErrorCode.UNKNOWN])

    # Метрики: отдельный счётчик для попаданий в rate limit.
    if bybit_code is BybitErrorCode.RATE_LIMIT:
        _record_rate_limit_hit(ctx)

    # Логирование с единым форматом.
    log_fields: Dict[str, Any] = {
        "error_code": _safe_int(error_code),
        "error_msg": error_msg,
        "http_status": http_status,
        "bybit_error": bybit_code.value,
        "retry": action.retry,
        "retry_delay_ms": action.retry_delay_ms,
        "alert_ops": action.alert_ops,
    }
    log_fields.update(_sanitize_context(ctx))

    log_fn = getattr(logger, action.log_level, logger.error)
    log_fn("Bybit API error handled", **log_fields)

    return action


# --------------------------------------------------------------------------- #
# Внутренние помощники
# --------------------------------------------------------------------------- #


def _extract_http_status(context: Mapping[str, Any]) -> Optional[int]:
    raw = context.get("http_status")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Union[int, str, None]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _classify_error(
    error_code: Union[int, str, None],
    http_status: Optional[int],
    error_msg: str,
) -> BybitErrorCode:
    """
    Маппинг числового ret_code / HTTP-статуса в категорию `BybitErrorCode`.

    Правила основаны на публичной документации Bybit v5 и могут быть
    расширены по мере необходимости.
    """
    code = _safe_int(error_code)

    # HTTP-уровень имеет приоритет, если ret_code неизвестен/отсутствует.
    if http_status == 429:
        return BybitErrorCode.RATE_LIMIT
    if http_status is not None and 500 <= http_status <= 599:
        return BybitErrorCode.SERVER_ERROR
    if http_status in (401, 403):
        return BybitErrorCode.AUTH_ERROR

    if code is None:
        return BybitErrorCode.UNKNOWN

    if code == 0:
        return BybitErrorCode.SUCCESS

    # Типичные коды Bybit (по документации v5).
    # 10001 — Parameter error
    if code == 10001:
        return BybitErrorCode.PARAM_ERROR

    # 10003/10004/10005/10020 — auth / permission issues
    if code in (10003, 10004, 10005, 10020):
        return BybitErrorCode.AUTH_ERROR

    # 10006 — Too many visits (rate limit)
    if code == 10006:
        return BybitErrorCode.RATE_LIMIT

    # 10002/10007 — timeout / server busy
    if code in (10002, 10007):
        return BybitErrorCode.SERVER_ERROR

    # 11xxxx — order-related errors (order not found, etc.)
    if 110000 <= code < 120000:
        return BybitErrorCode.ORDER_ERROR

    # 130026/130027 — insufficient balance (точные коды могут отличаться)
    if code in (130026, 130027):
        return BybitErrorCode.INSUFFICIENT_BALANCE

    # Эвристика по тексту
    lowered = error_msg.lower()
    if "rate limit" in lowered or "too many" in lowered:
        return BybitErrorCode.RATE_LIMIT
    if "insufficient balance" in lowered:
        return BybitErrorCode.INSUFFICIENT_BALANCE
    if "auth" in lowered or "signature" in lowered or "api key" in lowered:
        return BybitErrorCode.AUTH_ERROR

    return BybitErrorCode.UNKNOWN


def _record_rate_limit_hit(context: Mapping[str, Any]) -> None:
    """
    Обновить метрику rate_limit_hits_total для конкретного endpoint'а.
    """
    endpoint = (
        str(context.get("endpoint"))
        or str(context.get("path") or "")
        or "unknown"
    ).strip() or "unknown"

    try:
        _metrics.increment_rate_limit_hits(endpoint)
    except Exception as exc:  # noqa: BLE001
        # Метрики не должны ломать основной поток — просто логируем на debug.
        logger.debug(
            "Failed to update rate_limit_hits_total metric",
            error=str(exc),
            endpoint=endpoint,
        )


def _sanitize_context(context: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Отфильтровать и привести к простым типам полезный контекст для логов.

    Чтобы не тащить в JSON огромные payload'ы, берём только «узловые» поля.
    """
    allowed_keys = {
        "endpoint",
        "path",
        "method",
        "attempt",
        "symbol",
        "category",
    }
    result: Dict[str, Any] = {}
    for key in allowed_keys:
        if key not in context:
            continue
        value = context.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = repr(value)
    return result


def raise_for_bybit_rest_error(
    payload: Mapping[str, Any],
    *,
    http_status: Optional[int] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Обёртка для BybitRESTClient: по JSON-ответу решает,
    надо ли бросать исключение.

    * Если ответ успешный (retCode/ret_code == 0) — просто возвращает None.
    * Если ошибка — вызывает handle_api_error(...) и бросает исключение.
    """
    # Нормализуем retCode / retMsg
    if not isinstance(payload, Mapping):
        error_code: Optional[Union[int, str]] = None
        error_msg = "Non-mapping payload from Bybit REST"
    else:
        error_code = payload.get("retCode", payload.get("ret_code"))
        error_msg = str(
            payload.get("retMsg")
            or payload.get("ret_msg")
            or payload.get("msg")
            or ""
        )

    ctx: Dict[str, Any] = dict(context or {})

    # Чтобы _extract_http_status увидел http_status
    if http_status is not None:
        ctx.setdefault("http_status", http_status)

    # У тебя в контексте из REST-клиента передаётся url; для метрик
    # _record_rate_limit_hit смотрит на endpoint/path, добавим алиас.
    if "endpoint" not in ctx and "url" in ctx:
        ctx["endpoint"] = str(ctx["url"])

    action = handle_api_error(error_code, error_msg, ctx)

    # Успех — ничего не делаем, просто возвращаемся.
    if action.code is BybitErrorCode.SUCCESS:
        return

    # На этом уровне можно сделать более красивый доменный exception,
    # но без знания src.core.exceptions безопаснее не гадать.
    # Делаем информативный RuntimeError, который уже можно перехватывать выше.
    raise RuntimeError(
        f"Bybit REST error: code={error_code!r}, "
        f"msg={error_msg or action.user_message}, "
        f"type={action.code.value}, "
        f"retry={action.retry}"
    )