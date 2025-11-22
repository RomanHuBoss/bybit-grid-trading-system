from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from redis.asyncio import Redis

from src.core.logging_config import get_logger
from src.core.models import Signal
from src.db.repositories.signal_repository import SignalRepository

__all__ = ["CalibrationService"]

# Для работы с probability и порогами хотим вполне приличную точность
getcontext().prec = 28

logger = get_logger("strategies.calibration")


# ---------------------------------------------------------------------------
# Конфиги калибрации
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationParams:
    """
    Параметры калибрации AVI-5.

    Эти параметры не лезут в глобальный конфиг, а задаются при создании
    CalibrationService (в том числе для тестов / экспериментов).
    """

    # Окно истории для обучения (train)
    train_days: int = 180
    # Окно для out-of-sample / мониторинга дрифта
    oos_days: int = 30

    # Границы и шаг поиска порога theta
    theta_min: Decimal = Decimal("0.15")
    theta_max: Decimal = Decimal("0.50")
    # Вместо явного grid search используем квантиль — но границы всё равно
    # применяем как clamp.
    target_quantile: Decimal = Decimal("0.7")

    # Порог PSI, после которого считаем, что дрифт распределения probability
    # стал подозрительным.
    psi_threshold: Decimal = Decimal("0.2")

    # Ключи в Redis
    redis_theta_key: str = "avi5:calibration:theta_per_hour"
    redis_psi_baseline_key: str = "avi5:calibration:probability_hist_baseline"


# ---------------------------------------------------------------------------
# Сервис калибрации
# ---------------------------------------------------------------------------


