from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from src.core.logging_config import get_logger

__all__ = [
    "AlertSeverity",
    "AlertEvent",
    "ALERT_RUNBOOKS",
    "build_alert_event",
]

logger = get_logger("monitoring.alerts")


class AlertSeverity(str, Enum):
    """
    Уровни важности алертов.

    Совпадают с теми, что описаны в docs/alerting_rules.md и
    используются Prometheus/Grafana.

    - critical  — требует немедленного вмешательства;
    - warning   — важно, но не обязательно немедленно;
    - info      — информационные / низкоприоритетные события.
    """

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# Соответствие alert_name → путь к runbook (относительно репозитория).
# Имена должны совпадать с monitoring/alerts.yml и docs/runbooks/*.md.
ALERT_RUNBOOKS: Mapping[str, str] = {
    "bybit_latency_degraded": "docs/runbooks/bybit_latency_degraded.md",
    "db_down": "docs/runbooks/db_down.md",
    "db_replication_lag": "docs/runbooks/db_replication_lag.md",
    "high_error_rate_bybit": "docs/runbooks/high_error_rate_bybit.md",
    "high_http_5xx_rate": "docs/runbooks/high_http_5xx_rate.md",
    "http_latency_degraded": "docs/runbooks/http_latency_degraded.md",
    "kill_switch_triggered": "docs/runbooks/kill_switch_triggered.md",
    "max_drawdown_exceeded": "docs/runbooks/max_drawdown_exceeded.md",
    "no_ws_data": "docs/runbooks/no_ws_data.md",
    "redis_down": "docs/runbooks/redis_down.md",
    "slippage_spike": "docs/runbooks/slippage_spike.md",
    "wr_drop": "docs/runbooks/wr_drop.md",
}


@dataclass(frozen=True)
class AlertEvent:
    """
    Единая структура алерта, передаваемая из бизнес-логики наружу.

    Эта структура НЕ привязана к конкретной системе мониторинга —
    её можно сериализовать в лог, в вебхук, в Prometheus alertmanager
    (через адаптер) и т.д.
    """

    alert_name: str
    severity: AlertSeverity
    message: str
    timestamp: datetime
    labels: Dict[str, str]
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """
        Преобразовать алерт в плоскую структуру для сериализации (JSON/log).
        """
        return {
            "alert_name": self.alert_name,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "labels": dict(self.labels),
            "payload": dict(self.payload),
        }


def _normalize_alert_name(name: str) -> str:
    """
    Нормализовать имя алерта к нижнему регистру (snake_case).

    Имя алерта — это ключ, по которому:
        - находятся правила в monitoring/alerts.yml,
        - выбирается runbook из ALERT_RUNBOOKS.
    """
    return name.strip().lower()


def _resolve_runbook(alert_name: str) -> Optional[str]:
    """
    Найти runbook по имени алерта, если он описан.

    Возвращает относительный путь (например, "docs/runbooks/wr_drop.md") или None.
    """
    return ALERT_RUNBOOKS.get(alert_name)


def build_alert_event(
    *,
    alert_name: str,
    severity: AlertSeverity,
    message: str,
    labels: Optional[Mapping[str, str]] = None,
    payload: Optional[Mapping[str, Any]] = None,
    timestamp: Optional[datetime] = None,
) -> AlertEvent:
    """
    Построить AlertEvent c нормализованным именем и лейблами.

    Стандартные лейблы (минимальный контракт):
        - alert_name  — имя алерта (snake_case, соответствует alerts.yml);
        - severity    — уровень важности;
        - runbook     — относительный путь до runbook (если известен).

    Дополнительные доменные лейблы (symbol, exchange, component и т.п.)
    передаются через параметр `labels`.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    normalized_name = _normalize_alert_name(alert_name)

    base_labels: Dict[str, str] = {
        "alert_name": normalized_name,
        "severity": severity.value,
    }

    runbook_path = _resolve_runbook(normalized_name)
    if runbook_path is not None:
        base_labels["runbook"] = runbook_path

    if labels:
        # Пользовательские лейблы имеют приоритет в случае конфликта
        base_labels.update(labels)

    alert_payload: Dict[str, Any] = {}
    if payload:
        alert_payload.update(payload)

    event = AlertEvent(
        alert_name=normalized_name,
        severity=severity,
        message=message,
        timestamp=timestamp,
        labels=base_labels,
        payload=alert_payload,
    )

    # Лёгкое структурированное логирование для отладки: сам факт формирования алерта.
    logger.info(
        "Alert event created",
        extra={
            "alert": event.to_dict(),
        },
    )

    return event
