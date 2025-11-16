from __future__ import annotations

import logging
from logging import StreamHandler
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict

import structlog
from contextvars import ContextVar

LOGGER_NAME_ROOT = "algo_grid"

# Хранилище ContextVar для контекста (request_id, user_id, и т.п.)
_CONTEXT_VARS: Dict[str, ContextVar[Any]] = {}


def setup_logging(log_level: str = "INFO", log_file: str = "logs/app.jsonl") -> None:
    """
    Инициализировать логирование приложения.

    - Настраивает stdlib logging с ротацией файла.
    - Конфигурирует structlog для JSONL-формата и поддержки contextvars.
    - Ожидаемый формат записи:
      {"timestamp": "...Z", "level": "info", "event": "signal_generated", "request_id": "...", ...}

    :param log_level: Строковый уровень логирования ("DEBUG", "INFO", "WARNING", ...).
    :param log_file: Путь к JSONL-файлу логов.
    """
    level = _parse_log_level(log_level)

    # -------- stdlib logging --------
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # чтобы повторный вызов setup_logging не плодил хендлеры
    root_logger.handlers.clear()

    # Консоль
    console_handler = StreamHandler()
    console_handler.setLevel(level)
    root_logger.addHandler(console_handler)

    # Файл с ротацией
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    root_logger.addHandler(file_handler)

    # -------- structlog --------
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # подтягиваем ContextVar'ы (request_id, user_id и др.)
            timestamper,
            structlog.stdlib.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def add_context_vars(**kwargs: Any) -> None:
    """
    Добавить/обновить контекстные переменные для текущего запроса/таска.

    Примеры:
        add_context_vars(request_id="req-123", user_id="u-42")

    Все переданные пары ключ/значение будут доступны в логах как поля JSON.
    Разрешены значения простых типов (str, int, float, bool, None).

    :raises TypeError: если значение имеет неподдерживаемый тип.
    """
    for key, value in kwargs.items():
        if not _is_supported_value_type(value):
            raise TypeError(
                f"Unsupported context value type for '{key}': {type(value).__name__}. "
                "Allowed: str, int, float, bool, None."
            )

        ctx_var = _CONTEXT_VARS.get(key)
        if ctx_var is None:
            ctx_var = ContextVar(key, default=None)
            _CONTEXT_VARS[key] = ctx_var

        ctx_var.set(value)


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Получить именованный structlog-логгер.

    :param name: Имя логгера (обычно путь модуля: 'core.config_loader', 'api.routes.signals').
    :return: BoundLogger, готовый к использованию.
    :raises ValueError: если имя пустое.
    """
    if not name or not name.strip():
        raise ValueError("Logger name must be a non-empty string")

    # Единый префикс для всех логгеров приложения
    full_name = f"{LOGGER_NAME_ROOT}.{name}"
    return structlog.get_logger(full_name)


# ---------- Внутренние вспомогательные функции ----------


def _parse_log_level(level_name: str) -> int:
    """
    Преобразовать строковый уровень логирования в числовой (logging.*).

    Неподдержанные значения трактуются как INFO.
    """
    normalized = (level_name or "").strip().upper()
    mapping = {
        "CRITICAL": logging.CRITICAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
        "NOTSET": logging.NOTSET,
    }
    return mapping.get(normalized, logging.INFO)


def _is_supported_value_type(value: Any) -> bool:
    """
    Проверка, что значение контекста имеет «безопасный» простой тип,
    который потом корректно сериализуется в JSON.
    """
    return isinstance(value, (str, int, float, bool)) or value is None
