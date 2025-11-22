from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TradingConfigSchema(BaseModel):
    """
    Секция `trading` в settings.yaml.

    Описывает общие торговые параметры стратегии (не алгоритмика AVI-5),
    используется для ограничения максимального риска на сделку и включения
    research-режима.
    """

    model_config = ConfigDict(extra="forbid")

    max_stake: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Максимальный риск на сделку в USD (1R верхняя граница).",
    )
    research_mode: bool = Field(
        default=False,
        description=(
            "Режим исследовательских логов/метрик; дополнительные события и "
            "метрики пишутся только при включённом флаге."
        ),
    )


class RiskConfigSchema(BaseModel):
    """
    Секция `risk` в settings.yaml.

    Параметры риск-менеджмента на уровне аккаунта:
    - общее число позиций,
    - суммарный риск в R,
    - per-base лимиты.
    """

    model_config = ConfigDict(extra="forbid")

    max_concurrent: int = Field(
        ...,
        ge=1,
        description="Максимальное количество одновременно открытых позиций.",
    )
    max_total_risk_r: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Максимальный суммарный риск во всех позициях в R.",
    )
    max_positions_per_symbol: int = Field(
        2,
        ge=1,
        description="Максимум открытых позиций на один базовый актив.",
    )
    anti_churn_cooldown_minutes: int = Field(
        15,
        ge=0,
        description=(
            "Минимальный интервал (в минутах) между закрытием и новым "
            "открытием позиции по тому же символу."
        ),
    )

    @field_validator("max_total_risk_r")
    def _validate_max_total_risk_r(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("risk.max_total_risk_r must be positive")
        return v


class BybitConfigSchema(BaseModel):
    """
    Секция `bybit` в settings.yaml.

    Не хранит сами секреты (они идут через ENV/Vault), а задаёт базовые URL'ы
    и режимы работы.
    """

    model_config = ConfigDict(extra="forbid")

    # В YAML эти поля обычно не задаются — секреты берутся из окружения,
    # но оставляем их опциональными, чтобы удобно было использовать схему
    # и для тестовых конфигов.
    api_key: Optional[str] = Field(
        default=None,
        description="API-key Bybit (обычно берётся из ENV/Vault).",
    )
    api_secret: Optional[str] = Field(
        default=None,
        description="API-secret Bybit (обычно берётся из ENV/Vault).",
    )

    rest_base_url: str = Field(
        "https://api.bybit.com",
        description="Базовый URL REST API Bybit.",
    )
    ws_public_url: str = Field(
        "wss://stream.bybit.com/v5/public/linear",
        description="Публичный WebSocket эндпоинт.",
    )
    ws_private_url: str = Field(
        "wss://stream.bybit.com/v5/private",
        description="Приватный WebSocket эндпоинт.",
    )


class DBConfigSchema(BaseModel):
    """
    Секция `db` в settings.yaml.

    Настройки подключения к TimescaleDB/PostgreSQL. В полноценном окружении
    DSN может переопределяться через ENV (см. src/main.py::_resolve_db_dsn),
    но YAML-схема всё равно его описывает.
    """

    model_config = ConfigDict(extra="forbid")

    dsn: Optional[str] = Field(
        default=None,
        description="Строка подключения к TimescaleDB/PostgreSQL.",
    )
    pool_min_size: int = Field(
        1,
        ge=1,
        description="Минимальный размер пула соединений.",
    )
    pool_max_size: int = Field(
        10,
        ge=1,
        description="Максимальный размер пула соединений.",
    )

    @field_validator("pool_max_size")
    def _check_pool_sizes(cls, max_size: int, info) -> int:
        """
        Гарантирует, что максимальный размер пула не меньше минимального.
        """
        min_size = info.data.get("pool_min_size", 1) if hasattr(info, "data") else 1
        if isinstance(min_size, int) and max_size < min_size:
            raise ValueError("db.pool_max_size must be >= db.pool_min_size")
        return max_size


class UIConfigSchema(BaseModel):
    """
    Секция `ui` в settings.yaml.

    Параметры UI/notifications слоя: адрес фронтенда и настройки SSE.
    """

    model_config = ConfigDict(extra="forbid")

    public_base_url: str = Field(
        ...,
        description="Базовый URL фронтенда / UI.",
    )
    enable_sse: bool = Field(
        True,
        description="Флаг включения SSE-стриминга для UI.",
    )
    sse_channel: str = Field(
        "signals",
        description="Имя канала pub/sub для realtime-событий.",
    )


class RootConfigSchema(BaseModel):
    """
    Корневая схема YAML-конфига (config/settings.yaml).

    Эта модель описывает ожидаемую структуру файла настроек и используется
    ConfigLoader'ом для:
      * валидации значений;
      * генерации ошибок конфигурации с понятным контекстом;
      * построения AppConfig из src/core/models.py.
    """

    model_config = ConfigDict(extra="forbid")

    trading: TradingConfigSchema
    risk: RiskConfigSchema
    bybit: BybitConfigSchema
    db: DBConfigSchema
    ui: UIConfigSchema

    # Необязательное поле, позволяющее прокидывать путь до исходного файла
    # при валидации/загрузке (удобно для логов и CLI).
    config_path: Optional[Path] = Field(
        default=None,
        description="Опциональный путь до исходного YAML-конфига.",
    )


__all__ = [
    "TradingConfigSchema",
    "RiskConfigSchema",
    "BybitConfigSchema",
    "DBConfigSchema",
    "UIConfigSchema",
    "RootConfigSchema",
]
