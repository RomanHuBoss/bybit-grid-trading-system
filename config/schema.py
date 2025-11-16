# config/schema.py
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, conint, confloat


__all__ = [
    "TradingConfig",
    "RiskConfig",
    "BybitConfig",
    "AppConfig",
]


class TradingConfig(BaseModel):
    """
    Торговые параметры стратегии AVI-5.
    Валидация числовых ограничений для размеров позиций.
    """

    max_stake: confloat(gt=0) = Field(
        ...,
        description=(
            "Максимальный размер позиции (stake) для стратегии AVI-5 "
            "в базовой валюте счёта. Должен быть > 0."
        ),
    )

    class Config:
        # Конфиг — часть статической структуры, неизвестные поля считаем ошибкой.
        extra = "forbid"


class RiskConfig(BaseModel):
    """
    Риск-параметры, ограничивающие количество одновременных позиций.
    """

    max_concurrent: conint(ge=1) = Field(
        ...,
        description=(
            "Максимальное количество одновременных открытых позиций. "
            "Не может быть меньше 1."
        ),
    )

    class Config:
        extra = "forbid"


class BybitConfig(BaseModel):
    """
    Публично-конфигурационная часть интеграции с Bybit.
    Секреты (private key, TOTP и т.п.) хранятся вне YAML.
    """

    api_key: str = Field(
        ...,
        min_length=1,
        description=(
            "Публичная часть Bybit API key. "
            "В settings.yaml задаётся как плейсхолдер \"${BYBIT_API_KEY}\", "
            "который разворачивается загрузчиком конфигурации из переменных окружения."
        ),
    )

    class Config:
        extra = "forbid"


class AppConfig(BaseModel):
    """
    Корневая схема конфигурации приложения.

    Описывает структуру config/settings.yaml и служит единым
    контрактом для всех потребителей конфигурации: стратегий,
    сервисов интеграции, API и скриптов.
    """

    trading: TradingConfig = Field(
        ...,
        description="Торговые параметры (лимиты AVI-5).",
    )
    risk: RiskConfig = Field(
        ...,
        description="Риск-параметры (ограничения по одновременным позициям).",
    )
    bybit: BybitConfig = Field(
        ...,
        description="Конфигурация интеграции с Bybit (без секретов).",
    )

    class Config:
        # Запрещаем неожиданные поля на верхнем уровне конфигурации.
        extra = "forbid"


def build_app_config(raw: dict[str, Any]) -> AppConfig:
    """
    Утилита для явного построения AppConfig из уже разобранного YAML-словаря.

    Предполагается, что:
    - YAML уже прочитан с диска;
    - плейсхолдеры вида "${VAR_NAME}" уже развёрнуты через окружение;
    - секреты не попадают в raw (они должны подмешиваться отдельным слоем).

    Функция оставлена здесь, чтобы упростить использование схем в коде:
    ConfigLoader может вызвать её или напрямую инстанцировать AppConfig(**raw).
    """
    return AppConfig(**raw)
