from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from urllib.parse import quote

logger = logging.getLogger(__name__)

try:  # внешняя зависимость, используется для совместимости с Google Authenticator
    import pyotp  # type: ignore[import]
except ImportError:  # pragma: no cover - в тестовой среде библиотека может отсутствовать
    pyotp = None  # type: ignore[assignment]

# Отображаемое имя сервиса в приложении-аутентификаторе
DEFAULT_TOTP_ISSUER = "AlgoGrid AVI-5"

# Длина секрета по умолчанию (битовая энтропия соответствует рекомендациям pyotp/Google)
DEFAULT_SECRET_LENGTH = 32

# Количество 30-секундных окон, которое мы считаем валидным при проверке кода.
# 1 окно = текущий интервал +/- 1 интервал (в сумме 3), что даёт допуск на небольшие расхождения часов.
DEFAULT_VALID_WINDOW = 1


@dataclass(slots=True)
class TOTPConfig:
    """
    Конфигурация TOTP-провайдера.

    На случай, если в будущем потребуется переопределять параметры (длина секрета,
    окно валидации и т.п.) из конфигурации приложения.
    """

    issuer: str = DEFAULT_TOTP_ISSUER
    secret_length: int = DEFAULT_SECRET_LENGTH
    valid_window: int = DEFAULT_VALID_WINDOW


def _ensure_pyotp_available() -> None:
    """
    Убедиться, что библиотека pyotp доступна.

    Если pyotp не установлена, поднимаем понятную ошибку конфигурации.
    """
    if pyotp is None:  # type: ignore[truthy-function]
        raise RuntimeError(
            "pyotp library is required for TOTP operations, but is not installed. "
            "Install it with 'pip install pyotp' or отключите TOTP-аутентификацию "
            "в настройках сервиса."
        )


def generate_totp_secret(config: Optional[TOTPConfig] = None) -> str:
    """
    Сгенерировать секрет для TOTP (base32-строка).

    Секрет сохраняется в БД пользователя и используется как основа
    для всех последующих проверок кода аутентификатора.

    :param config: Необязательная конфигурация TOTP. Если не передана —
                   используется TOTPConfig по умолчанию.
    :return: base32-строка секрета.
    """
    _ensure_pyotp_available()
    cfg = config or TOTPConfig()

    length = max(16, int(cfg.secret_length))  # минимальная разумная длина
    secret = pyotp.random_base32(length=length)  # type: ignore[no-any-return]

    logger.debug("Generated new TOTP secret", extra={"secret_length": length})
    return secret


def build_provisioning_uri(
    *,
    secret: str,
    account_label: str,
    config: Optional[TOTPConfig] = None,
) -> str:
    """
    Сформировать otpauth:// URI для передачи в мобильное приложение (через QR-код).

    Именно этот URI кодируется в QR, который показывает фронтенд при включении 2FA.

    :param secret: base32-секрет пользователя.
    :param account_label: Метка аккаунта, которая будет отображаться в приложении
                          (обычно email или "<login>@<env>").
    :param config: Конфигурация TOTP (issuer, длина секрета и т.п.).
    :return: Строка otpauth://totp/...
    """
    _ensure_pyotp_available()
    cfg = config or TOTPConfig()

    normalized_label = account_label.strip()
    if not normalized_label:
        raise ValueError("account_label must be a non-empty string")

    # В соответствии со стандартной схемой: otpauth://totp/<issuer>:<label>?secret=...&issuer=...
    # pyotp сам корректно сформирует URI с нужными параметрами.
    totp = pyotp.TOTP(secret)  # type: ignore[no-any-return]
    uri = totp.provisioning_uri(
        name=normalized_label,
        issuer_name=cfg.issuer,
    )

    logger.debug(
        "Built TOTP provisioning URI",
        extra={"label": normalized_label, "issuer": cfg.issuer},
    )
    return uri


def verify_totp_code(
    *,
    secret: str,
    code: str,
    config: Optional[TOTPConfig] = None,
) -> bool:
    """
    Проверить TOTP-код, введённый пользователем.

    Валидация соответствует рекомендациям из документации:
    - используем допуск по времени `valid_window` (по умолчанию 1 интервал);
    - код нормализуем (удаляем пробелы, приводим к строке, проверяем формат).

    :param secret: base32-секрет пользователя.
    :param code: Код, введённый пользователем (обычно 6 цифр).
    :param config: Конфигурация TOTP.
    :return: True, если код валиден в текущем окне; иначе False.
    """
    _ensure_pyotp_available()
    cfg = config or TOTPConfig()

    # Нормализация: убираем пробелы и невидимые символы.
    normalized_code = str(code).strip().replace(" ", "")

    if not normalized_code.isdigit():
        logger.info(
            "TOTP code verification failed: non-numeric code",
            extra={"code_length": len(normalized_code)},
        )
        return False

    totp = pyotp.TOTP(secret)  # type: ignore[no-any-return]
    # valid_window — это количество дополнительных интервалов вокруг текущего,
    # которые считаются допустимыми (для компенсации рассинхрона часов).
    try:
        # bool(...) на случай, если pyotp вернёт что-то не строгое.
        is_valid = bool(totp.verify(normalized_code, valid_window=cfg.valid_window))
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while verifying TOTP code")
        return False

    if not is_valid:
        logger.info(
            "TOTP code verification failed: invalid code",
            extra={"code_length": len(normalized_code)},
        )
    else:
        logger.debug("TOTP code verified successfully")

    return is_valid


def _get_totp_instance(secret: str):
    """
    Внутренний helper: получить экземпляр pyotp.TOTP по секрету.

    Вынесен отдельно, чтобы при необходимости можно было мокать его в тестах.
    """
    _ensure_pyotp_available()
    return pyotp.TOTP(secret)  # type: ignore[no-any-return]


def _quote_label(label: str) -> str:
    """
    URL-экранирование метки для otpauth:// URI.

    Сейчас не используется напрямую, но оставлено как приватный helper
    на случай, если понадобится формировать URI вручную.
    """
    return quote(label, safe="")
