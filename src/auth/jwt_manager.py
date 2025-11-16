from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from uuid import UUID, uuid4

try:
    import jwt  # type: ignore[import]
    from jwt import ExpiredSignatureError, InvalidTokenError  # type: ignore[import]
except ImportError:  # pragma: no cover - библиотека может отсутствовать в окружении выполнения
    jwt = None  # type: ignore[assignment]
    ExpiredSignatureError = InvalidTokenError = None  # type: ignore[assignment]

from src.core.config_loader import ConfigLoader

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class JWTSettings:
    """
    Настройки генерации и проверки JWT-токенов.

    Значения по умолчанию соответствуют требованиям раздела 2.4:
    - access: 15 минут
    - refresh: 7 дней
    """

    secret: str
    algorithm: str = "HS256"
    access_ttl_seconds: int = 15 * 60
    refresh_ttl_seconds: int = 7 * 24 * 60 * 60


class JWTManagerError(Exception):
    """Базовый класс ошибок JWT-менеджера."""


class ExpiredTokenError(JWTManagerError):
    """Токен истёк."""


class InvalidTokenError(JWTManagerError):
    """Токен некорректен или скомпрометирован."""


class RevokedTokenError(JWTManagerError):
    """Токен отозван (попал в blacklist)."""


# Кеш настроек и singleton-экземпляр менеджера
_JWT_SETTINGS: Optional[JWTSettings] = None
_JWT_MANAGER: Optional["JWTAuthManager"] = None


def _require_jwt_lib() -> None:
    """
    Убедиться, что библиотека PyJWT доступна.

    Отдельная функция, чтобы давать осмысленную ошибку,
    а не ImportError где-нибудь внутри.
    """
    if jwt is None:
        raise RuntimeError(
            "PyJWT is required for JWT operations but is not installed "
            "(install 'pyjwt' or configure JWT backend accordingly)."
        )


def _load_jwt_settings() -> JWTSettings:
    """
    Загрузить настройки JWT из settings.yaml / окружения и закешировать их.

    Ожидаем в settings.yaml структуру вида:

        auth:
          jwt:
            secret: "..."                    # секрет подписи
            algorithm: "HS256"              # или RS256/ES256 и т.п.
            access_ttl_minutes: 15
            refresh_ttl_days: 7

    Все поля опциональны, кроме secret. При отсутствии конфига пытаемся
    взять секрет из переменной окружения JWT_SECRET. При отсутствии секрета
    выбрасывается RuntimeError.
    """
    global _JWT_SETTINGS

    if _JWT_SETTINGS is not None:
        return _JWT_SETTINGS

    secret: Optional[str] = None
    algorithm = "HS256"
    access_ttl_seconds = 15 * 60
    refresh_ttl_seconds = 7 * 24 * 60 * 60

    try:
        loader = ConfigLoader()
        raw_config = loader.load_yaml_config(Path("config/settings.yaml"))
        auth_cfg = raw_config.get("auth") if isinstance(raw_config, dict) else None

        if isinstance(auth_cfg, dict):
            jwt_cfg = auth_cfg.get("jwt")
            if isinstance(jwt_cfg, dict):
                # Секрет и алгоритм
                conf_secret = jwt_cfg.get("secret")
                if isinstance(conf_secret, str) and conf_secret.strip():
                    secret = conf_secret.strip()

                conf_algo = jwt_cfg.get("algorithm")
                if isinstance(conf_algo, str) and conf_algo.strip():
                    algorithm = conf_algo.strip()

                # TTL access
                if "access_ttl_seconds" in jwt_cfg:
                    try:
                        val = int(jwt_cfg["access_ttl_seconds"])
                        if val > 0:
                            access_ttl_seconds = val
                    except (TypeError, ValueError):
                        logger.warning(
                            "Invalid auth.jwt.access_ttl_seconds, falling back to default",
                            extra={"value": jwt_cfg.get("access_ttl_seconds")},
                        )
                elif "access_ttl_minutes" in jwt_cfg:
                    try:
                        minutes = int(jwt_cfg["access_ttl_minutes"])
                        if minutes > 0:
                            access_ttl_seconds = minutes * 60
                    except (TypeError, ValueError):
                        logger.warning(
                            "Invalid auth.jwt.access_ttl_minutes, falling back to default",
                            extra={"value": jwt_cfg.get("access_ttl_minutes")},
                        )

                # TTL refresh
                if "refresh_ttl_seconds" in jwt_cfg:
                    try:
                        val = int(jwt_cfg["refresh_ttl_seconds"])
                        if val > 0:
                            refresh_ttl_seconds = val
                    except (TypeError, ValueError):
                        logger.warning(
                            "Invalid auth.jwt.refresh_ttl_seconds, falling back to default",
                            extra={"value": jwt_cfg.get("refresh_ttl_seconds")},
                        )
                elif "refresh_ttl_days" in jwt_cfg:
                    try:
                        days = int(jwt_cfg["refresh_ttl_days"])
                        if days > 0:
                            refresh_ttl_seconds = days * 24 * 60 * 60
                    except (TypeError, ValueError):
                        logger.warning(
                            "Invalid auth.jwt.refresh_ttl_days, falling back to default",
                            extra={"value": jwt_cfg.get("refresh_ttl_days")},
                        )

    except FileNotFoundError:
        logger.warning("settings.yaml not found while loading JWT settings, using env/defaults")
    except Exception:
        # Любые странности с конфигом не должны ломать запуск сервиса.
        logger.exception("Failed to load JWT settings, using env/defaults")

    # Приоритет окружения поверх YAML.
    env_secret = os.getenv("JWT_SECRET")
    if env_secret:
        secret = env_secret

    if not secret:
        raise RuntimeError(
            "JWT secret is not configured. "
            "Set auth.jwt.secret in settings.yaml or define JWT_SECRET environment variable."
        )

    _JWT_SETTINGS = JWTSettings(
        secret=secret,
        algorithm=algorithm,
        access_ttl_seconds=access_ttl_seconds,
        refresh_ttl_seconds=refresh_ttl_seconds,
    )
    return _JWT_SETTINGS


