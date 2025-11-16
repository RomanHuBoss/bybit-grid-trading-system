from __future__ import annotations

import logging
import os
import signal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import yaml
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

    _instance: Optional["ConfigLoader"] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> "ConfigLoader":  # singleton
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: str | Path = "config/settings.yaml") -> None:
        # защита от повторной инициализации при множественных вызовах ConfigLoader()
        if getattr(self, "_initialized", False):
            return

        self._initialized = True
        self._config_path = Path(config_path)
        self._config: Optional[AppConfig] = None
        self._needs_reload: bool = True

        self._register_sighup_handler()

    # ---------- Публичный API ----------

    def load_yaml_config(self, path: Path) -> Dict[str, Any]:
        """
        Прочитать и распарсить YAML-конфиг, применив подстановку переменных окружения.

        :param path: Путь к YAML файлу.
        :raises FileNotFoundError: если файл не существует.
        :raises UnicodeDecodeError: если файл не декодируется как UTF-8.
        :raises yaml.YAMLError: при ошибках синтаксиса YAML.
        :return: Словарь конфигурации (уже после env-подстановок и env-override).
        """
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

        # 1) подставляем ${ENV_VAR} в строковых значениях
        data = self._expand_env_placeholders(data)
        # 2) применяем env-override вида TRADING__MAX_STAKE и т.п.
        data = self._apply_env_overrides(data)

        return data

    def get_config(self) -> AppConfig:
        """
        Получить актуальную конфигурацию приложения.

        - При первом вызове читает YAML, валидирует через AppConfig и кеширует результат.
        - При получении SIGHUP конфигурация будет перечитана при следующем вызове.
        - При ошибке валидации пробрасывает pydantic.ValidationError.
        """
        if self._config is None or self._needs_reload:
            logger.info("Loading application config", extra={"config_path": str(self._config_path)})
            raw_config = self.load_yaml_config(self._config_path)

            try:
                config = AppConfig(**raw_config)
            except ValidationError:
                # Не маскируем сами сообщения pydantic — там нет секретов из env,
                # а вот сериализацию готового объекта делаем аккуратно.
                logger.exception("Configuration validation failed")
                raise

            self._config = config
            self._needs_reload = False

            # Маскируем чувствительные поля перед логированием
            try:
                config_dict = config.dict()
            except AttributeError:
                # На случай pydantic v2
                config_dict = config.model_dump()

            masked = self._mask_secrets(config_dict)
            logger.info("Configuration loaded successfully", extra={"config": masked})

        return self._config

    # ---------- Внутренняя логика ----------

    def _register_sighup_handler(self) -> None:
        """
        Зарегистрировать обработчик SIGHUP для hot-reload конфигурации.

        В средах без SIGHUP (например, Windows) или вне main-thread молча пропускается.
        """
        if not hasattr(signal, "SIGHUP"):
            return

        try:
            signal.signal(signal.SIGHUP, self._handle_sighup)
        except (ValueError, OSError):
            # Не удалось повесить обработчик (например, не main thread) — просто логируем отладочно.
            logger.debug("SIGHUP handler was not installed", exc_info=True)

    def _handle_sighup(self, signum: int, frame: Any) -> None:  # noqa: ARG002
        """
        Обработчик сигнала SIGHUP.

        Не выполняет тяжёлых операций: только помечает конфиг как устаревший.
        """
        logger.info("Received SIGHUP, configuration will be reloaded on next access")
        self._needs_reload = True

    def _expand_env_placeholders(self, data: Any) -> Any:
        """
        Рекурсивно подставляет переменные окружения в строках:

        - `"${BYBIT_API_KEY}"` -> значение из os.environ["BYBIT_API_KEY"] (если задано).
        - `$VAR` и `${VAR}` обрабатываются через os.path.expandvars.

        Если переменная не задана, placeholder оставляется как есть.
        """
        if isinstance(data, dict):
            return {k: self._expand_env_placeholders(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._expand_env_placeholders(v) for v in data]
        if isinstance(data, str):
            return os.path.expandvars(data)
        return data

    def _apply_env_overrides(self, base: Mapping[str, Any]) -> Dict[str, Any]:
        """
        Применить override из окружения к YAML-конфигу.

        Ожидаемый формат переменных:
            TRADING__MAX_STAKE=200
            RISK__MAX_CONCURRENT=10

        Где часть до "__" — имя секции (trading, risk, bybit, db, ui),
        а после — имя поля внутри этой секции. Регистр игнорируется.
        """
        # Копируем, чтобы не мутировать исходный словарь
        result: Dict[str, Any] = {k: self._clone_value(v) for k, v in base.items()}

        for env_key, env_value in os.environ.items():
            key = env_key.lower()
            if "__" not in key:
                continue

            section_name, field_name = key.split("__", 1)
            section = result.get(section_name)
            if section is None:
                # неизвестная секция — пропускаем
                continue
            if not isinstance(section, dict):
                # если секция не словарь, переопределять небезопасно
                logger.debug(
                    "Cannot apply env override for non-mapping section",
                    extra={"section": section_name, "env_key": env_key},
                )
                continue

            # Pydantic сам приведёт типы из строк, поэтому не заморачиваемся кастом.
            section[field_name] = env_value

        return result

    @staticmethod
    def _clone_value(value: Any) -> Any:
        """
        Простое «глубокое» копирование для dict/list,
        чтобы не тащить сюда copy.deepcopy без необходимости.
        """
        if isinstance(value, dict):
            return {k: ConfigLoader._clone_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [ConfigLoader._clone_value(v) for v in value]
        return value

    def _mask_secrets(self, data: Any) -> Any:
        """
        Рекурсивно маскирует чувствительные значения в конфиге.

        Любой ключ, содержащий в имени подстроку:
            - "secret"
            - "password"
            - "token"
            - "api_key"
            - "dsn"
        будет заменён на "***".
        """
        sensitive_keywords = ("secret", "password", "token", "api_key", "apikey", "dsn", "key")

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
