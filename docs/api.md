# Bybit Algo-Grid API (AVI-5)

Документ описывает REST и SSE API для управления стратегией AVI-5 и мониторинга её работы.

Версия API: **v1**  
Базовый путь для REST: **`/api/v1`**

Во всех описаниях ниже пути указаны **без префикса** `/api/v1` (например, `GET /signals` → фактически `GET /api/v1/signals`).  
Исключения: `/metrics` и `/stream` находятся на корне.

---

## 1. Общие принципы

### 1.1. Аутентификация

Все защищённые эндпоинты (кроме `POST /auth/login`, `POST /auth/refresh`) используют JWT Bearer-аутентификацию:

```http
Authorization: Bearer <access_token>
````

* `access_token` — короткоживущий JWT.
* `refresh_token` — длинноживущий JWT для обновления `access_token`.

JWT содержит, как минимум, следующие поля:

* `sub` — идентификатор пользователя (`user_id`, UUID).
* `role` — роль пользователя: `viewer` / `trader` / `admin`.
* `exp` — время истечения токена (Unix timestamp).

### 1.2. Роли и права

Ролевая модель:

* **viewer**

  * Только чтение: сигналы, позиции, агрегированные метрики, часть конфигурации.
  * Может просматривать **только свои** API-ключи (`GET /api-keys`), но не создавать/удалять их.
* **trader**

  * Всё, что может `viewer`.
  * Управление позициями (ручное закрытие).
  * Включение/выключение торговли (через kill-switch).
  * Запуск калибровки (через отдельные скрипты, не публичный REST).
* **admin**

  * Всё, что может `trader`.
  * Управление пользователями и их ролями.
  * Управление API-ключами.
  * Изменение risk-конфигурации.

Подробная матрица по части эндпоинтов приведена в разделе [11. Матрица доступа](#11-матрица-доступа-к-эндпоинтам).

### 1.3. Формат времени

Во всех ответах время передаётся в формате **ISO 8601 / RFC3339** в UTC, например:

```text
2025-03-10T12:34:56Z
```

### 1.4. Ошибки

Если не указано иное, при ошибках возвращается JSON вида:

```json
{
  "detail": "Человекочитаемое описание ошибки"
}
```

Типичные статусы:

* `400 Bad Request` — некорректный запрос.
* `401 Unauthorized` — неправильные/отсутствующие креды.
* `403 Forbidden` — недостаточно прав (роль/статус пользователя).
* `404 Not Found` — ресурс не найден.
* `409 Conflict` — конфликт бизнес-логики (например, позиция уже закрыта).
* `422 Unprocessable Entity` — ошибка валидации тела запроса.
* `500 Internal Server Error` — внутренняя ошибка сервера.

---

## 2. Модели данных (DTO)

Ниже приведены упрощённые модели, используемые в ответах API.

### 2.1. User

```json
{
  "id": "5f8d0d55-8c3b-4d5b-9a5f-5f8d0d558c3b",
  "email": "user@example.com",
  "role": "trader",
  "is_active": true,
  "created_at": "2025-01-01T10:00:00Z",
  "last_login_at": "2025-01-10T12:00:00Z"
}
```

Поля:

* `id` — UUID (`users.user_id`).
* `email` — логин.
* `role` — `viewer` / `trader` / `admin`.
* `is_active` — активен ли пользователь.
* `created_at` — когда создан.
* `last_login_at` — последний успешный вход (может быть `null`).

### 2.2. Signal

```json
{
  "id": "b4f3c5db-4b1b-4b43-b0f9-5c4c7b741a11",
  "created_at": "2025-01-10T12:34:56Z",
  "symbol": "BTCUSDT",
  "direction": "long",
  "entry_price": 60000.5,
  "stake_usd": 1000.0,
  "probability": 0.83,
  "strategy": "AVI-5",
  "strategy_version": "1.0.0"
}
```

Поля:

* `id` — UUID записи в `signals`.
* `symbol` — тикер (например, `BTCUSDT`).
* `direction` — `long` или `short`.
* `entry_price` — предполагаемая цена входа.
* `stake_usd` — рекомендуемый размер позиции в USD.
* `probability` — вероятность срабатывания (0..1, с точностью до 3 знаков).
* `strategy` — имя стратегии (например, `AVI-5`).
* `strategy_version` — версия стратегии.

### 2.3. Position

```json
{
  "id": "a3c5e7f1-4e5a-4f1b-8c2e-7d9f1a2b3c4d",
  "signal_id": "b4f3c5db-4b1b-4b43-b0f9-5c4c7b741a11",
  "symbol": "BTCUSDT",
  "side": "long",
  "entry_price": 60000.5,
  "size_base": 0.05,
  "size_quote": 3000.0,
  "status": "open",
  "opened_at": "2025-01-10T12:35:10Z",
  "closed_at": null,
  "pnl_usd": null
}
```

Поля (подмножество `positions`):

* `id` — UUID позиции.
* `signal_id` — UUID сигнала, из которого она родилась (может быть `null` для ручных позиций).
* `symbol` — тикер.
* `side` — `long` / `short`.
* `entry_price` — цена входа.
* `size_base` / `size_quote` — размер позиции в базовой/квотируемой валюте.
* `status` — `open` / `closing` / `closed` / `error`.
* `opened_at`, `closed_at` — времена открытия/закрытия.
* `pnl_usd` — итоговый PnL (для закрытых позиций).

### 2.4. ApiKey

```json
{
  "id": "c5f7e9a1-2b3c-4d5e-8f9a-0b1c2d3e4f5a",
  "label": "Main Bybit Account",
  "exchange": "bybit",
  "env": "mainnet",
  "permissions": {
    "trading": true,
    "read_only": false,
    "withdrawals": false
  },
  "created_at": "2025-01-01T11:00:00Z",
  "last_used_at": "2025-01-10T13:00:00Z",
  "is_active": true
}
```

Поля:

* `id` — UUID строки в `api_keys`.
* `label` — человекочитаемое имя.
* `exchange` — биржа (`bybit`).
* `env` — окружение (`mainnet` / `testnet`).
* `permissions` — JSON-объект с правами.
* `created_at`, `last_used_at` — аудиторная информация.
* `is_active` — активен ли ключ.

---

## 3. Аутентификация

### 3.1. POST `/auth/login`

Авторизация по email + паролю + (опционально) 2FA-коду.

* **Роли**: доступен без авторизации.
* **Назначение**: выдача пары `access_token` / `refresh_token`.

#### Тело запроса

```json
{
  "email": "user@example.com",
  "password": "plaintext-or-hash-on-wire",
  "totp_code": "123456"
}
```

* `email` — email пользователя.
* `password` — пароль.
* `totp_code` — одноразовый код 2FA (обязателен для ролей `trader` / `admin`, если 2FA включена).

#### Ответ 200

```json
{
  "access_token": "<jwt>",
  "refresh_token": "<jwt>",
  "token_type": "bearer"
}
```

#### Ошибки

* `400` — неверный формат запроса.
* `401` — неправильный логин/пароль или 2FA-код.
* `403` — пользователь деактивирован (`is_active = false`).

---

### 3.2. POST `/auth/refresh`

Обновление `access_token` по `refresh_token`.

#### Тело запроса

```json
{
  "refresh_token": "<jwt>"
}
```

#### Ответ 200

Аналогичен `POST /auth/login`:

```json
{
  "access_token": "<jwt>",
  "refresh_token": "<jwt>",
  "token_type": "bearer"
}
```

#### Ошибки

* `401` — токен просрочен или отозван.
* `403` — пользователь заблокирован.

---

### 3.3. POST `/auth/logout`

Выход из сессии.

* **Роли**: любой авторизованный пользователь.

Тело запроса отсутствует. Используется `Authorization: Bearer <access_token>`.

#### Ответ

* `204 No Content` — успешный выход, текущий access/refresh токены помечены как недействительные (в т.ч. через blacklist при необходимости).

---

## 4. Пользователи

Все эндпоинты этого раздела доступны только для роли **admin**.

### 4.1. GET `/users`

Получение списка пользователей.

#### Ответ 200

```json
[
  {
    "id": "...",
    "email": "admin@example.com",
    "role": "admin",
    "is_active": true,
    "created_at": "2025-01-01T10:00:00Z",
    "last_login_at": "2025-01-10T12:00:00Z"
  }
]
```

---

### 4.2. POST `/users`

Создание нового пользователя.

#### Тело запроса

```json
{
  "email": "new.user@example.com",
  "role": "viewer"
}
```

* Пароль не задаётся сразу: создаётся запись с пустым `password_hash`, генерируется токен активации и отправляется ссылка активации на email.

#### Ответ 201

```json
{
  "id": "...",
  "email": "new.user@example.com",
  "role": "viewer",
  "is_active": true,
  "created_at": "2025-01-15T09:00:00Z",
  "last_login_at": null
}
```

---

### 4.3. PATCH `/users/{user_id}`

Изменение роли/статуса пользователя.

#### Тело запроса

```json
{
  "role": "trader",
  "is_active": true
}
```

Оба поля опциональны: можно менять только роль или только флаг активности.

#### Ответ 200

Возвращается обновлённый `User`.

---

## 5. Сигналы

### 5.1. GET `/signals`

Список активных сигналов.

* **Роли**: `viewer` / `trader` / `admin`.

#### Параметры запроса (query)

* `symbol` — фильтр по тикеру (например, `BTCUSDT`).
* `direction` — `long` / `short`.
* `min_probability` — минимальная вероятность (0..1).

Пример:

```http
GET /api/v1/signals?symbol=BTCUSDT&direction=long&min_probability=0.7
```

#### Ответ 200

```json
[
  {
    "id": "...",
    "created_at": "2025-01-10T12:34:56Z",
    "symbol": "BTCUSDT",
    "direction": "long",
    "entry_price": 60000.5,
    "stake_usd": 1000.0,
    "probability": 0.83,
    "strategy": "AVI-5",
    "strategy_version": "1.0.0"
  }
]
```

---

## 6. Позиции

### 6.1. GET `/positions`

Список открытых позиций текущего пользователя.

* **Роли**: `viewer` / `trader` / `admin`.

#### Ответ 200

```json
[
  {
    "id": "...",
    "signal_id": "...",
    "symbol": "BTCUSDT",
    "side": "long",
    "entry_price": 60000.5,
    "size_base": 0.05,
    "size_quote": 3000.0,
    "status": "open",
    "opened_at": "2025-01-10T12:35:10Z",
    "closed_at": null,
    "pnl_usd": null
  }
]
```

---

### 6.2. POST `/positions/{id}/close`

Ручное закрытие позиции.

* **Роли**: минимум `trader`.

Тело запроса отсутствует: закрывается вся позиция по указанному `id`.

#### Ответ 200

Возвращается обновлённый объект `Position` (со статусом `closing` или `closed` — в зависимости от модели исполнения).

#### Ошибки

* `404` — позиция не найдена.
* `403` — недостаточно прав.
* `409` — позиция уже в процессе закрытия или закрыта.

---

## 7. Конфигурация

### 7.1. GET `/config`

Чтение текущей конфигурации (read-only).

* **Роли**: `viewer` / `trader` / `admin`.

#### Ответ 200

```json
{
  "trading": {
    "max_stake": 100.0
  },
  "risk": {
    "max_concurrent": 5
  },
  "bybit": {
    "api_key": "BYBIT-XXXXXXXXXXXXXXXX"
  }
}
```

Значения берутся из `config/settings.yaml` + окружения, секреты (пароли/приватные ключи) не возвращаются.

---

### 7.2. PATCH `/config`

Частичное изменение конфигурации (только для администратора).

* **Роли**: `admin`.
* Все изменения логируются в `audit_trail` (тип действия, user_id, старое/новое значение).

#### Тело запроса

Передаётся фрагмент объекта конфигурации. Поля, которых нет в теле, не изменяются.

Пример:

```json
{
  "trading": {
    "max_stake": 200.0
  },
  "risk": {
    "max_concurrent": 10
  }
}
```

#### Ответ 200

Возвращается обновлённая конфигурация (как в `GET /config`).

---

## 8. Администрирование

### 8.1. POST `/admin/kill_switch`

Принудительное выключение торговли.

* **Роли**: `admin`.

#### Тело запроса

```json
{
  "reason": "Manual kill switch due to abnormal behavior"
}
```

* `reason` — человекочитаемое обоснование (логируется).

#### Ответ 200

```json
{
  "kill_switch_active": true
}
```

Флаг `kill_switch_active` также экспонируется в метриках/алертах.

---

### 8.2. GET `/admin/reconciliation/status`

Последние записи сверки состояния (reconciliation).

* **Роли**: `admin`.

#### Ответ 200

```json
[
  {
    "id": 123,
    "created_at": "2025-01-10T12:40:00Z",
    "severity": "warning",
    "description": "Bybit position mismatch for BTCUSDT",
    "details": {
      "local_size": 0.05,
      "exchange_size": 0.04
    }
  }
]
```

---

## 9. Health & Метрики

### 9.1. GET `/health`

Проверка состояния основных зависимостей: БД, Redis, соединения с биржей.

* Может быть открыт без аутентификации на уровне маршрутизации/ingress (решается инфраструктурой).

#### Ответ 200

```json
{
  "status": "ok",
  "components": {
    "db": "up",
    "redis": "up",
    "bybit_ws": "up",
    "bybit_rest": "up"
  }
}
```

* При частичной деградации возможен `status: "degraded"`.

#### Ответ 503

Если одна из критичных зависимостей недоступна.

---

### 9.2. GET `/metrics`

Prometheus-метрики.

* Контент-тип: `text/plain; version=0.0.4`.

Примеры метрик:

* latency API;
* количество ошибок интеграции с Bybit;
* состояние kill-switch;
* технические счётчики (WS-соединения, ретраи и т.д.).

---

## 10. Streaming (SSE)

### 10.1. GET `/stream`

Realtime-стрим сигналов и статусов позиций через **Server-Sent Events**.

* **Роли**: `viewer` / `trader` / `admin`.
* Требует заголовка `Authorization: Bearer <access_token>`.

#### Формат событий

Каждое событие имеет вид:

```text
event: signal
data: {"id": "...", "symbol": "BTCUSDT", "direction": "long", ...}

