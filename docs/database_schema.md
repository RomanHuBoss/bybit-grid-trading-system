# Схема базы данных — Bybit Algo-Grid / AVI-5

Документ описывает логическую структуру БД системы **Bybit Algo-Grid / стратегия AVI-5**:

- какие таблицы используются;
- как они связаны между собой;
- какие у них ключевые поля и индексы;
- какие Alembic-миграции создают и изменяют эту схему.

Фактические DDL живут в Alembic-миграциях (каталог `alembic/`), а здесь — согласованный
человеко-читаемый контракт.

---

## 1. Обзор и ER-диаграмма

### 1.1. Основные сущности

Ключевые таблицы:

- `users` — пользователи и их роли (viewer / trader / admin), 2FA и статусы активности.
- `api_keys` — API-ключи для биржи (Bybit): ciphertext секрета, метаданные и права.
- `signals` — торговые сигналы стратегии AVI-5.
- `positions` — открытые/закрытые позиции, рождающиеся из сигналов.
- `audit_trail` — аудит действий пользователей и системных операций.
- `reconciliation_log` — лог результатов сверок состояния с биржей.
- `order_rejections` — лог отклонённых заявок/операций.
- time-series таблицы рыночных данных:
  - `klines_5m` (и другие интервалы) — свечи;
  - `orderbook_l50_log` — снимки стакана;
  - `slippage_log` — измерения проскальзывания.

### 1.2. Связи (ER-схема в текстовом виде)

Основные FK-отношения:

- `users (1)` ──< `api_keys (N)`
  - `api_keys.user_id` → `users.user_id`.
- `users (1)` ──< `audit_trail (N)`
  - `audit_trail.user_id` → `users.user_id`.
- `users (1)` ──< `order_rejections (N)` (опционально, user_id может быть NULL)
  - `order_rejections.user_id` → `users.user_id`.
- `signals (1)` ──< `positions (N)`
  - `positions.signal_id` → `signals.id` (может быть NULL для полностью ручных позиций).
- `signals (1)` ──< `order_rejections (N)` (опционально)
  - `order_rejections.signal_id` → `signals.id`.
- `positions (1)` ──< `slippage_log (N)` (на уровне логики)
  - в `slippage_log` хранится измерение проскальзывания для входа/выхода по позиции.

Для time-series таблиц (свечи, стакан, slippage):

- ключевая комбинация: `(ts, symbol)` — используется как первичный/уникальный ключ или ключ
  hypertable в TimescaleDB;
- FK в основном не используются (это технические данные, не критичные для бизнес-инвариантов).

---

## 2. Общие принципы схемы

- База данных: **PostgreSQL 15+** (с расширением **TimescaleDB** для time-series).  
- Все главные таблицы имеют:
  - явный `PRIMARY KEY`;
  - индексы по критичным полям (`created_at`, `symbol`, `user_id`, `status` и т.п.);
  - внешние ключи для ссылок на `users`, `signals`, `positions` там, где это требуется.  
- Любые изменения схемы проходят через **Alembic**; миграции описаны в разделе ниже.

---

## 3. Описание таблиц

### 3.1. Таблица `users`

Назначение: хранит базовую информацию о пользователях, их ролях и 2FA-статусе.

DDL-эталон:

