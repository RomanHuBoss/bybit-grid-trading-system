from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, FrozenSet, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

__all__ = ["AuthConfig", "APIKeyAuthMiddleware"]


@dataclass(frozen=True)
class AuthConfig:
    """
    Конфигурация простой API-key аутентификации на уровне middleware.

    Этот слой не является основной системой аутентификации (JWT + RBAC),
    описанной в документации (auth-эндпоинты и роли пользователей).
    Он предназначен для дополнительной защиты отдельных внутренних /
    служебных эндпоинтов, когда это требуется инфраструктурой
    (например, internal-tools, cron, сервисные вызовы).

    enabled:
        Включена ли проверка вообще. Если False — middleware прозрачен.
    header_name:
        Имя HTTP-заголовка, в котором передаётся ключ (по умолчанию X-API-Key).
    valid_keys:
        Набор допустимых ключей. Если пустой — фактически аутентификация отключена.
    """

    enabled: bool = True
    header_name: str = "X-API-Key"
    valid_keys: FrozenSet[str] = frozenset()


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """
    Базовая API-key аутентификация для HTTP-API.

    Логика:
    - если AuthConfig.enabled == False или valid_keys пуст — ничего не проверяем;
    - иначе читаем ключ из указанного заголовка (по умолчанию X-API-Key);
    - если ключ отсутствует или не входит в valid_keys — 401 Unauthorized;
    - при успешной проверке сохраняем key в request.state.api_key и пропускаем запрос.

    Это middleware не знает про пользователей/роли и не лезет в базу;
    оно обеспечивает только "тонкий" слой защиты для внутренних/админских эндпоинтов
    и не заменяет JWT-аутентификацию и RBAC.
    """

    def __init__(self, app, config: Optional[AuthConfig] = None) -> None:  # type: ignore[override]
        super().__init__(app)
        self._config = config or AuthConfig()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """
        Выполнить проверку API-ключа и передать управление следующему обработчику.

        Проверка выполняется только если:
        - включён флаг enabled,
        - и в конфигурации есть хотя бы один допустимый ключ.
        """
        cfg = self._config

        # Если аутентификация отключена или нет ни одного валидного ключа —
        # ведём себя прозрачно.
        if not cfg.enabled or not cfg.valid_keys:
            return await call_next(request)

        api_key = self._get_header(request, cfg.header_name)

        if api_key is None or api_key not in cfg.valid_keys:
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
            )

        # Кладём ключ в request.state для последующего использования в хэндлерах.
        request.state.api_key = api_key  # type: ignore[attr-defined]
        return await call_next(request)

    # --------------------------------------------------------------------- #
    # Внутренние утилиты
    # --------------------------------------------------------------------- #

    @staticmethod
    def _get_header(request: Request, name: str) -> Optional[str]:
        """
        Мягко прочитать заголовок, не заваливаясь на странных значениях.
        """
        value = request.headers.get(name)
        if value is None:
            return None
        value = value.strip()
        return value or None
