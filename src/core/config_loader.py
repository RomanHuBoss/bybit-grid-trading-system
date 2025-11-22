from __future__ import annotations

import logging
import os
import signal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, ClassVar

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from src.core.models import AppConfig

logger = logging.getLogger(__name__)


class ConfigLoader:
    """
    Singleton-загрузчик конфигурации приложения.

    - Читает config/settings.yaml (по умолчанию) и подставляет значения из переменных окружения.
    - Возвращает типизированный объект AppConfig.
    - Обновляет конфигурацию по сигналу SIGHUP.
    - Маскирует секреты при логировании.
    """

    _instance: ClassVar[Optional["ConfigLoader"]] = None

    _initialized: bool
    _config_path: Path
    _config: Optional[AppConfig]
    _needs_reload: bool

    def __new__(cls, *args: Any, **kwargs: Any) -> "ConfigLoader":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: str | Path = "config/settings.yaml") -> None:
        existing_initialized = getattr(self, "_initialized", False)
        existing_path = getattr(self, "_config_path", None) if existing_initialized else None

        if existing_initialized:
            new_path = Path(config_path)
            if (
                existing_path is not None
                and new_path != existing_path
                and getattr(self, "_config", None) is None
            ):
                self._config_path = new_path
                self._needs_reload = True
                logger.info(
                    "ConfigLoader config_path overridden before first load",
                    extra={"old_path": str(existing_path), "new_path": str(new_path)},
                )
            return

        self._initialized = True
        self._config_path = Path(config_path)
        self._config = None
        self._needs_reload = True

        self._register_sighup_handler()

    # ---------- Публичный API ----------

    def load_yaml_config(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        try:
            raw_text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.exception("Failed to decode config file as UTF-8", extra={"config_path": str(path)})
            raise

        try:
            data = yaml.safe_load(raw_text) or {}
        except yaml.YAMLError:
            logger.exception("Failed to parse YAML config", extra={"config_path": str(path)})
            raise

        if not isinstance(data, dict):
            raise ValueError("Top-level YAML config must be a mapping")

        data = self._expand_env_placeholders(data)
        data = self._apply_env_overrides(data)

        return data

    def get_config(self) -> AppConfig:
        if self._config is None or self._needs_reload:
            logger.info("Loading application config", extra={"config_path": str(self._config_path)})

            raw_config = self.load_yaml_config(self._config_path)

            try:
                config = AppConfig(**raw_config)
            except ValidationError:
                logger.exception("Configuration validation failed")
                raise

            self._config = config
            self._needs_reload = False

            # ---------------------------------------------------------
            #  FIX: работаем строго на Pydantic v2 → используем model_dump
            # ---------------------------------------------------------
            config_dict = config.model_dump()

            masked = self._mask_secrets(config_dict)
            logger.info("Configuration loaded successfully", extra={"config": masked})

        return self._config

    # ---------- Внутренняя логика ----------

    def _register_sighup_handler(self) -> None:
        if not hasattr(signal, "SIGHUP"):
            return
        try:
            signal.signal(signal.SIGHUP, self._handle_sighup)
        except (ValueError, OSError):
            logger.debug("SIGHUP handler was not installed", exc_info=True)

    def _handle_sighup(self, _signum: int, _frame: Any) -> None:
        logger.info(
            "Received SIGHUP, configuration will be reloaded on next access",
            extra={"signum": _signum},
        )
        self._needs_reload = True

    def _expand_env_placeholders(self, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: self._expand_env_placeholders(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._expand_env_placeholders(v) for v in data]
        if isinstance(data, str):
            return os.path.expandvars(data)
        return data

    def _apply_env_overrides(self, base: Mapping[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {k: self._clone_value(v) for k, v in base.items()}

        for env_key, env_value in os.environ.items():
            key = env_key.lower()
            if "__" not in key:
                continue

            section_name, field_name = key.split("__", 1)

            section = result.get(section_name)
            if section is None:
                continue
            if not isinstance(section, dict):
                logger.debug(
                    "Cannot apply env override for non-mapping section",
                    extra={"section": section_name, "env_key": env_key},
                )
                continue

            section[field_name] = env_value

        return result

    @staticmethod
    def _clone_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: ConfigLoader._clone_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [ConfigLoader._clone_value(v) for v in value]
        return value

    def _mask_secrets(self, data: Any) -> Any:
        sensitive_keywords = ["secret", "password", "token", "api_key", "dsn"]

        if isinstance(data, dict):
            masked: Dict[str, Any] = {}
            for key, value in data.items():
                key_lower = str(key).lower()
                if any(word in key_lower for word in sensitive_keywords):
                    masked[key] = "***"
                else:
                    masked[key] = self._mask_secrets(value)
            return masked

        if isinstance(data, list):
            return [self._mask_secrets(item) for item in data]

        return data
