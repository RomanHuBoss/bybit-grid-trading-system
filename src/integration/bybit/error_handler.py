from __future__ import annotations

from typing import Any, Mapping, Optional

from src.core.exceptions import ExternalAPIError, ExecutionError


__all__ = ["raise_for_bybit_rest_error"]


def _is_success_response(
    data: Mapping[str, Any],
    http_status: Optional[int],
) -> bool:
    """
    Проверить, является ли ответ Bybit "успешным".

    Условие успеха:
      * HTTP-статус в диапазоне 2xx (если он известен);
      * retCode == 0 или retCode отсутствует.

    В остальных случаях считаем, что это ошибка.
    """
    if http_status is not None and not (200 <= http_status < 300):
        return False

    ret_code = data.get("retCode")
    if ret_code is None:
        # Некоторые вспомогательные эндпоинты могут не иметь retCode — считаем ok.
        return True

    try:
        ret_code_int = int(ret_code)
    except (TypeError, ValueError):
        # Невалидный retCode => точно не успех.
        return False

    return ret_code_int == 0


def _extract_error_info(data: Mapping[str, Any]) -> tuple[Optional[int], Optional[str]]:
    """
    Достать retCode и retMsg из ответа Bybit (если есть).
    """
    ret_code_raw = data.get("retCode")
    ret_msg = data.get("retMsg")

    ret_code: Optional[int]
    try:
        ret_code = int(ret_code_raw) if ret_code_raw is not None else None
    except (TypeError, ValueError):
        ret_code = None

    if isinstance(ret_msg, str):
        msg = ret_msg
    else:
        msg = None

    return ret_code, msg


def raise_for_bybit_rest_error(
    data: Mapping[str, Any],
    http_status: Optional[int] = None,
    *,
    context: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Проверить ответ Bybit и, если нужно, выбросить доменное исключение.

    :param data: Распарсенный JSON-ответ от Bybit.
    :param http_status: HTTP-код ответа (если доступен).
    :param context: Дополнительный контекст (url, method, attempts и т.п.).

    Поведение:
      * если ответ считается успешным (`_is_success_response`) — функция
        просто возвращает None;
      * если retCode / HTTP-код сигнализируют об ошибке — выбрасывает
        ExternalAPIError или ExecutionError, в зависимости от кода.
    """
    if _is_success_response(data, http_status):
        return

    ret_code, ret_msg = _extract_error_info(data)

    # Сформируем базовое сообщение и детали для логов.
    message_parts = ["Bybit REST API error"]
    if http_status is not None:
        message_parts.append(f"HTTP {http_status}")
    if ret_code is not None:
        message_parts.append(f"retCode={ret_code}")
    if ret_msg:
        message_parts.append(f"retMsg='{ret_msg}'")

    message = " | ".join(message_parts)

    # Стараемся не тащить в details гигантское тело ответа.
    details: dict[str, Any] = {
        "http_status": http_status,
        "retCode": ret_code,
        "retMsg": ret_msg,
    }

    # Немного полезных кусков тела, если они есть.
    if "result" in data:
        details["has_result"] = True
    if "time" in data:
        details["time"] = data["time"]
    if context:
        details["context"] = dict(context)

    # Некоторые retCode явно относятся к "бизнесовым" ошибкам торговли,
    # для них удобнее бросить ExecutionError, чтобы выше можно было
    # отличать их от общих проблем внешнего API.
    execution_error_codes = {
        10001,  # parameter error
        10002,  # request expired
        130021,  # order not found / already closed (пример)
        130024,  # insufficient balance
    }

    if ret_code in execution_error_codes:
        raise ExecutionError(message, details=details)

    # Все остальные случаи рассматриваем как ошибки внешнего API.
    raise ExternalAPIError(message, details=details)
