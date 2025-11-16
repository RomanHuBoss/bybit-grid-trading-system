from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, BaseSettings, Field, root_validator, validator

from src.core.exceptions import InvalidCandleError


class TradingConfig(BaseModel):
    """
    Торговые параметры стратегии в целом (не алгоритмические детали AVI-5).
    Значения читаются из settings.yaml (секция trading.*).
    """

    max_stake: Decimal = Field(..., gt=Decimal("0"), description="Максимальный риск на сделку в USD.")
    research_mode: bool = Field(
        default=False,
        description=(
            "Режим исследовательских логов/метрик; "
            "часть данных пишется только при включённом режиме."
        ),
    )


class RiskConfig(BaseModel):
    """
    Параметры риск-менеджмента на уровне аккаунта.
    Соответствуют ограничениям MAX_CONCURRENT_POSITIONS, MAX_TOTAL_RISK_R и per-base лимитам.
    """

    max_concurrent: int = Field(..., ge=1, description="Максимальное количество одновременно открытых позиций.")
    max_total_risk_r: Decimal = Field(
        ..., gt=Decimal("0"), description="Максимальный суммарный риск во всех позициях в R-мультипликаторах."
    )
    max_positions_per_symbol: int = Field(
        2, ge=1, description="Максимум открытых позиций на один базовый актив (per-base лимит)."
    )
    anti_churn_cooldown_minutes: int = Field(
        15,
        ge=0,
        description="Минимальный интервал (в минутах) между закрытием и новым открытием позиции по символу.",
    )

    @validator("max_total_risk_r")
    def _check_max_total_risk_r(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("max_total_risk_r must be positive")
        return v


class BybitConfig(BaseModel):
    """
    Настройки доступа к бирже Bybit и базовые URL'ы.
    Секреты сами по себе берутся из ENV/Vault, см. config_loader.
    """

    api_key: str = Field(..., min_length=1)
    api_secret: str = Field(..., min_length=1)

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


class DBConfig(BaseModel):
    """
    Настройки подключения к базе данных.
    """

    dsn: str = Field(..., description="Строка подключения к TimescaleDB/PostgreSQL.")
    pool_min_size: int = Field(1, ge=1)
    pool_max_size: int = Field(10, ge=1)

    @validator("pool_max_size")
    def _check_pool_sizes(cls, max_size: int, values: Dict[str, object]) -> int:
        min_size = values.get("pool_min_size", 1)
        if isinstance(min_size, int) and max_size < min_size:
            raise ValueError("pool_max_size must be >= pool_min_size")
        return max_size


class UIConfig(BaseModel):
    """
    Параметры UI/notifications слоя.
    """

    public_base_url: str = Field(..., description="Базовый URL фронтенда / UI.")
    enable_sse: bool = Field(True, description="Флаг включения SSE-стриминга для UI.")
    sse_channel: str = Field("signals", description="Имя канала pub/sub для realtime-событий.")


class AVI5Config(BaseModel):
    """
    Параметры конкретной торговой стратегии AVI-5, используемые SignalEngine.
    """

    theta: float = Field(..., gt=0.0, lt=1.0, description="Доля R, используемая для расчёта размеров позиций.")
    atr_window: int = Field(14, ge=1, description="Окно для расчёта ATR.")
    atr_multiplier: float = Field(..., gt=0.0, description="Множитель ATR для расчёта уровней SL/TP.")
    spread_threshold: float = Field(
        0.0,
        ge=0.0,
        description="Максимально допустимый спред (в процентах или bps, согласно конфигурации стратегии).",
    )

    @validator("theta")
    def _validate_theta(cls, v: float) -> float:
        # θ в требованиях фигурирует в диапазоне около [0.15, 0.50],
        # но здесь мягко ограничиваем общим (0, 1] — детали диапазона решаются конфигом.
        if not (0.0 < v <= 1.0):
            raise ValueError("theta must be in (0, 1]")
        return v


class ConfirmedCandle(BaseModel):
    """
    Подтверждённая 5-минутная свеча с базовым sanity-check.

    Используется как вход SignalEngine и для последующей аналитики.
    """

    symbol: str = Field(..., min_length=1)
    open_time: datetime = Field(..., description="Начало интервала свечи (UTC).")
    close_time: datetime = Field(..., description="Конец интервала свечи (UTC).")
    open: Decimal = Field(..., description="Цена открытия.")
    high: Decimal = Field(..., description="Максимальная цена.")
    low: Decimal = Field(..., description="Минимальная цена.")
    close: Decimal = Field(..., description="Цена закрытия.")
    volume: Decimal = Field(..., ge=Decimal("0"), description="Объём за интервал.")
    confirmed: bool = Field(
        True,
        description="Флаг, что свеча подтверждена (бар полностью сформирован и можно торговать по его данным).",
    )

    @root_validator
    def _sanity_check(cls, values: Dict[str, object]) -> Dict[str, object]:
        open_ = values.get("open")
        high = values.get("high")
        low = values.get("low")
        close = values.get("close")
        volume = values.get("volume")
        close_time = values.get("close_time")
        confirmed = values.get("confirmed", True)

        # Приведение к Decimal
        def as_decimal(x: object) -> Decimal:
            if isinstance(x, Decimal):
                return x
            return Decimal(str(x))

        try:
            open_d = as_decimal(open_)
            high_d = as_decimal(high)
            low_d = as_decimal(low)
            close_d = as_decimal(close)
            volume_d = as_decimal(volume)
        except Exception as exc:  # noqa: BLE001
            raise InvalidCandleError("Failed to coerce OHLCV values to Decimal", details={"error": str(exc)})

        if high_d < low_d:
            raise InvalidCandleError(
                "High price must be >= low price",
                details={"high": str(high_d), "low": str(low_d)},
            )

        if not (low_d <= open_d <= high_d):
            raise InvalidCandleError(
                "Open price must be within [low, high]",
                details={"open": str(open_d), "low": str(low_d), "high": str(high_d)},
            )

        if not (low_d <= close_d <= high_d):
            raise InvalidCandleError(
                "Close price must be within [low, high]",
                details={"close": str(close_d), "low": str(low_d), "high": str(high_d)},
            )

        if volume_d < 0:
            raise InvalidCandleError(
                "Volume must be non-negative",
                details={"volume": str(volume_d)},
            )

        # Проверка того, что бар действительно завершён
        if confirmed and isinstance(close_time, datetime):
            now = datetime.now(timezone.utc)
            if close_time > now:
                raise InvalidCandleError(
                    "Candle cannot be confirmed before its close_time",
                    details={"close_time": close_time.isoformat(), "now": now.isoformat()},
                )

        # Нормализуем значения в поля модели
        values["open"] = open_d
        values["high"] = high_d
        values["low"] = low_d
        values["close"] = close_d
        values["volume"] = volume_d
        return values


class Signal(BaseModel):
    """
    Сигнал на открытие позиции, публикуемый AVI-5 и сохраняемый в БД.
    """

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    symbol: str = Field(..., min_length=1)
    direction: str = Field(..., regex="^(long|short)$")
    entry_price: Decimal = Field(..., gt=Decimal("0"))
    stake_usd: Decimal = Field(..., gt=Decimal("0"), description="Риск в долларах (1R) для данного сигнала.")
    probability: Decimal = Field(..., ge=Decimal("0"), le=Decimal("1"), description="Оценка p_win.")
    strategy: str = Field("AVI-5", description="Имя стратегии.")
    strategy_version: str = Field(..., description="Версия стратегии (например, 'avi5-1.0.0').")
    queued_until: Optional[datetime] = Field(
        None,
        description="Момент времени, до которого сигнал допускается к постановке в очередь.",
    )

    tp1: Optional[Decimal] = Field(
        None,
        description="Первый уровень take-profit.",
    )
    tp2: Optional[Decimal] = Field(
        None,
        description="Второй уровень take-profit.",
    )
    tp3: Optional[Decimal] = Field(
        None,
        description="Третий уровень take-profit.",
    )
    stop_loss: Optional[Decimal] = Field(
        None,
        description="Уровень стоп-лосса.",
    )

    error_code: Optional[int] = Field(
        None,
        description="Код ошибки при обработке сигнала (если применимо).",
    )
    error_message: Optional[str] = Field(
        None,
        description="Текст ошибки при обработке сигнала (если применимо).",
    )

    @validator("tp1", "tp2", "tp3", "stop_loss")
    def _check_price_non_negative(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and v <= 0:
            raise ValueError("Price levels must be positive")
        return v


class Position(BaseModel):
    """
    Модель открытой/закрытой позиции, связанной с конкретным сигналом.
    """

    id: UUID = Field(default_factory=uuid4)
    signal_id: UUID = Field(..., description="Идентификатор исходного сигнала.")
    opened_at: datetime = Field(..., description="Время открытия позиции (UTC).")
    closed_at: Optional[datetime] = Field(None, description="Время закрытия позиции (UTC), если позиция закрыта.")
    symbol: str = Field(..., min_length=1)
    direction: str = Field(..., regex="^(long|short)$")
    entry_price: Decimal = Field(..., gt=Decimal("0"))
    size_base: Decimal = Field(..., gt=Decimal("0"), description="Размер позиции в базовой валюте.")
    size_quote: Decimal = Field(..., gt=Decimal("0"), description="Размер позиции в котируемой валюте (ноционал).")

    fill_ratio: Decimal = Field(
        Decimal("1"),
        ge=Decimal("0"),
        le=Decimal("1"),
        description="Коэффициент фактически исполненного объёма относительно запрошенного.",
    )
    slippage: Decimal = Field(
        Decimal("0"),
        description="Фактическое проскальзывание (в деньгах или bps, в зависимости от конвенции отчётности).",
    )
    funding: Decimal = Field(
        Decimal("0"),
        description="Накопленная сумма funding-платежей по позиции.",
    )

    @validator("size_base", "size_quote")
    def _check_size_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Position size must be positive")
        return v

    @validator("fill_ratio")
    def _check_fill_ratio(cls, v: Decimal) -> Decimal:
        if v < 0 or v > 1:
            raise ValueError("fill_ratio must be within [0, 1]")
        return v


class RiskLimits(BaseModel):
    """
    Снимок актуальных риск-лимитов, который использует RiskManager.
    """

    max_concurrent: int = Field(..., ge=1)
    max_total_risk_r: Decimal = Field(..., gt=Decimal("0"))
    max_positions_per_symbol: int = Field(..., ge=1)
    per_symbol_risk_r: Dict[str, Decimal] = Field(
        default_factory=dict,
        description="Дополнительные пер-символьные лимиты риска в R для отдельных инструментов.",
    )

    @validator("per_symbol_risk_r")
    def _check_per_symbol_limits(
        cls,
        v: Dict[str, Decimal],
    ) -> Dict[str, Decimal]:
        for symbol, limit in v.items():
            if limit <= 0:
                raise ValueError(f"Per-symbol risk limit for {symbol} must be positive")
        return v


class SlippageRecord(BaseModel):
    """
    Запись о проскальзывании, логируется при каждом fill ордера.
    """

    position_id: UUID = Field(..., description="ID позиции, к которой относится измерение.")
    symbol: str = Field(..., min_length=1)
    direction: str = Field(..., regex="^(long|short)$")

    expected_price: Decimal = Field(..., gt=Decimal("0"))
    actual_price: Decimal = Field(..., gt=Decimal("0"))
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AppConfig(BaseSettings):
    """
    Корневая модель конфигурации приложения.

    Содержит сгруппированные настройки по доменам:
    - trading: общие торговые параметры;
    - risk: лимиты риск-менеджмента;
    - bybit: параметры подключения к бирже;
    - db: настройки БД;
    - ui: параметры интеграции с UI.
    """

    trading: TradingConfig
    risk: RiskConfig
    bybit: BybitConfig
    db: DBConfig
    ui: UIConfig

    class Config:
        env_prefix = ""
        env_nested_delimiter = "__"
        case_sensitive = False
