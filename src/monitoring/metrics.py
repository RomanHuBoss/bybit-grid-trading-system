from typing import Dict

from prometheus_client import Counter, Gauge, Histogram


Labels = Dict[str, str]


class Metrics:
    """
    Синглтон-обёртка над Prometheus-метриками системы AVI-5.
    """

    _instance = None
    _initialized = False

    def __new__(cls) -> "Metrics":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Инициализируем метрики только один раз
        if self.__class__._initialized:
            return
        self.__class__._initialized = True

        # Latency
        self._signal_generation_latency_ms: Histogram = Histogram(
            "signal_generation_latency_ms",
            "Latency of signal generation in milliseconds.",
        )
        self._be_delivery_latency_ms: Histogram = Histogram(
            "be_delivery_latency_ms",
            "Latency of backend event delivery in milliseconds.",
        )

        # Бизнес-метрики
        self._signals_generated_total: Counter = Counter(
            "signals_generated_total",
            "Total number of signals generated.",
            ("symbol", "side"),
        )
        self._positions_opened_total: Counter = Counter(
            "positions_opened_total",
            "Total number of positions opened.",
            ("symbol", "side"),
        )
        # Имя метрики win-rate выровнено с docs/alerting_rules.md (strategy_wr)
        self._strategy_wr: Gauge = Gauge(
            "strategy_wr",
            "Strategy win rate over a rolling window (0..1).",
            ("window_days",),
        )
        self._profit_factor: Gauge = Gauge(
            "profit_factor",
            "Profit factor over a rolling window.",
            ("window_days",),
        )
        # Имя метрики MaxDD выровнено с docs/alerting_rules.md (strategy_max_drawdown)
        self._strategy_max_drawdown: Gauge = Gauge(
            "strategy_max_drawdown",
            "Maximum drawdown of the strategy (fraction or percent).",
        )

        # Инфраструктурные метрики
        self._ws_reconnects_total: Counter = Counter(
            "ws_reconnects_total",
            "Total number of WebSocket reconnects.",
            ("channel",),
        )
        self._rate_limit_hits_total: Counter = Counter(
            "rate_limit_hits_total",
            "Total number of Bybit rate-limit hits.",
            ("endpoint",),
        )
        self._db_query_duration_ms: Histogram = Histogram(
            "db_query_duration_ms",
            "Database query duration in milliseconds.",
            ("query_name",),
        )

    # -------- Latency --------

    def signal_latency(self, latency_ms: float) -> None:
        """
        Обновляет гистограмму latency генерации сигналов.
        """
        if latency_ms < 0:
            raise TypeError("latency_ms must be non-negative.")
        self._signal_generation_latency_ms.observe(latency_ms)

    def be_delivery_latency(self, latency_ms: float) -> None:
        """
        Обновляет гистограмму latency доставки BE-событий.
        """
        if latency_ms < 0:
            raise TypeError("latency_ms must be non-negative.")
        self._be_delivery_latency_ms.observe(latency_ms)

    # -------- Бизнес-метрики --------

    def increment_signals(self, symbol: str, side: str) -> None:
        """
        Инкрементирует счётчик сгенерированных сигналов.

        :param symbol: Торговый инструмент (например, "BTCUSDT").
        :param side: Направление сделки: "long" или "short".
        """
        normalized_side = side.lower()
        if normalized_side not in ("long", "short"):
            raise ValueError("side must be 'long' or 'short'.")

        labels: Labels = {
            "symbol": symbol,
            "side": normalized_side,
        }
        self._signals_generated_total.labels(**labels).inc()

    def set_win_rate(self, window_days: int, value: float) -> None:
        """
        Устанавливает win-rate для заданного окна в днях.

        :param window_days: Размер окна в днях (для label).
        :param value: Значение win-rate в диапазоне [0.0, 1.0].
        """
        if not 0.0 <= value <= 1.0:
            raise ValueError("value must be in [0.0, 1.0].")

        labels: Labels = {"window_days": str(window_days)}
        # Метрика strategy_wr — источник для алерта wr_drop
        self._strategy_wr.labels(**labels).set(value)

    def set_profit_factor(self, window_days: int, value: float) -> None:
        """
        Устанавливает profit-factor для заданного окна в днях.

        :param window_days: Размер окна в днях (для label).
        :param value: Profit factor (неотрицательное число).
        """
        if value < 0:
            raise ValueError("value must be non-negative.")

        labels: Labels = {"window_days": str(window_days)}
        self._profit_factor.labels(**labels).set(value)

    def set_max_drawdown(self, value_pct: float) -> None:
        """
        Устанавливает текущее значение MaxDD.

        :param value_pct: Просадка (в долях или процентах по договорённости).
        """
        if value_pct < 0:
            raise ValueError("value_pct must be non-negative.")

        # Метрика strategy_max_drawdown — источник для алерта max_drawdown_exceeded
        self._strategy_max_drawdown.set(value_pct)

    # -------- Инфраструктурные метрики --------

    def increment_ws_reconnects(self, channel: str) -> None:
        """
        Инкрементирует счётчик reconnect'ов WebSocket по каналу.

        :param channel: Логическое имя/тип канала (например, "kline", "orders").
        """
        if not channel:
            raise ValueError("channel must be non-empty.")

        labels: Labels = {"channel": channel}
        self._ws_reconnects_total.labels(**labels).inc()

    def increment_rate_limit_hits(self, endpoint: str) -> None:
        """
        Инкрементирует счётчик попаданий в rate-limit Bybit по endpoint.

        :param endpoint: Логическое имя/путь endpoint'а Bybit.
        """
        if not endpoint:
            raise ValueError("endpoint must be non-empty.")

        labels: Labels = {"endpoint": endpoint}
        self._rate_limit_hits_total.labels(**labels).inc()

    def db_query_duration(self, query_name: str, duration_ms: float) -> None:
        """
        Наблюдает длительность выполнения запроса к БД.

        :param query_name: Логическое имя/тип запроса.
        :param duration_ms: Длительность в миллисекундах.
        """
        if duration_ms < 0:
            raise ValueError("duration_ms must be non-negative.")

        labels: Labels = {"query_name": query_name}
        self._db_query_duration_ms.labels(**labels).observe(duration_ms)