event: position
data: {"id": "...", "symbol": "BTCUSDT", "status": "open", ...}
```

Типы событий:

* `signal` — новый или обновлённый сигнал (модель как в `GET /signals`).
* `position` — обновление статуса позиции (модель как в `GET /positions`).

Соединение должно восстанавливаться клиентом при обрыве (с поддержкой `Last-Event-ID` при необходимости).

---

## 11. 2FA (TOTP)

### 11.1. POST `/auth/2fa/setup`

Инициация настройки 2FA для текущего пользователя.

* **Роли**: авторизованный пользователь (для `trader`/`admin` — рекомендуется/обязательно).

#### Ответ 200

```json
{
  "otpauth_uri": "otpauth://totp/BybitAlgo:user@example.com?secret=XXXX&period=30&issuer=BybitAlgo",
  "qr_svg": "<svg>...</svg>"
}
```

* `otpauth_uri` — URI для подключения в приложении (Google Authenticator и т.п.).
* `qr_svg` — SVG-представление QR-кода (может быть не реализовано и генерироваться на фронте).

---

### 11.2. POST `/auth/2fa/confirm`

Подтверждение включения 2FA.

#### Тело запроса

```json
{
  "code": "123456"
}
```

#### Ответ 204

При успешной проверке 2FA включается (`is_totp_enabled = true`).

---

### 11.3. POST `/auth/2fa/disable`

Отключение 2FA с дополнительной проверкой.

#### Тело запроса

```json
{
  "code": "123456"
}
```

#### Ответ 204

2FA отключена (`is_totp_enabled = false`), событие логируется в `audit_trail`.

---

## 12. API-ключи

### 12.1. GET `/api-keys`

Список API-ключей текущего пользователя.

* **Роли**:

  * `viewer` / `trader` — только свои ключи.
  * `admin` — может получать ключи любых пользователей через отдельные фильтры (зависит от реализации; базовый сценарий — только свои).

#### Ответ 200

```json
[
  {
    "id": "...",
    "label": "Main Bybit Account",
    "exchange": "bybit",
    "env": "mainnet",
    "permissions": {
      "trading": true,
      "read_only": false,
      "withdrawals": false
    },
    "created_at": "2025-01-01T11:00:00Z",
    "last_used_at": "2025-01-10T13:00:00Z",
    "is_active": true
  }
]
```

---

### 12.2. POST `/api-keys`

Создание нового API-ключа.

* **Роли**: обычно `admin` (см. матрицу доступа ниже).

#### Тело запроса

```json
{
  "label": "Main Bybit Account",
  "exchange": "bybit",
  "env": "mainnet",
  "api_key": "BYBIT-XXXX",
  "api_secret": "SECRET-XXXX",
  "permissions": {
    "trading": true,
    "read_only": false,
    "withdrawals": false
  }
}
```

Backend:

1. Валидирует ключи через тестовый запрос к Bybit.
2. Шифрует `api_secret` через Vault Transit.
3. Сохраняет запись в `api_keys` (в БД хранится только ciphertext и метаданные).

#### Ответ 201

Возвращается созданный объект `ApiKey` **без** секрета:

```json
{
  "id": "...",
  "label": "Main Bybit Account",
  "exchange": "bybit",
  "env": "mainnet",
  "permissions": {
    "trading": true,
    "read_only": false,
    "withdrawals": false
  },
  "created_at": "2025-01-01T11:00:00Z",
  "last_used_at": null,
  "is_active": true
}
```

---

### 12.3. DELETE `/api-keys/{id}`

Деактивация/удаление API-ключа.

* **Роли**: `admin`.

#### Ответ 204

Ключ помечен как неактивный (`is_active = false`), физическое удаление опционально.

---

## 13. Матрица доступа к эндпоинтам

Таблица основных эндпоинтов и минимальных ролей для доступа.

| Эндпоинт                | Метод | viewer | trader | admin |
| ----------------------- | ----- | ------ | ------ | ----- |
| `/signals`              | GET   | ✅      | ✅      | ✅     |
| `/positions`            | GET   | ✅      | ✅      | ✅     |
| `/positions/{id}/close` | POST  | ❌      | ✅      | ✅     |
| `/config`               | GET   | ✅      | ✅      | ✅     |
| `/config`               | PATCH | ❌      | ❌      | ✅     |
| `/admin/kill_switch`    | POST  | ❌      | ❌      | ✅     |
| `/users`                | GET   | ❌      | ❌      | ✅     |
| `/users`                | POST  | ❌      | ❌      | ✅     |
| `/api-keys`             | GET   | ✅*     | ✅*     | ✅     |
| `/api-keys`             | POST  | ❌      | ❌      | ✅     |

*только свои ключи.

Остальные эндпоинты (аутентификация, health/metrics, streaming, 2FA) следуют правилам, описанным выше в соответствующих разделах.

