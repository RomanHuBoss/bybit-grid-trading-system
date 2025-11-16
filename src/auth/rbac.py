from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, Optional, Set, Union

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

# Определения ролей (должны совпадать с CHECK-constraint в БД и общей моделью системы)
ROLE_VIEWER = "viewer"
ROLE_TRADER = "trader"
ROLE_ADMIN = "admin"

# Все допустимые роли и их «ранги» для иерархии прав:
# viewer < trader < admin
_ALLOWED_ROLES: Set[str] = {ROLE_VIEWER, ROLE_TRADER, ROLE_ADMIN}
_ROLE_RANK: Dict[str, int] = {
    ROLE_VIEWER: 0,
    ROLE_TRADER: 1,
    ROLE_ADMIN: 2,
}

# Краткое текстовое описание ролей — используется для документации
_ROLE_DESCRIPTIONS: Dict[str, str] = {
    ROLE_VIEWER: (
        "Только чтение: сигналы, открытые/закрытые позиции, агрегированные метрики. "
        "Нет доступа к изменению конфигурации, торговле и управлению API-ключами."
    ),
    ROLE_TRADER: (
        "Все права viewer плюс возможность включать/выключать торговлю, "
        "управлять позициями (например, ручное закрытие), запускать калибровку."
    ),
    ROLE_ADMIN: (
        "Все права trader плюс управление пользователями и их ролями, "
        "управление API-ключами, изменение risk-конфигурации, доступ к административным операциям."
    ),
}


RoleLike = Union[str, Any]
DependencyCallable = Callable[[Request], Any]


def require_role(*roles: RoleLike) -> DependencyCallable:
    """
    Создать FastAPI-зависимость для проверки роли пользователя.

    Пример использования в роуте:

        @router.post("/users", dependencies=[Depends(require_role("admin"))])
        async def create_user(...):
            ...

        @router.post("/positions/{id}/close")
        async def close_position(
            id: UUID,
            _=Depends(require_role("trader")),  # trader и admin
        ):
            ...

    Логика иерархии:

    * viewer  (ранг 0) — минимальные права;
    * trader  (ранг 1) — все права viewer + торговля;
    * admin   (ранг 2) — все права trader + админские операции.

    Если эндпоинт требует, например, роль "trader", то доступ получают пользователи
    с ролью "trader" ИЛИ "admin" (т.е. ранги >= требуемого).
    Если требуется "admin", то только "admin".

    :param roles: Набор требуемых ролей (строки, достаточно одной:
                  require_role("trader") или require_role("admin")).
    :raises ValueError: если указана неизвестная роль или список ролей пуст.
    :return: Асинхронная функция-зависимость для FastAPI.
    """
    normalized_roles = _normalize_required_roles(roles)
    min_required_rank = min(_ROLE_RANK[role] for role in normalized_roles)

    async def dependency(request: Request) -> None:
        """
        Зависимость FastAPI, которая:

        - извлекает current_user из request.state.current_user;
        - проверяет наличие и валидность роли;
        - сравнивает роль пользователя с требуемым уровнем доступа;
        - поднимает 401/403 в случае нарушения.
        """
        # current_user заполняется auth-middleware (см. src/auth/middleware.py).
        current_user: Any = getattr(request.state, "current_user", None)

        if current_user is None:
            # Теоретически до require_role должен стоять аутентификатор,
            # но подстрахуемся — без аутентификации доступ запрещён.
            logger.debug("RBAC denied: missing current_user on request.state")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )

        raw_role: Any = getattr(current_user, "role", None)
        if raw_role is None:
            logger.warning(
                "RBAC denied: current_user has no 'role' attribute",
                extra={"user_repr": repr(current_user)},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User role is not set",
            )

        try:
            user_role = _normalize_role(raw_role)
        except ValueError:
            logger.warning(
                "RBAC denied: unknown user role",
                extra={"role": raw_role},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Unknown user role",
            )

        user_rank = _ROLE_RANK[user_role]
        if user_rank < min_required_rank:
            logger.info(
                "RBAC denied: insufficient role",
                extra={
                    "user_role": user_role,
                    "required_min_role": _role_by_rank(min_required_rank),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Operation not allowed for this role",
            )

        # Если добрались сюда — доступ разрешён; сама зависимость ничего не возвращает.
        return None

    return dependency


# ===== Вспомогательные функции (в т.ч. для документации) =====


def get_role_descriptions() -> Dict[str, str]:
    """
    Получить текстовые описания ролей.

    Функция полезна для генерации раздела про RBAC в документации (docs/api.md),
    а также для UI-подсказок.

    :return: Словарь {имя_роли: описание}.
    """
    # Копируем, чтобы внешний код не мутировал внутренний словарь.
    return dict(_ROLE_DESCRIPTIONS)


def _normalize_required_roles(roles: Iterable[RoleLike]) -> Set[str]:
    """
    Привести список требуемых ролей к внутреннему набору строк.

    :param roles: Имена ролей (строки или значения с атрибутом `.value`).
    :raises ValueError: если список пустой или contains неизвестные роли.
    """
    normalized: Set[str] = set()

    for role in roles:
        if role is None:
            continue

        # Поддерживаем как строки, так и enum-подобные объекты с `.value`.
        if hasattr(role, "value"):
            value = getattr(role, "value")
        else:
            value = role

        if not isinstance(value, str):
            raise ValueError(f"Role must be a string or enum-like with .value, got {type(role)!r}")

        value = value.strip().lower()
        if not value:
            continue

        if value not in _ALLOWED_ROLES:
            raise ValueError(f"Unknown role: {value!r}")

        normalized.add(value)

    if not normalized:
        raise ValueError("At least one role must be specified for require_role")

    return normalized


def _normalize_role(role: Any) -> str:
    """
    Привести произвольное значение роли к строке из _ALLOWED_ROLES
    или выбросить ValueError, если это невозможно.
    """
    if hasattr(role, "value"):
        role = getattr(role, "value")

    if not isinstance(role, str):
        raise ValueError(f"Role must be a string or enum-like with .value, got {type(role)!r}")

    value = role.strip().lower()
    if value not in _ALLOWED_ROLES:
        raise ValueError(f"Unknown role: {value!r}")

    return value


def _role_by_rank(rank: int) -> Optional[str]:
    """
    Вспомогательная функция для логов: найти имя роли по её рангу.
    """
    for name, r in _ROLE_RANK.items():
        if r == rank:
            return name
    return None
