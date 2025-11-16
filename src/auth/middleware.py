# src/auth/middleware.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import HTTPException, status
from starlette.requests import Request

from src.auth.jwt_manager import (
    JWTAuthManager,
    ExpiredTokenError,
    InvalidTokenError,
    RevokedTokenError,
    JWTManagerError,
)
from src.core.logging_config import add_context_vars, get_logger

logger = get_logger("auth.middleware")

_BEARER_PREFIX = "bearer"
_JWT_MANAGER_SINGLETON: Optional[JWTAuthManager] = None


@dataclass
class CurrentUser:
    """
    Текущий аутентифицированный пользователь.

    Атрибуты:
        id: UUID пользователя (sub в JWT).
        role: Роль пользователя (viewer / trader / admin).
        is_active: Флаг активности пользователя.
    """

    id: UUID
    role: str
    is_active: bool = True


def _get_jwt_manager() -> JWTAuthManager:
    """
    Ленивое создание singleton-инстанса JWTAuthManager.

    JWTAuthManager сам подтянет настройки из конфигурации/ENV.
    """
    global _JWT_MANAGER_SINGLETON  # noqa: PLW0603
    if _JWT_MANAGER_SINGLETON is None:
        _JWT_MANAGER_SINGLETON = JWTAuthManager()
        logger.debug("Initialized JWTAuthManager singleton")
    return _JWT_MANAGER_SINGLETON


def _extract_bearer_token(request: Request) -> str:
    """
    Достаёт Bearer-токен из заголовка Authorization.

    :raises HTTPException(401): если заголовок отсутствует или в неверном формате.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        logger.debug("Missing Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    # Ожидаем формат: "Bearer <token>"
    parts = auth_header.split()
    if len(parts) != 2:
        logger.debug("Invalid Authorization header format", extra={"header": auth_header})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format",
        )

    scheme, token = parts[0], parts[1]
    if scheme.lower() != _BEARER_PREFIX or not token:
        logger.debug(
            "Unsupported auth scheme or empty token",
            extra={"scheme": scheme},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme",
        )

    return token


def _build_current_user_from_payload(payload: Dict[str, Any]) -> CurrentUser:
    """
    Собирает CurrentUser из payload JWT.

    Ожидаемые клаймы:
        - sub: UUID пользователя
        - role: строковая роль (viewer/trader/admin)
        - is_active: bool (опционально, по умолчанию True)
    """
    raw_sub = payload.get("sub")
    try:
        user_id = UUID(str(raw_sub))
    except (TypeError, ValueError) as exc:
        logger.warning(
            "JWT payload does not contain a valid 'sub' UUID",
            extra={"sub": raw_sub},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        ) from exc

    raw_role = payload.get("role")
    if not isinstance(raw_role, str) or not raw_role.strip():
        logger.warning(
            "JWT payload does not contain a valid 'role'",
            extra={"role": raw_role},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    role = raw_role.strip().lower()

    # is_active может не быть в payload — трактуем как активного пользователя.
    # Спецификация JWT-пейлоада в базовом документе не перечисляет is_active,
    # но middleware обязан уметь его уважать, если он присутствует.
    is_active_raw = payload.get("is_active", True)
    is_active = bool(is_active_raw)

    if not is_active:
        logger.info(
            "Access denied for inactive user",
            extra={"user_id": str(user_id), "role": role},
        )
        # По API-спецификации заблокированный пользователь → 403.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is inactive",
        )

    return CurrentUser(id=user_id, role=role, is_active=is_active)


async def get_current_user(request: Request) -> CurrentUser:
    """
    FastAPI-зависимость для получения текущего пользователя по JWT.

    Выполняет:
    - Извлечение Bearer-токена из заголовка Authorization.
    - Валидацию токена через JWTAuthManager как access-токена.
    - Построение CurrentUser из payload.
    - Проброс CurrentUser в request.state.current_user и request.state.user.
    - Запись user_id / user_role в контекст логов через add_context_vars.

    :raises HTTPException(401): при отсутствии/некорректности токена.
    :raises HTTPException(403): если пользователь заблокирован (is_active == False).
    :return: CurrentUser, пригодный для использования в Depends и RBAC.
    """
    token = _extract_bearer_token(request)
    jwt_manager = _get_jwt_manager()

    try:
        payload = jwt_manager.validate_token(token, expected_type="access")
    except ExpiredTokenError as exc:
        logger.info("JWT token expired", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": 'Bearer error="token_expired"'},
        ) from exc
    except RevokedTokenError as exc:
        logger.info("JWT token revoked", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": 'Bearer error="token_revoked"'},
        ) from exc
    except InvalidTokenError as exc:
        logger.warning("Invalid JWT token", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    except JWTManagerError as exc:
        # Непредвиденные ошибки JWT-менеджера считаем проблемой аутентификации.
        logger.exception("Unexpected JWT manager error", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
        ) from exc

    current_user = _build_current_user_from_payload(payload)

    # Пробрасываем в request.state.
    # RBAC (`require_role`) ожидает request.state.current_user.
    request.state.current_user = current_user  # type: ignore[attr-defined]

    # Для совместимости с некоторыми частями спецификации, где фигурирует state.user.
    if getattr(request.state, "user", None) is None:  # type: ignore[attr-defined]
        request.state.user = current_user  # type: ignore[attr-defined]

    # Обогащаем лог-контекст.
    try:
        add_context_vars(user_id=str(current_user.id), user_role=current_user.role)
    except Exception as exc:  # noqa: BLE001
        # Логирование контекста не должно ломать аутентификацию.
        logger.exception(
            "Failed to add logging context vars for user",
            extra={"error": str(exc)},
        )

    return current_user