class CalibrationService:
    """
    Сервис offline-калибрации порога probability для AVI-5.

    Отвечает за:
      * выбор порога theta(h) по часам суток на основе истории сигналов;
      * запись карты theta в Redis;
      * оценку дрифта распределения probability через PSI.

    Сервис не знает ничего про HTTP / CLI — предполагается, что его вызывают
    внешние джобы/хэндлеры.
    """

    def __init__(
        self,
        *,
        redis: Redis,
        signal_repository: SignalRepository,
        params: Optional[CalibrationParams] = None,
    ) -> None:
        self._redis = redis
        self._signals = signal_repository
        self._params = params or CalibrationParams()

    # ------------------------------------------------------------------ #
    # Публичный API
    # ------------------------------------------------------------------ #

    async def calibrate(
        self,
        *,
        now: Optional[datetime] = None,
        symbol: Optional[str] = None,
    ) -> Dict[int, Decimal]:
        """
        Пересчитать карту theta(h) по истории сигналов и записать её в Redis.

        Алгоритм (упрощённая версия, согласованная со спецификацией):
          * берём сигналы за train_days;
          * группируем по часу суток created_at.hour;
          * для каждого часа считаем квантиль target_quantile по probability;
          * ограничиваем результат [theta_min, theta_max];
          * если по какому-то часу данных нет — используем theta_min;
          * сохраняем карту {hour -> str(theta)} в Redis в JSON.

        :param now: Текущее время, по умолчанию UTC now.
        :param symbol: Опциональный фильтр по символу (если None — по всем).
        :return: Словарь {час -> theta}, который был записан.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        train_since = now - timedelta(days=self._params.train_days)

        logger.info(
            "Starting AVI-5 calibration",
            symbol=symbol,
            train_since=train_since.isoformat(),
            train_days=self._params.train_days,
        )

        # Забираем историю сигналов из БД.
        signals = await self._load_signals(since=train_since, symbol=symbol)

        if not signals:
            logger.warning(
                "No signals found for calibration window; "
                "theta map will fallback to theta_min for all hours",
            )
            theta_map = {hour: self._params.theta_min for hour in range(24)}
            # Не трогаем PSI baseline, чтобы не затирать его пустой выборкой.
            logger.warning(
                "Skipping PSI baseline update: no signals available for calibration window",
            )
        else:
            theta_map = self._build_theta_map(signals)

            # Сохраняем baseline распределения probability для PSI-мониторинга.
            hist = self._build_probability_histogram(signals)
            await self._save_histogram_baseline(hist)

        # Пишем theta_map в Redis.
        await self._save_theta_map(theta_map)

        logger.info(
            "AVI-5 calibration finished",
            theta_map={h: str(v) for h, v in theta_map.items()},
        )

        return theta_map

    async def check_psi_drift(
        self,
        *,
        now: Optional[datetime] = None,
        symbol: Optional[str] = None,
    ) -> Tuple[Optional[Decimal], bool]:
        """
        Проверить дрифт распределения probability через PSI.

        Сценарий:
          * забираем сохранённый baseline-гистограмму из Redis;
          * считаем гистограмму по последним oos_days;
          * считаем PSI между baseline и текущей выборкой;
          * сравниваем с порогом psi_threshold.

        :param now: Текущее время, по умолчанию UTC now.
        :param symbol: Опциональный фильтр по символу (если None — по всем).
        :return: (psi, ok), где:
                 * psi == None, если нет baseline или нет актуальной выборки;
                 * ok == True, если дрифт в пределах нормы
                   (psi <= psi_threshold).
        """
        if now is None:
            now = datetime.now(timezone.utc)

        baseline = await self._load_histogram_baseline()
        if baseline is None:
            logger.warning("PSI baseline is missing; cannot compute drift")
            return None, False

        oos_since = now - timedelta(days=self._params.oos_days)
        signals = await self._load_signals(since=oos_since, symbol=symbol)

        if not signals:
            logger.warning("No signals in OOS window; PSI is undefined")
            return None, False

        current_hist = self._build_probability_histogram(signals)
        psi = self._compute_psi(baseline, current_hist)

        ok = psi <= self._params.psi_threshold

        logger.info(
            "PSI drift check",
            psi=str(psi),
            psi_threshold=str(self._params.psi_threshold),
            is_ok=ok,
        )

        return psi, ok

    # ------------------------------------------------------------------ #
    # Внутренние методы
    # ------------------------------------------------------------------ #

    async def _load_signals(
        self,
        *,
        since: datetime,
        symbol: Optional[str],
    ) -> List[Signal]:
        """
        Обёртка над SignalRepository.list_recent, которая забирает
        "достаточно много" сигналов от указанного момента времени.

        list_recent отдаёт limit последних записей, поэтому мы берём
        заведомо большой limit, а фильтрацию по since доверяем SQL.
        """
        # 10k сигналов для offline-калибрации обычно достаточно,
        # при необходимости параметр можно сделать настраиваемым.
        limit = 10_000

        signals = await self._signals.list_recent(
            limit=limit,
            symbol=symbol,
            since=since,
        )

        logger.info(
            "Loaded signals for calibration",
            count=len(signals),
            symbol=symbol,
            since=since.isoformat(),
        )

        return signals

    def _build_theta_map(self, signals: Sequence[Signal]) -> Dict[int, Decimal]:
        """
        Построить карту theta(h) на основе квантилей probability по часам.

        Для каждого часа h:
          * берём probability всех сигналов c created_at.hour == h;
          * считаем квантиль target_quantile;
          * clamp в [theta_min, theta_max];
          * если данных нет — theta_min.
        """
        buckets: Dict[int, List[Decimal]] = {h: [] for h in range(24)}

        for s in signals:
            hour = s.created_at.hour
            buckets[hour].append(s.probability)

        theta_map: Dict[int, Decimal] = {}

        for hour in range(24):
            probs = buckets[hour]
            if not probs:
                theta_map[hour] = self._params.theta_min
                continue

            probs_sorted = sorted(probs)
            # Индекс квантиля: floor(q * (n-1))
            q = self._params.target_quantile
            n = len(probs_sorted)
            idx_raw = (q * Decimal(n - 1)).to_integral_value(
                rounding=getcontext().rounding,
            )
            idx = int(idx_raw)
            candidate = probs_sorted[idx]

            # Ограничиваем диапазоном [theta_min, theta_max]
            if candidate < self._params.theta_min:
                candidate = self._params.theta_min
            if candidate > self._params.theta_max:
                candidate = self._params.theta_max

            theta_map[hour] = candidate

        return theta_map

    @staticmethod
    def _build_probability_histogram(
        signals: Iterable[Signal],
        *,
        bins: int = 10,
    ) -> List[Decimal]:
        """
        Построить простую равномерную гистограмму probability на [0, 1].

        :param signals: Итерация по Signal.
        :param bins: Количество бинов (по умолчанию 10 — по 0.1).
        :return: Список длины bins, суммарно дающих 1 (в Decimal).
        :raises ValueError: если выборка пуста.
        """
        probs: List[Decimal] = [s.probability for s in signals]
        if not probs:
            raise ValueError("Cannot build histogram from empty probability set")

        counts = [0] * bins
        for p in probs:
            # Страхуемся от граничных случаев вроде 1.0000...
            if p < 0:
                idx = 0
            elif p >= 1:
                idx = bins - 1
            else:
                idx = int(p * bins)
                if idx >= bins:
                    idx = bins - 1
                if idx < 0:
                    idx = 0
            counts[idx] += 1

        total = Decimal(len(probs))
        return [Decimal(c) / total for c in counts]

    @staticmethod
    def _compute_psi(
        expected: Sequence[Decimal],
        actual: Sequence[Decimal],
    ) -> Decimal:
        """
        Population Stability Index (PSI) для двух гистограмм.

        PSI = sum( (a_i - e_i) * ln(a_i / e_i) )

        Нули заменяем на небольшое epsilon, чтобы не ловить деление на 0.
        """
        if len(expected) != len(actual):
            raise ValueError("PSI histograms must have the same length")

        epsilon = Decimal("1e-6")
        psi = Decimal("0")

        for e, a in zip(expected, actual):
            e_safe = e if e > 0 else epsilon
            a_safe = a if a > 0 else epsilon
            diff = a_safe - e_safe
            ratio = a_safe / e_safe
            # ln через natural log — в Decimal его нет, поэтому считаем
            # через float, что для PSI вполне достаточно.
            from math import log

            psi += diff * Decimal(str(log(float(ratio))))

        return psi

    async def _save_theta_map(self, theta_map: Dict[int, Decimal]) -> None:
        """
        Сохранить карту theta(h) в Redis в виде JSON.

        Формат:
          {
            "0": "0.23",
            "1": "0.25",
            ...
          }
        """
        payload = {str(h): str(v) for h, v in theta_map.items()}

        await self._redis.set(self._params.redis_theta_key, json.dumps(payload))
        logger.info(
            "Theta map saved to Redis",
            key=self._params.redis_theta_key,
        )

    async def _save_histogram_baseline(self, hist: Sequence[Decimal]) -> None:
        """
        Сохранить baseline-гистограмму probability в Redis.
        """
        payload = [str(x) for x in hist]
        await self._redis.set(
            self._params.redis_psi_baseline_key,
            json.dumps(payload),
        )
        logger.info(
            "PSI baseline histogram saved to Redis",
            key=self._params.redis_psi_baseline_key,
        )

    async def _load_histogram_baseline(self) -> Optional[List[Decimal]]:
        """
        Загрузить baseline-гистограмму probability из Redis.

        :return: Список Decimal или None, если baseline отсутствует.
        """
        raw = await self._redis.get(self._params.redis_psi_baseline_key)
        if raw is None:
            return None

        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.error(
                "Failed to decode PSI baseline histogram from Redis; "
                "treating as missing",
            )
            return None

        hist: List[Decimal] = []
        for item in data:
            try:
                hist.append(Decimal(str(item)))
            except Exception:  # noqa: BLE001
                logger.error(
                    "Invalid histogram entry in PSI baseline; skipping entry",
                    raw_item=item,
                )
        return hist if hist else None
