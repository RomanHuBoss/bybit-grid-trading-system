# src/monitoring/alerts.py

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from src.core.logging_config import get_logger
from src.db.repositories.metrics_repository import MetricsRepository
from src.db.repositories.position_repository import PositionRepository
from src.notifications.ui_notifier import UINotifier
from src.notifications.webhooks import WebhookNotifier

__all__ = ["AlertManager"]

logger = get_logger("monitoring.alerts")


# Значения по умолчанию — лишь безопасные guard-рейлы.
# Реальные значения ожидается прокидывать из конфигурации через `thresholds`.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    # Минимально допустимый Win Rate за 30 дней (0.0–1.0)
    "win_rate_min": 0.45,
    # Минимально допустимый Profit Factor за 30 дней
    "profit_factor_min": 1.2,
    # Максимально допустимый Max Drawdown за 30 дней, %
    "max_drawdown_max": 30.0,
    # Максимально допустимая медиана входного slippage за 24 часа, bps
    "median_slippage_entry_bps_max": 10.0,
}


class AlertManager:
    """
    Менеджер алёртов и kill-switch для стратегии.

    Отвечает за:
    * чтение агрегированных метрик через MetricsRepository;
    * оценку их относительно риск-лимитов (thresholds);
    * публикацию метрик и состояния kill-switch в UI через UINotifier;
    * отправку webhook-уведомлений во внешний сервис через WebhookNotifier.

    Жизненный цикл:
    * создаётся один экземпляр на приложение;
    * периодически вызывается `check_and_alert()` планировщиком/cron-джобой.
    """

    def __init__(
        self,
        metrics_repo: MetricsRepository,
        position_repo: PositionRepository,
        ui_notifier: UINotifier,
        *,
        webhook_notifier: Optional[WebhookNotifier] = None,
        thresholds: Optional[Mapping[str, float]] = None,
        kill_switch_on_breach: bool = True,
    ) -> None:
        """
        :param metrics_repo: Репозиторий агрегированных метрик по позициям.
        :param position_repo: Репозиторий позиций (для метрик по открытым позициям).
        :param ui_notifier: Паблишер событий для UI.
        :param webhook_notifier: Опциональный отправитель webhook-уведомлений.
        :param thresholds: Карта порогов для алёртов. Ключи:
                           - "win_rate_min"
                           - "profit_factor_min"
                           - "max_drawdown_max"
                           - "median_slippage_entry_bps_max"
                           Неизвестные ключи игнорируются с предупреждением в логах.
        :param kill_switch_on_breach: Если True — при нарушении лимитов
                                      активируется kill-switch.
        """
        self._metrics_repo = metrics_repo
        self._position_repo = position_repo
        self._ui_notifier = ui_notifier
        self._webhook_notifier = webhook_notifier

        base_thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            for key, value in thresholds.items():
                if key not in base_thresholds:
                    logger.warning(
                        "Unknown alert threshold key, ignoring",
                        key=key,
                        value=value,
                    )
                    continue
                try:
                    base_thresholds[key] = float(value)
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid alert threshold value, keeping default",
                        key=key,
                        value=value,
                    )

        self._thresholds: Dict[str, float] = base_thresholds
        self._kill_switch_on_breach: bool = kill_switch_on_breach

        self._kill_switch_active: bool = False
        self._last_kill_switch_reason: Optional[str] = None
        self._last_metrics: Dict[str, Any] = {}
        self._last_breaches: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Публичные свойства
    # ------------------------------------------------------------------ #

    @property
    def kill_switch_active(self) -> bool:
        """Текущее состояние kill-switch (True — включен, новые позиции не открываем)."""
        return self._kill_switch_active

    @property
    def last_metrics(self) -> Dict[str, Any]:
        """
        Последние рассчитанные метрики.

        Возвращается копия словаря, чтобы снаружи нельзя было испортить внутреннее состояние.
        """
        return dict(self._last_metrics)

    @property
    def last_breaches(self) -> Dict[str, Any]:
        """
        Последние зафиксированные нарушения лимитов.

        Ключи соответствуют именам метрик, значения — словари вида:
        {
            "value": <фактическое значение>,
            "threshold": <порог>,
            "type": "min" | "max",
        }
        """
        return dict(self._last_breaches)

    # ------------------------------------------------------------------ #
    # Основной публичный метод
    # ------------------------------------------------------------------ #

    async def check_and_alert(self) -> Dict[str, Any]:
        """
        Рассчитать метрики, проверить их относительно порогов и при необходимости:

        * обновить состояние kill-switch;
        * запаблишить метрики и kill-switch в UI;
        * отправить webhook-уведомление.

        :return: Диагностический словарь:
                 {
                   "metrics": {...},
                   "breaches": {...},
                   "kill_switch_active": bool,
                   "kill_switch_changed": bool,
                 }

        :raises DatabaseError: при ошибках БД внутри репозиториев метрик/позиций.
        :raises RedisError: пробрасывается из MetricsRepository.refresh_cache (если вызовется)
                            и из других мест, где репозитории так задокументированы.
        """
        metrics = await self._collect_metrics()
        breaches = self._detect_breaches(metrics)
        kill_switch_changed = await self._apply_kill_switch(breaches)

        await self._publish_to_ui(metrics, kill_switch_changed)
        await self._send_webhook(metrics, breaches, kill_switch_changed)

        self._last_metrics = metrics
        self._last_breaches = breaches

        return {
            "metrics": metrics,
            "breaches": breaches,
            "kill_switch_active": self._kill_switch_active,
            "kill_switch_changed": kill_switch_changed,
        }

    # ------------------------------------------------------------------ #
    # Внутренние помощники: сбор метрик
    # ------------------------------------------------------------------ #

    async def _collect_metrics(self) -> Dict[str, Any]:
        """
        Собрать набор метрик, необходимых для принятия решений по алёртам.

        Метрики:
        * win_rate_last_30d                      — Win Rate за 30 дней [0.0, 1.0]
        * profit_factor_last_30d                — Profit Factor за 30 дней
        * max_drawdown_last_30d_pct             — Max Drawdown за 30 дней, %
        * median_slippage_entry_last_24h_bps    — медиана slippage_entry_bps за 24 часа
        * open_positions                         — количество открытых позиций
        """
        win_rate = await self._metrics_repo.get_win_rate_last_30d()
        profit_factor = await self._metrics_repo.get_profit_factor_last_30d()
        max_drawdown_pct = await self._metrics_repo.get_max_drawdown_last_30d()
        median_slippage_bps = await self._metrics_repo.get_median_slippage_last_24h()

        # Метрика по текущему числу открытых позиций — через PositionRepository
        open_positions = await self._position_repo.list_open()
        open_positions_count = len(open_positions)

        metrics: Dict[str, Any] = {
            "win_rate_last_30d": win_rate,
            "profit_factor_last_30d": profit_factor,
            "max_drawdown_last_30d_pct": max_drawdown_pct,
            "median_slippage_entry_last_24h_bps": median_slippage_bps,
            "open_positions": open_positions_count,
        }

        logger.debug("Monitoring metrics collected", **metrics)
        return metrics

    # ------------------------------------------------------------------ #
    # Внутренние помощники: определение нарушений лимитов
    # ------------------------------------------------------------------ #

    def _detect_breaches(self, metrics: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """
        На основе метрик и порогов сформировать список нарушений лимитов.

        Возвращает словарь, где ключ — имя метрики, значение — словарь:
        {
            "value": <фактическое значение>,
            "threshold": <порог>,
            "type": "min" | "max",
        }
        """
        thresholds = self._thresholds
        breaches: Dict[str, Dict[str, Any]] = {}

        win_rate = float(metrics.get("win_rate_last_30d", 0.0))
        if win_rate < thresholds["win_rate_min"]:
            breaches["win_rate_last_30d"] = {
                "value": win_rate,
                "threshold": thresholds["win_rate_min"],
                "type": "min",
            }

        profit_factor = float(metrics.get("profit_factor_last_30d", 0.0))
        if profit_factor < thresholds["profit_factor_min"]:
            breaches["profit_factor_last_30d"] = {
                "value": profit_factor,
                "threshold": thresholds["profit_factor_min"],
                "type": "min",
            }

        max_drawdown_pct = float(metrics.get("max_drawdown_last_30d_pct", 0.0))
        if max_drawdown_pct > thresholds["max_drawdown_max"]:
            breaches["max_drawdown_last_30d_pct"] = {
                "value": max_drawdown_pct,
                "threshold": thresholds["max_drawdown_max"],
                "type": "max",
            }

        median_slippage_bps = float(
            metrics.get("median_slippage_entry_last_24h_bps", 0.0)
        )
        if median_slippage_bps > thresholds["median_slippage_entry_bps_max"]:
            breaches["median_slippage_entry_last_24h_bps"] = {
                "value": median_slippage_bps,
                "threshold": thresholds["median_slippage_entry_bps_max"],
                "type": "max",
            }

        if breaches:
            logger.warning("Risk limits breached", breaches=breaches)

        return breaches

    # ------------------------------------------------------------------ #
    # Внутренние помощники: kill-switch
    # ------------------------------------------------------------------ #

    async def _apply_kill_switch(
        self,
        breaches: Mapping[str, Any],
    ) -> bool:
        """
        Применить логику kill-switch на основе нарушений лимитов.

        :param breaches: Нарушения лимитов (как возвращает `_detect_breaches`).
        :return: True, если состояние kill-switch изменилось.
        """
        if not self._kill_switch_on_breach:
            # Kill-switch отключен конфигурацией: только логируем нарушения.
            if breaches:
                logger.warning(
                    "Risk limits breached but kill_switch_on_breach is disabled",
                    breaches=breaches,
                )
            return False

        has_breaches = bool(breaches)
        previous_state = self._kill_switch_active

        if has_breaches and not self._kill_switch_active:
            # Включаем kill-switch
            self._kill_switch_active = True
            self._last_kill_switch_reason = self._build_breach_reason(breaches)
            logger.error(
                "Kill-switch activated due to risk limit breach",
                reason=self._last_kill_switch_reason,
                breaches=breaches,
            )
        elif not has_breaches and self._kill_switch_active:
            # Выключаем kill-switch
            self._kill_switch_active = False
            self._last_kill_switch_reason = "Risk metrics back to normal"
            logger.info(
                "Kill-switch deactivated, metrics back to normal",
            )

        return previous_state != self._kill_switch_active

    @staticmethod
    def _build_breach_reason(breaches: Mapping[str, Mapping[str, Any]]) -> str:
        """
        Сформировать человекочитаемое описание того, какие лимиты нарушены.
        """
        parts: list[str] = []
        for metric_name, info in breaches.items():
            value = info.get("value")
            threshold = info.get("threshold")
            breach_type = info.get("type")
            if breach_type == "min":
                parts.append(f"{metric_name}: {value:.4f} < {threshold:.4f}")
            elif breach_type == "max":
                parts.append(f"{metric_name}: {value:.4f} > {threshold:.4f}")
            else:
                parts.append(f"{metric_name}: {value} vs {threshold}")

        if not parts:
            return "Risk limits breached"

        return "Risk limits breached: " + "; ".join(parts)

    # ------------------------------------------------------------------ #
    # Внутренние помощники: публикация в UI и webhook
    # ------------------------------------------------------------------ #

    async def _publish_to_ui(
        self,
        metrics: Dict[str, Any],
        kill_switch_changed: bool,
    ) -> None:
        """
        Запаблишить метрики и состояние kill-switch в UI.

        Ошибки публикации не влияют на основную логику — только логируются.
        """
        # Метрики — публикуем всегда, даже если нет нарушений.
        try:
            await self._ui_notifier.publish_metrics(metrics)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to publish metrics to UI",
                error=str(exc),
            )

        # Состояние kill-switch публикуем только если оно изменилось
        if kill_switch_changed:
            try:
                await self._ui_notifier.publish_kill_switch(
                    active=self._kill_switch_active,
                    reason=self._last_kill_switch_reason,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to publish kill_switch state to UI",
                    error=str(exc),
                )

    async def _send_webhook(
        self,
        metrics: Dict[str, Any],
        breaches: Dict[str, Any],
        kill_switch_changed: bool,
    ) -> None:
        """
        Отправить webhook-уведомление (если сконфигурирован WebhookNotifier).

        Стратегия:
        * при первом появлении нарушений отправляется событие "risk_limits_breached";
        * при восстановлении метрик — "risk_limits_recovered";
        * если kill-switch отключён, но нарушения есть — всё равно шлём "risk_limits_breached".
        """
        if self._webhook_notifier is None:
            return

        # Если нет ни нарушений, ни изменения kill-switch — уведомлять нечего.
        if not breaches and not kill_switch_changed:
            return

        if breaches:
            event = "risk_limits_breached"
        elif self._kill_switch_active:
            # Теоретически сюда не попадём: нарушения есть, если kill-switch активен.
            event = "risk_limits_breached"
        else:
            event = "risk_limits_recovered"

        payload: Dict[str, Any] = {
            "event": event,
            "kill_switch_active": self._kill_switch_active,
            "reason": self._last_kill_switch_reason,
            "metrics": metrics,
            "breaches": breaches,
        }

        try:
            await self._webhook_notifier.send(payload)
        except Exception as exc:  # noqa: BLE001
            # Ошибки webhook не должны ломать мониторинг — только логируем.
            logger.error(
                "Failed to send webhook for alerts",
                error=str(exc),
                event=event,
            )
