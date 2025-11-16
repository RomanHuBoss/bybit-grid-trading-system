"""
Глобальные константы системы Algo-Grid / AVI-5.

Здесь собраны строковые и числовые литералы, URL, лимиты и таймауты,
которые используются в разных частях проекта. Логики и функций быть
не должно — только значения.
"""

WS_PUBLIC_URL = "wss://stream.bybit.com/v5/public/linear"
WS_PRIVATE_URL = "wss://stream.bybit.com/v5/private"

# Максимальное количество подписок на WebSocket для одного соединения
MAX_WS_SUBSCRIPTIONS = 300

# Ограничения по rate-limit'ам
RATE_LIMIT_READ_PER_MIN = 1200
RATE_LIMIT_ORDER_PER_SEC = 10

# Время блокировки/«охлаждения» при churn, в секундах
CHURN_BLOCK_SEC = 900

# Целевые P95 по доставке backend-ивентов и подтверждений, в миллисекундах
BE_DELIVERY_P95_TARGET_MS = 5000
CONFIRM_LATENCY_P95_TARGET_MS = 5000
