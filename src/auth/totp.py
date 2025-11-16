from __future__ import annotations

import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)

try:  # внешняя зависимость, используется для совместимости с Google Authenticator
    import pyotp  # type: ignore[import]
except ImportError:  # pragma: no cover - в тестовой среде библиотека может отсутствовать
    pyotp = None  # type: ignore[assignment]

# Отображаемое имя сервиса в приложении-аутентификаторе
DEFAULT_TOTP_ISSUER = "AlgoGrid AVI-5"


def generate_secret() -> str:
    """
    Сгенерировать новый секрет для TOTP.

    Секрет совместим с Google Authenticator (base32-строка).
    Его следует шифровать и хранить в БД на стороне сервисного слоя.
    """
    if pyotp is None:
        raise RuntimeError(
            "pyotp is required for TOTP operations but is not installed"
        )

    # pyotp.random_base32 генерирует криптографически стойкую base32-строку
    return pyotp.random_base32()  # type: ignore[attr-defined]


def generate_uri(secret: str, email: str) -> str:
    """
    Сформировать otpauth:// URI для TOTP на основе секрета и e-mail пользователя.

    URI совместим с Google Authenticator и аналогичными клиентами, его можно
    кодировать в QR и показывать пользователю.

    :param secret: TOTP-секрет (base32).
    :param email: E-mail пользователя, который будет отображаться в клиенте.
    :raises TypeError: если secret или email не строки.
    :raises RuntimeError: если pyotp недоступен в окружении.
    """
    if not isinstance(secret, str) or not isinstance(email, str):
        raise TypeError("secret and email must be strings")

    if pyotp is None:
        raise RuntimeError(
            "pyotp is required for TOTP operations but is not installed"
        )

    # Библиотека pyotp сама сформирует корректный otpauth:// URI.
    totp = _get_totp(secret)
    # issuer_name отображается в аутентификаторе как название сервиса.
    uri = totp.provisioning_uri(
        name=email,
        issuer_name=DEFAULT_TOTP_ISSUER,
    )
    # На всякий случай проверим ожидаемый префикс.
    if not uri.startswith("otpauth://totp/"):
        # Это не критично для работы, но полезно залогировать.
        logger.warning("Generated TOTP URI has unexpected format", extra={"uri_prefix": uri.split("?", 1)[0]})
    return uri


def verify(code: str, secret: str) -> bool:
    """
    Проверить TOTP-код для заданного секрета.

    Возвращает:
      * True  — код корректен (в пределах допустимого временного окна).
      * False — код неверен, просрочен, секрет битый или формат кода некорректен.

    :param code: Одноразовый код, введённый пользователем.
    :param secret: TOTP-секрет (base32), соответствующий пользователю.
    :raises TypeError: если входные значения не строки.
    :raises RuntimeError: если pyotp недоступен в окружении.
    """
    if not isinstance(code, str) or not isinstance(secret, str):
        raise TypeError("code and secret must be strings")

    if pyotp is None:
        raise RuntimeError(
            "pyotp is required for TOTP operations but is not installed"
        )

    # Нормализуем ввод: убираем пробелы, возможные разделители.
    normalized_code = code.replace(" ", "").strip()

    # Простейшая валидация формата: 6-значный код.
    if not (normalized_code.isdigit() and 6 <= len(normalized_code) <= 8):
        # Не выбрасываем ошибок — просто считаем код неверным.
        return False

    try:
        totp = _get_totp(secret)
    except Exception:
        # Битый секрет, неверный формат base32 и т.п. — для внешнего мира это просто "код неверен".
        logger.warning("Failed to create TOTP instance from secret", exc_info=True)
        return False

    try:
        # valid_window=1 позволяет принимать соседние интервалы (±1 шаг)
        # для небольшой погрешности времени между сервером и клиентом.
        is_valid = bool(totp.verify(normalized_code, valid_window=1))
    except Exception:
        # Любая ошибка в процессе проверки не должна падать наружу.
        logger.warning("Error while verifying TOTP code", exc_info=True)
        return False

    return is_valid


# ---------- Внутренние вспомогательные функции ----------


def _get_totp(secret: str) -> "pyotp.TOTP":
    """
    Получить экземпляр TOTP для заданного секрета.

    Вынесено в отдельную функцию для единообразной обработки ошибок.
    """
    if pyotp is None:
        # Защита от несогласованного состояния; в публичных функциях мы уже проверяем pyotp.
        raise RuntimeError(
            "pyotp is required for TOTP operations but is not installed"
        )

    # type: ignore[name-defined] — pyotp подгружается динамически.
    return pyotp.TOTP(secret)  # type: ignore[no-any-return]


def _quote_label(label: str) -> str:
    """
    URL-экранирование метки для otpauth:// URI.

    Сейчас не используется напрямую, но оставлено как приватный helper
    на случай, если понадобится формировать URI вручную.
    """
    return quote(label, safe="")
