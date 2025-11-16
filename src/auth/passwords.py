from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from argon2 import PasswordHasher, exceptions as argon2_exceptions
except ImportError:  # pragma: no cover - библиотека может быть не установлена в окружении выполнения
    PasswordHasher = None  # type: ignore[assignment]
    argon2_exceptions = None  # type: ignore[assignment]

try:
    import bcrypt
except ImportError:  # pragma: no cover
    bcrypt = None  # type: ignore[assignment]

from src.core.config_loader import ConfigLoader

logger = logging.getLogger(__name__)

# Поддерживаемые алгоритмы
PASSWORD_ALGO_ARGON2ID = "argon2id"
PASSWORD_ALGO_BCRYPT = "bcrypt"


@dataclass(slots=True)
class _PasswordHashSettings:
    """
    Внутренние настройки алгоритма хэширования паролей.

    Пока содержим только выбор алгоритма, но при необходимости сюда
    можно добавить параметры cost/memory/parallelism и т.п.
    """

    algorithm: str = PASSWORD_ALGO_ARGON2ID


_PASSWORD_SETTINGS: Optional[_PasswordHashSettings] = None
_ARGON2_HASHER: Optional["PasswordHasher"] = None


def hash_password(plain: str) -> str:
    """
    Создать криптографический хэш пароля.

    Алгоритм и его параметры берутся из конфигурации (settings.yaml),
    при отсутствии явных настроек по умолчанию используется Argon2id.

    :param plain: исходный пароль в виде строки.
    :return: строка с хэшом (формат зависит от выбранного алгоритма).
    :raises TypeError: если пароль не строка.
    :raises RuntimeError: если выбранный алгоритм недоступен в окружении.
    """
    if not isinstance(plain, str):
        raise TypeError("Password must be a string")

    settings = _get_password_settings()
    algorithm = settings.algorithm

    if algorithm == PASSWORD_ALGO_BCRYPT:
        return _hash_bcrypt(plain)

    # По умолчанию — Argon2id
    return _hash_argon2(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """
    Проверить соответствие пароля заранее сохранённому хэшу.

    Возвращает True при успешной проверке и False при несоответствии
    или некорректном формате хэша.

    :param plain: исходный пароль.
    :param hashed: сохранённый ранее хэш.
    :return: True, если пароль подходит к хэшу; иначе False.
    :raises TypeError: если входные значения не строки.
    :raises RuntimeError: если требуемый алгоритм недоступен в окружении.
    """
    if not isinstance(plain, str) or not isinstance(hashed, str):
        raise TypeError("Password and hash must be strings")

    # Сначала пробуем определить алгоритм по самому хэшу.
    detected_algo = _detect_algorithm_from_hash(hashed)
    if detected_algo is None:
        # Если алгоритм не удалось определить по префиксу, используем текущие настройки.
        detected_algo = _get_password_settings().algorithm

    if detected_algo == PASSWORD_ALGO_BCRYPT:
        return _verify_bcrypt(plain, hashed)

    # По умолчанию считаем, что это Argon2id.
    return _verify_argon2(plain, hashed)


# ---------- Внутренние функции конфигурации ----------


def _get_password_settings() -> _PasswordHashSettings:
    """
    Загрузить (и закешировать) настройки алгоритма хэширования паролей.

    Ожидаем в settings.yaml структуру вида:

        auth:
          passwords:
            algorithm: "argon2id"  # или "bcrypt"

    Дополнительно поддерживаем более плоские варианты ключей:
    auth.password_algorithm, auth.hash_algorithm.

    При любом непонимании конфига откатываемся к Argon2id.
    """
    global _PASSWORD_SETTINGS

    if _PASSWORD_SETTINGS is not None:
        return _PASSWORD_SETTINGS

    # Значения по умолчанию
    algorithm = PASSWORD_ALGO_ARGON2ID

    try:
        loader = ConfigLoader()
        # Берём тот же путь по умолчанию, что и в ConfigLoader.
        raw_config = loader.load_yaml_config(Path("config/settings.yaml"))

        auth_cfg = raw_config.get("auth") if isinstance(raw_config, dict) else None
        if isinstance(auth_cfg, dict):
            # Ждём либо вложенную секцию "passwords", либо плоские ключи.
            pw_cfg = auth_cfg.get("passwords") if isinstance(auth_cfg.get("passwords"), dict) else auth_cfg

            algo_value = None
            if isinstance(pw_cfg, dict):
                algo_value = (
                    pw_cfg.get("algorithm")
                    or pw_cfg.get("password_algorithm")
                    or pw_cfg.get("hash_algorithm")
                )

            if isinstance(algo_value, str):
                algo_value_normalized = algo_value.strip().lower()
                if algo_value_normalized in {PASSWORD_ALGO_ARGON2ID, PASSWORD_ALGO_BCRYPT}:
                    algorithm = algo_value_normalized
                else:
                    logger.warning(
                        "Unsupported password hashing algorithm in config, falling back to Argon2id",
                        extra={"value": algo_value},
                    )
    except FileNotFoundError:
        # Конфиг не найден — работаем с настройками по умолчанию.
        logger.warning("settings.yaml not found, using default password hashing settings")
    except Exception:
        # Не должны падать из-за незначительных проблем с конфигом.
        logger.exception("Failed to load password hashing settings, using defaults")

    _PASSWORD_SETTINGS = _PasswordHashSettings(algorithm=algorithm)
    return _PASSWORD_SETTINGS


def _get_argon2_hasher() -> "PasswordHasher":
    """
    Ленивая инициализация глобального PasswordHasher для Argon2id.
    """
    global _ARGON2_HASHER

    if PasswordHasher is None:
        raise RuntimeError(
            "argon2-cffi is not installed but required for password hashing "
            "when algorithm=argon2id"
        )

    if _ARGON2_HASHER is None:
        _ARGON2_HASHER = PasswordHasher()  # type: ignore[call-arg]

    return _ARGON2_HASHER  # type: ignore[return-value]


# ---------- Внутренние функции для Argon2id ----------


def _hash_argon2(plain: str) -> str:
    """
    Хэширование пароля с помощью Argon2id.
    """
    hasher = _get_argon2_hasher()
    return hasher.hash(plain)


def _verify_argon2(plain: str, hashed: str) -> bool:
    """
    Проверка пароля против хэша Argon2id.
    """
    hasher = _get_argon2_hasher()

    if argon2_exceptions is None:
        # Теоретически не должны сюда попасть, т.к. _get_argon2_hasher уже проверяет наличие библиотеки,
        # но оставим защиту на случай странных сценариев.
        raise RuntimeError("argon2-cffi exceptions module is not available")

    try:
        # verify возвращает bool и выбрасывает исключения при ошибках/несоответствии.
        return bool(hasher.verify(hashed, plain))
    except (  # type: ignore[misc]
        argon2_exceptions.VerificationError,
        argon2_exceptions.InvalidHash,
    ):
        # Несоответствие пароля или битый хэш.
        return False


# ---------- Внутренние функции для bcrypt ----------


def _hash_bcrypt(plain: str) -> str:
    """
    Хэширование пароля с помощью bcrypt.
    """
    if bcrypt is None:
        raise RuntimeError(
            "bcrypt is not installed but required for password hashing "
            "when algorithm=bcrypt"
        )

    hashed_bytes = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())
    return hashed_bytes.decode("utf-8")


def _verify_bcrypt(plain: str, hashed: str) -> bool:
    """
    Проверка пароля против хэша bcrypt.
    """
    if bcrypt is None:
        raise RuntimeError(
            "bcrypt is not installed but required for password verification "
            "when algorithm=bcrypt"
        )

    try:
        return bool(
            bcrypt.checkpw(
                plain.encode("utf-8"),
                hashed.encode("utf-8"),
            )
        )
    except ValueError:
        # Неверный формат хэша.
        return False


# ---------- Детекция алгоритма по виду хэша ----------


def _detect_algorithm_from_hash(hashed: str) -> Optional[str]:
    """
    Попробовать определить алгоритм по префиксу хэша.

    Argon2id: строки вида "$argon2id$..." или "$argon2$..."
    bcrypt: строки "$2a$...", "$2b$...", "$2y$..."

    Если по виду строки определить алгоритм нельзя, возвращает None.
    """
    if hashed.startswith("$argon2id$") or hashed.startswith("$argon2$"):
        return PASSWORD_ALGO_ARGON2ID
    if hashed.startswith("$2a$") or hashed.startswith("$2b$") or hashed.startswith("$2y$"):
        return PASSWORD_ALGO_BCRYPT
    return None
