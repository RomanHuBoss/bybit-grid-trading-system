from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


__all__ = [
    "AppError",
    "ConfigError",
    "NetworkError",
    "DatabaseError",
    "ExecutionError",
    "ExternalAPIError",
    "WSConnectionError",
    "RateLimitExceededError",
    "InvalidCandleError",
]


@dataclass
class AppError(Exception):
    """
    Базовое исключение для всех доменных ошибок AVI-5.

    Атрибуты:
        message: Человеко-читаемое описание ошибки (одна строка).
        details: Дополнительный структурированный контекст для логов /
                 сериализации (id сущности, url, payload и т.п.).
    """

    message: str
    details: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        # Exception.__init__ ожидает строку в args[0]
        super().__init__(self.message)

    def __str__(self) -> str:  # pragma: no cover - тривиальная логика
        if not self.details:
            return self.message
        return f"{self.message} | details={self.details!r}"


class ConfigError(AppError):
    """
    Ошибка конфигурации приложения.

    Примеры:
        - отсутствует обязательная переменная окружения;
        - некорректный формат значения конфига;
        - несовместимые параметры запуска.
    """

    pass


class NetworkError(AppError):
    """
    Ошибка сетевого уровня (HTTP, WebSocket, DNS и т.п.).

    Используется в тех местах, где есть надежда на успешный retry
    (нестабильная сеть, временные проблемы у провайдера).
    """

    pass


class DatabaseError(AppError):
    """
    Ошибка при работе с базой данных.

    Обычно оборачивает конкретные исключения драйвера (asyncpg, psycopg и т.п.),
    не раскрывая детали наружу.
    """

    pass


class ExecutionError(AppError):
    """
    Ошибка исполнения торговой логики / ордеров.

    Примеры:
        - не удалось открыть/закрыть позицию;
        - невозможный сигнал (некорректная цена/объём);
        - рассинхронизация состояния с биржей.
    """

    pass


class ExternalAPIError(AppError):
    """
    Обёртка над бизнес-ошибками внешних API (Bybit REST/WS, Slack, webhooks).

    Внутри обычно хранится исходный код ошибки / тело ответа.
    """

    pass


class WSConnectionError(NetworkError):
    """
    Базовая ошибка WebSocket-подсистемы.

    Любые проблемы с установлением / поддержанием WS-соединения
    (как с биржей, так и с другими сервисами) должны наследоваться от неё.
    """

    pass


class RateLimitExceededError(AppError):
    """
    Базовая ошибка превышения лимитов (REST, WS, webhooks).

    На неё уже навешиваются более конкретные:
        - RateLimitTimeoutError
        - WSRateLimitError
    в модуле rate_limiter.
    """

    pass

class InvalidCandleError(ValueError):
    """
    Исключение, выбрасываемое при некорректных OHLCV-данных свечи.

    Используется в `ConfirmedCandle` и связанных с ним местах, когда входная
    свеча не проходит базовые sanity-check-и и не может быть использована
    движком сигналов (SignalEngine).

    Типичные причины:
    - нарушена базовая геометрия OHLC (high < max(open, close) или
      low > min(open, close));
    - цена закрытия выходит за диапазон [low, high];
    - отрицательный объём;
    - свеча помечена как `confirmed=True`, но её `close_time` находится в будущем.

    Атрибут `details` (dict) может содержать дополнительные данные о причинах
    отклонения для последующей логгирования и отладки.
    """

    def __init__(self, message: str, *, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.details: dict = details or {}