class JWTAuthManager:
    """
    Центральный менеджер JWT-токенов.

    Отвечает за:
    - генерацию пар access/refresh токенов;
    - декодирование и валидацию токена;
    - проверку типа токена (access / refresh);
    - базовую интеграцию с blacklist по jti (через переданный callback).
    """

    def __init__(
        self,
        settings: Optional[JWTSettings] = None,
        *,
        is_jti_blacklisted: Optional[Callable[[str], bool]] = None,
    ) -> None:
        _require_jwt_lib()

        self._settings = settings or _load_jwt_settings()
        self._is_jti_blacklisted = is_jti_blacklisted
        self._logger = logger

    # ---------- Публичный API ----------

    def issue_token_pair(
        self,
        user_id: UUID,
        role: str,
        extra_claims: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """
        Сгенерировать пару (access, refresh) токенов для пользователя.

        :param user_id: Идентификатор пользователя (UUID).
        :param role: Роль пользователя.
        :param extra_claims: Дополнительные кастомные клаймы.
        :return: (access_token, refresh_token).
        """
        jti_access = str(uuid4())
        jti_refresh = str(uuid4())

        access = self._encode(
            subject=user_id,
            role=role,
            token_type="access",
            jti=jti_access,
            extra_claims=extra_claims,
        )
        refresh = self._encode(
            subject=user_id,
            role=role,
            token_type="refresh",
            jti=jti_refresh,
            extra_claims=extra_claims,
        )

        self._logger.info(
            "JWT token pair issued",
            extra={
                "user_id": str(user_id),
                "role": role,
                "access_jti": jti_access,
                "refresh_jti": jti_refresh,
            },
        )

        return access, refresh

    def refresh_from_token(
        self,
        refresh_token: str,
    ) -> Tuple[str, str]:
        """
        Обновить пару токенов по действующему refresh-токену.

        :param refresh_token: Строка refresh-токена.
        :return: Новая пара (access, refresh).
        :raises ExpiredTokenError: если токен истёк.
        :raises RevokedTokenError: если токен в blacklist.
        :raises InvalidTokenError: при любом другом нарушении формата/подписи.
        """
        payload = self.validate_token(refresh_token, expected_type="refresh")
        try:
            user_id = UUID(str(payload.get("sub")))
        except (TypeError, ValueError) as exc:
            raise InvalidTokenError("Refresh token payload does not contain a valid 'sub' UUID") from exc

        role = str(payload.get("role", "") or "")
        if not role:
            raise InvalidTokenError("Refresh token payload does not contain 'role' claim")

        access, new_refresh = self.issue_token_pair(user_id=user_id, role=role)

        self._logger.info(
            "JWT token pair refreshed",
            extra={
                "user_id": str(user_id),
                "role": role,
                "old_refresh_jti": payload.get("jti"),
            },
        )

        return access, new_refresh

    def decode_token(
        self,
        token: str,
        *,
        verify_exp: bool = True,
    ) -> Dict[str, Any]:
        """
        Декодировать JWT и получить payload без доп. семантических проверок.

        :param token: Строка JWT.
        :param verify_exp: Проверять ли `exp` (по умолчанию да).
        :return: Раскодированный payload.
        :raises ExpiredTokenError: если токен истёк.
        :raises InvalidTokenError: при любом нарушении формата/подписи.
        """
        _require_jwt_lib()

        try:
            payload = jwt.decode(
                token,
                key=self._settings.secret,
                algorithms=[self._settings.algorithm],
                options={"verify_exp": verify_exp},
            )
        except ExpiredSignatureError as exc:  # type: ignore[misc]
            self._logger.info("JWT token expired", extra={"reason": "expired"})
            raise ExpiredTokenError("JWT token has expired") from exc
        except InvalidTokenError as exc:  # type: ignore[misc]
            self._logger.info("JWT token invalid", extra={"reason": "invalid"})
            raise InvalidTokenError("JWT token is invalid") from exc
        except Exception as exc:  # noqa: BLE001
            # Любые другие ошибки интерпретируем как "некорректный токен".
            self._logger.exception("Unexpected error while decoding JWT")
            raise InvalidTokenError("Failed to decode JWT token") from exc

        if not isinstance(payload, dict):
            raise InvalidTokenError("JWT payload must be a JSON object")

        return payload

    def validate_token(
        self,
        token: str,
        *,
        expected_type: Optional[str] = None,
        verify_exp: bool = True,
    ) -> Dict[str, Any]:
        """
        Полная валидация JWT: подпись, exp, type и blacklist (если настроен).

        :param token: JWT строка (access или refresh).
        :param expected_type: Ожидаемый тип токена: "access" или "refresh".
        :param verify_exp: Проверять ли истечение срока действия.
        :return: payload, если токен валиден.
        :raises ExpiredTokenError: если токен истёк.
        :raises RevokedTokenError: если токен отозван.
        :raises InvalidTokenError: при нарушении подписи, структуры или типа.
        """
        payload = self.decode_token(token, verify_exp=verify_exp)

        token_type = str(payload.get("type", "") or "")
        if expected_type and token_type != expected_type:
            raise InvalidTokenError(
                f"Unexpected JWT type: expected '{expected_type}', got '{token_type or '<missing>'}'"
            )

        jti_val = payload.get("jti")
        if self._is_jti_blacklisted and jti_val is not None:
            jti_str = str(jti_val)
            try:
                if self._is_jti_blacklisted(jti_str):
                    raise RevokedTokenError("JWT token has been revoked (blacklisted)")
            except RevokedTokenError:
                raise
            except Exception:  # noqa: BLE001
                # Не даём падать наружу из-за проблем с blacklist; считаем токен недействительным.
                self._logger.exception("Error while checking JWT blacklist")
                raise InvalidTokenError("Failed to verify token revocation status")

        return payload

    # ---------- Внутренние помощники ----------

    def _encode(
        self,
        *,
        subject: UUID,
        role: str,
        token_type: str,
        jti: Optional[str] = None,
        extra_claims: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Сформировать и подписать JWT с заданным типом и TTL.
        """
        now = datetime.now(timezone.utc)
        if token_type == "access":
            ttl = self._settings.access_ttl_seconds
        elif token_type == "refresh":
            ttl = self._settings.refresh_ttl_seconds
        else:
            raise ValueError(f"Unsupported JWT token_type: {token_type!r}")

        base_claims: Dict[str, Any] = {
            "sub": str(subject),
            "role": role,
            "type": token_type,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl)).timestamp()),
            "jti": jti or str(uuid4()),
        }

        if extra_claims:
            # Пользовательские клаймы могут переопределять базовые только осознанно.
            for key, value in extra_claims.items():
                if key in base_claims:
                    self._logger.warning(
                        "Overriding standard JWT claim with extra_claims",
                        extra={"claim": key},
                    )
                base_claims[key] = value

        _require_jwt_lib()
        token = jwt.encode(
            base_claims,
            key=self._settings.secret,
            algorithm=self._settings.algorithm,
        )
        if isinstance(token, bytes):
            token = token.decode("utf-8")
        return token


def get_jwt_manager() -> JWTAuthManager:
    """
    Получить singleton-экземпляр JWTAuthManager.

    Используется в middleware/endpoint'ах как точка входа в подсистему JWT.
    """
    global _JWT_MANAGER

    if _JWT_MANAGER is None:
        settings = _load_jwt_settings()
        _JWT_MANAGER = JWTAuthManager(settings=settings)

    return _JWT_MANAGER