```sql
CREATE TABLE users (
    user_id UUID PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'trader', 'admin')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    totp_secret TEXT,
    is_totp_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    password_reset_token TEXT,
    password_reset_expires_at TIMESTAMPTZ
);
````

Ключевые поля и индексы:

* `user_id` — PK, используется как `sub` в JWT.
* `email` — уникальный логин, индексируется уникальным индексом.
* `role` — значение из фиксированного набора (`viewer`, `trader`, `admin`).
* `is_active` — флаг блокировки.
* `created_at`, `last_login_at` — используются для аудита и аналитики.

Рекомендуемые индексы:

* `UNIQUE (email)` — уже задан.
* (опционально) `INDEX (created_at)` — для выборок по дате создания.

---

### 3.2. Таблица `api_keys`

Назначение: хранит Bybit API-ключи пользователей (в зашифрованном виде) и их метаданные.

DDL-эталон:

```sql
CREATE TABLE api_keys (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    exchange TEXT NOT NULL CHECK (exchange = 'bybit'),
    label TEXT NOT NULL,
    key_id TEXT NOT NULL,
    key_ciphertext BYTEA NOT NULL,
    permissions JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE UNIQUE INDEX idx_api_keys_user_label
    ON api_keys (user_id, label);

CREATE INDEX idx_api_keys_user_active
    ON api_keys (user_id, is_active);
```

Основные моменты:

* В проде в таблице хранится **только ciphertext** секрета, шифрование через Vault Transit.
* `exchange` сейчас фиксированно `'bybit'` (но оставлен отдельным полем на будущее).
* `permissions` — JSONB со структурой прав (торговля, только чтение, вывод и т.п.).

---

### 3.3. Таблица `signals`

Назначение: хранит торговые сигналы стратегии AVI-5.

Схема собирает воедино общие DDL и детализацию по миграциям:

Основные поля:

* `id UUID PRIMARY KEY` — идентификатор сигнала.
* `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` — время генерации сигнала.
* `symbol TEXT NOT NULL` — тикер (например, `BTCUSDT`).
* `side TEXT NOT NULL CHECK (side IN ('long', 'short'))`
  В API это поле экспонируется как `direction`; на уровне БД используется имя `side` для унификации с позициями.
* `entry_price NUMERIC(18,8) NOT NULL` — целевая цена входа.
* `stake_usd NUMERIC(18,2) NOT NULL` — рекомендуемый размер позиции в USD (из раздела про миграции).
* `tp1_price NUMERIC(18,8) NOT NULL` — первый таргет (TP1).
* `tp2_price NUMERIC(18,8)` — второй таргет (опционален).
* `tp3_price NUMERIC(18,8)` — третий таргет (опционален).
* `sl_price NUMERIC(18,8) NOT NULL` — стоп-лосс.
* `risk_r NUMERIC(6,3) NOT NULL` — риск в R-единицах (отношение reward/risk).
* `probability NUMERIC(4,3) NOT NULL` — вероятность срабатывания (0..1).
* `strategy TEXT NOT NULL` — имя стратегии (например, `AVI-5`).
* `strategy_version VARCHAR(20) NOT NULL` — версия стратегии.
* `queued_until TIMESTAMPTZ` — до какого момента сигнал актуален для постановки.
* `error_code INTEGER` — код ошибки при обработке (если была проблема).
* `error_message TEXT` — подробности ошибки.

Индексы (рекомендуемые):

* `(symbol, created_at)` — выборка сигналов по инструменту и времени.
* (опционально) `(queued_until)` — поиск просроченных/активных сигналов.

> Примечание: в разных разделах спецификации поле именуется `side` или `direction`.
> Для БД в качестве столбца используется `side`, в API — `direction`.
> Это зафиксировано, чтобы сохранить совместимость и не тащить лишние rename-миграции.

---

### 3.4. Таблица `positions`

Назначение: хранит открытые и закрытые позиции, связанные с сигналами.

DDL-эталон (собран из DDL-фрагмента):

```sql
CREATE TABLE positions (
    id UUID PRIMARY KEY,
    signal_id UUID REFERENCES signals(id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
    entry_price NUMERIC(18,8) NOT NULL,
    size_base NUMERIC(30,10) NOT NULL,
    size_quote NUMERIC(30,10) NOT NULL,
    executed_size_base NUMERIC(30,10) NOT NULL DEFAULT 0,
    fill_ratio NUMERIC(6,4) NOT NULL DEFAULT 0,
    tp1_price NUMERIC(18,8),
    tp2_price NUMERIC(18,8),
    tp3_price NUMERIC(18,8),
    sl_price NUMERIC(18,8),
    status TEXT NOT NULL CHECK (status IN ('open','closing','closed','error')),
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    pnl_usd NUMERIC(20,4),
    slippage_entry_bps NUMERIC(10,4),
    slippage_exit_bps NUMERIC(10,4)
);

CREATE INDEX idx_positions_symbol_status
    ON positions (symbol, status);
```

Основные поля:

* `signal_id` — связь с первичным сигналом (опциональна для чисто ручных позиций).
* `size_base` / `size_quote` — размер позиции в базовой/квотируемой валюте.
* `executed_size_base`, `fill_ratio` — прогресс исполнения.
* `tp*_price`, `sl_price` — фактические уровни выхода.
* `status` — состояние жизненного цикла позиции.
* `opened_at`, `closed_at` — временные метки.
* `pnl_usd` — итоговый PnL по позиции.
* `slippage_entry_bps` / `slippage_exit_bps` — измеренное проскальзывание на входе/выходе.

Индексы:

* `idx_positions_symbol_status (symbol, status)` — быстрый поиск открытых/ошибочных позиций по тикеру.
* Рекомендуется добавить индекс по `opened_at` для репортинга.

---

### 3.5. Таблица `audit_trail`

Назначение: журналирует действия пользователей и важные системные операции.

DDL-эталон:

```sql
CREATE TABLE audit_trail (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID,
    action TEXT NOT NULL,
    details JSONB
);

CREATE INDEX idx_audit_trail_user
    ON audit_trail (user_id, created_at);
```

Поля:

* `user_id` — кто совершил действие (может быть NULL для системных действий).
* `action` — тип (`login`, `place_order`, `close_position`, `change_config`, `bybit_key_used` и т.п.).
* `details` — произвольные детали (symbol, size, старое/новое значение).

---

### 3.6. Таблица `reconciliation_log`

Назначение: хранит результаты сверок состояния позиций/балансов с биржей.

DDL-эталон:

```sql
CREATE TABLE reconciliation_log (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    severity TEXT CHECK (severity IN ('info','warning','critical')),
    description TEXT NOT NULL,
    details JSONB
);
```

Поля:

* `severity` — уровень важности (`info` / `warning` / `critical`).
* `description` — текстовое описание найденного расхождения.
* `details` — JSONB с деталями: ID позиций, расхождение по количеству/цене и т.д.

Рекомендуемый индекс:

* `(created_at)` или `(severity, created_at)` — быстрый просмотр последних критичных записей.

---

### 3.7. Таблица `order_rejections`

Назначение: лог всех отклонённых ордеров/операций.

DDL-эталон:

```sql
CREATE TABLE order_rejections (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID,
    signal_id UUID,
    reason TEXT NOT NULL,
    bybit_ret_code INTEGER,
    bybit_ret_msg TEXT
);
```

Поля:

* `user_id` — для какого пользователя операция выполнялась (может быть NULL для системных).
* `signal_id` — по какому сигналу пытались открыть позицию (если применимо).
* `reason` — человекочитаемое объяснение (лимит риска, kill-switch, ошибка бизнес-логики).
* `bybit_ret_code` / `bybit_ret_msg` — код/сообщение ошибки, если отказ пришёл с биржи.

Рекомендуемые индексы:

* `(created_at)` — общий просмотр истории.
* `(user_id, created_at)` — поиск по конкретному пользователю.

---

### 3.8. Таблица `slippage_log` (time-series)

Назначение: хранит измерения проскальзывания по входу/выходу из позиций.

Общие принципы:

* реализуется как **TimescaleDB hypertable** (часть time-series слоя);
* ключ: `(ts, symbol)` или `(created_at, symbol)` в зависимости от выбранного имени временного поля.

Рекомендуемый минимальный набор полей:

* `id BIGSERIAL PRIMARY KEY`
* `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` — момент фиксации slippage.
* `position_id UUID` — ссылка на позицию (если хранится).
* `symbol TEXT NOT NULL`.
* `side TEXT CHECK (side IN ('long','short'))`.
* `kind TEXT CHECK (kind IN ('entry','exit'))` — для входа и выхода.
* `slippage_bps NUMERIC(10,4) NOT NULL` — рассчитанное значение
  `slippage_bps = (avg_fill_price / requested_price - 1) * 10000`.
* `details JSONB` — метаданные: `requested_price`, `avg_fill_price`, depth/ATR-флаги и т.п.

> Конкретное DDL фиксируется в миграции `003_create_slippage_log_table.py`.
> Здесь описан целевой контракт: есть явное поле `slippage_bps` и возможная связь с `positions`.

---

### 3.9. Таблицы рыночных данных (TimescaleDB hypertables)

Назначение: хранить исторические рыночные данные для аналитики и расчёта индикаторов.

Перечень:

* `klines_5m` (и другие интервалы по аналогичной схеме) — OHLCV-свечи.
* `orderbook_l50_log` — снимки стакана L50.
* часть измерений по slippage (`slippage_log`) также относится к time-series.

Общие принципы:

* реализуются как **hypertables TimescaleDB** с ключом по времени и `symbol`;
* содержат типовой набор полей:

  * `ts TIMESTAMPTZ NOT NULL` — время бара/замера;
  * `symbol TEXT NOT NULL`;
  * для свечей: `open`, `high`, `low`, `close`, `volume`;
  * для стакана: агрегированные значения по bid/ask уровням;
* детальное DDL конкретных таблиц закрепляется в соответствующих Alembic-миграциях
  (например, `004_create_timescale_hypertable_klines.py`).

---

## 4. Alembic-миграции

Все изменения схемы БД проходят через Alembic; для **каждой логически цельной задачи** создаётся
отдельный `revision`. Ручные изменения схемы в продакшене запрещены.

### 4.1. Ключевые миграции

Из спецификации:

| Файл миграции                               | Назначение                                                |
| ------------------------------------------- | --------------------------------------------------------- |
| `001_create_signals_table.py`               | Создание базовой таблицы `signals`.                       |
| `002_create_positions_table.py`             | Создание таблицы `positions` и FK на `signals`.           |
| `003_create_slippage_log_table.py`          | Создание таблицы `slippage_log` для измерения slippage.   |
| `004_create_timescale_hypertable_klines.py` | Инициализация hypertable для `klines_5m` и родственников. |

Дополнительно (по мере появления в проекте) сюда должны быть добавлены миграции,
создающие/меняющие:

* таблицы `users`, `api_keys`, `audit_trail`, `reconciliation_log`, `order_rejections`;
* другие time-series таблицы (например, для `orderbook_l50_log`);
* структурные изменения (добавление TP-уровней, полей риска и т.п.).

Формат для дальнейшего пополнения:

```markdown
| `<revision_id>_<short_name>.py` | Краткое описание изменений (какие таблицы и почему). |
```

---

## 5. Согласованность с DR/backup

* Стратегия резервного копирования (`docs/backup_strategy.md`) опирается на то, что:

  * все критичные таблицы перечислены и задокументированы здесь;
  * backup/restore рассматриваются на уровне всей БД (включая time-series).
* DR-план (`docs/disaster_recovery.md`) использует список таблиц для проверки целостности
  после восстановления (наличие `users`, `signals`, `positions`, `api_keys`, `audit_trail`,
  `reconciliation_log`, `order_rejections` и корректность их данных).

При изменении схемы (новые таблицы, новые связи) необходимо:

1. Обновить Alembic-миграции.
2. Задокументировать изменения в этом файле.
3. Пересмотреть backup/DR-процедуры при необходимости.
