# Bybit Algo-Grid System / стратегия AVI-5

Система реализует полуавтоматическую **grid-/swing-стратегию** для деривативов Bybit (AVI-5):

- **На вход**: рыночные данные (свечи 5m/15m, стакан L2/L50, trade-tape), открытые позиции и параметры стратегии.
- **На выход**: торговые сигналы и поток ордеров в соответствии с риск-параметрами заказчика.
- Система **не** является HFT — фокус на корректности логики, наблюдаемости и управляемом риске.
- Рабочий режим: **24/7**, с механизмами reconciliation и kill-switch для безопасной деградации.

---

## Архитектура

Проект построен как асинхронный backend на FastAPI с разделением на слои.

Высокоуровневая схема:

- **User UI (Vanilla JS)**  
  - Отдаётся тем же FastAPI-приложением по пути `/ui` (через `StaticFiles`).  
  - Общается с backend по REST (`/api/v1/...`) и SSE-стриму (`/stream/...`).

- **FastAPI API-gateway**
  - Эндпоинты: `/signals`, `/positions`, `/config`, `/health`, `/metrics` и др.
  - Подключает middleware аутентификации и RBAC.
  - Отвечает за валидацию входных данных и трансляцию запросов в core-сервисы.

- **Core business layer**
  - `StrategyEngine (AVI-5)` — реализация логики сеточной/свинг-стратегии.
  - `RiskManager` — лимиты, риск-параметры, контроль плечей и max-stake.
  - `OrderManager` — работа с ордерами (в т.ч. ручное вмешательство).
  - `IndicatorCalc` — VWAP, ATR, EMA, Donchian, другие индикаторы.
  - `PositionTracker`, `FillTracker` — отслеживание позиций и исполнения, учёт проскальзывания.

- **Data & Integration layer**
  - `BybitWSClient` — подписка на kline/объём/ордербук.
  - `BybitRESTClient` — ордера, снимки позиций и балансов.
  - `Redis` (streams/pub-sub) — очереди событий, промежуточные буферы.
  - `PostgreSQL (TimescaleDB)` — долговременное хранение:
    - `klines_*`, `signals`, `positions`, `slippage_log`, `orderbook_l50_log`, `reconciliation_log` и др.

- **Monitoring & observability**
  - Метрики Prometheus → дашборды Grafana → оповещения Alertmanager (в т.ч. kill-switch).
  - Структурированные JSON-логи (`structlog`) в `logs/` с ротацией.

Подробнее по модулям и контрактам см. `docs/api.md` и внутренние описания в `project_overview.md`.

---

## Структура проекта (сверху вниз)

```text
bybit-algo-grid/
├── config/
│   ├── settings.yaml            # Публичная конфигурация (dev/sandbox)
│   ├── secrets.env.example      # Пример секретов для локального запуска
│   └── schema.py                # Pydantic-схемы конфигурации
├── docs/
│   ├── api.md                   # OpenAPI / REST контракты
│   ├── deployment.md            # Инструкция по деплою
│   ├── disaster_recovery.md     # DR-план
│   ├── risk_disclaimer.md       # Юридический дисклеймер
│   └── backup_strategy.md       # Стратегия резервного копирования
├── docker/
│   ├── Dockerfile               # Мульти-стейдж сборка backend
│   ├── docker-compose.yml       # Dev-стек: PostgreSQL, Redis, app
│   └── docker-compose.prod.yml  # Prod-стек: Traefik, реплики и пр.
├── frontend/                    # Статический UI (Vanilla JS + Tailwind)
├── src/                         # Основной backend (FastAPI, бизнес-логика)
├── scripts/                     # Утилиты: миграции, backup/restore и т.п.
├── tests/                       # Unit / integration тесты
├── README.md                    # Этот файл
└── alembic.ini, alembic/        # Миграции БД (Alembic)
````

---

## Быстрый старт (локальная разработка)

### 1. Требования

* Python **3.12+**
* [Poetry](https://python-poetry.org/) (менеджер зависимостей)
* Docker + Docker Compose
* Доступ к тестовой среде Bybit (testnet) и отдельной тестовой БД PostgreSQL (если используете не docker-compose).

### 2. Установка Poetry (если ещё не установлен)

```bash
pip install poetry
```

Проверьте:

```bash
poetry --version
```

### 3. Установка зависимостей

```bash
# в корне репозитория
poetry install
```

Poetry подтянет зависимости backend (FastAPI, asyncpg, redis, pydantic, structlog и т.д.) и dev-зависимости (pytest, mypy, ruff).

---

### 4. Конфигурация (dev)

1. Создайте файл с секретами для локального запуска на основе примера:

   ```bash
   cp config/secrets.env.example config/secrets.env
   ```

2. Заполните `config/secrets.env` тестовыми значениями:

   ```env
   BYBIT_API_KEY=your_testnet_key
   BYBIT_SECRET=your_testnet_secret
   JWT_SECRET=some_dev_jwt_secret
   ```

3. Отредактируйте при необходимости `config/settings.yaml`:

   * `trading.max_stake`
   * `risk.max_concurrent`
   * `bybit.*` параметры подключения и др.

> Важное ограничение: `config/secrets.env` и любые реальные ключи
> **никогда не коммитятся** в репозиторий.

---

### 5. Запуск dev-стека через Docker Compose

Из корня репозитория:

```bash
docker-compose -f docker/docker-compose.yml up --build
```

В dev-стеке поднимаются:

* `db` — PostgreSQL (порт `5432`);
* `redis` — Redis (порт `6379`);
* `app` — backend (порт `8000`), собранный из `docker/Dockerfile`.

Первый запуск может занять время из-за установки и сборки зависимостей.

---

### 6. Миграции БД

Миграции описаны в `alembic/` и могут запускаться как напрямую через Alembic, так и через утилиту `scripts/migrate.py`.

Базовый вариант (из Poetry-окружения):

```bash
alembic upgrade head
```

или эквивалентно через helper-скрипт:

```bash
poetry run python -m scripts.migrate
```

После этого таблицы (`signals`, `positions`, `slippage_log`, `klines_*` и др.) будут созданы согласно схеме.

---

### 7. Запуск приложения (без Docker, локально)

Если вы хотите запустить только backend (при уже поднятых PostgreSQL/Redis):

```bash
poetry run uvicorn src.main:app --reload
```

По умолчанию:

* API будет доступно на `http://localhost:8000/api/v1/...`;
* UI — на `http://localhost:8000/ui`;
* OpenAPI/Swagger — на `http://localhost:8000/docs`;
* метрики Prometheus — на `http://localhost:8000/metrics`.

Адреса и порты уточняются конфигурацией в `config/settings.yaml` и параметрами запуска.

---

## Dev vs Prod: работа с секретами

Разница между окружениями принципиальная и *явно* описана здесь.

### Dev / Test

* Допускается использование `.env` / `config/secrets.env`:

  * файл создаётся локально;
  * значения — тестовые (Bybit testnet, локальные JWT-секреты);
  * файл **не коммитится** и игнорируется `.gitignore`.
* Примеры значений приведены в `config/secrets.env.example`.
* Такое хранение допустимо только для локальной разработки и тестовых сред.

### Prod

* Секреты (API-ключи Bybit, TOTP-секреты и др.) **не хранятся в открытом виде** ни в БД, ни в файловой системе.
* Используется **HashiCorp Vault** с Transit-движком:

  * БД хранит только ciphertext и минимальный набор метаданных (`api_keys`).
  * Операции шифрования/дешифрования выполняются через Vault Transit API.
  * Каждая операция логируется в `audit_trail` и подлежит аудиту.
* Prod-окружение поднимается отдельным стеком (см. `docker/docker-compose.prod.yml`, `docs/deployment.md`), где приложение получает секреты уже из внешнего Vault, а не из `.env`.

---

## Полезные команды (Poetry scripts)

Некоторые команды вынесены в `pyproject.toml`:

* Запуск миграций:

  ```bash
  poetry run migrate
  # под капотом: alembic upgrade head
  ```

* Тесты:

  ```bash
  poetry run test
  # под капотом: pytest
  ```

* Dev-стек (если настроен alias):

  ```bash
  poetry run dev
  # под капотом: docker-compose up
  ```

Конкретные имена и команды см. в `pyproject.toml`.

---

## Дополнительная документация

* **API и контракты** — `docs/api.md`
* **Деплой и операции** — `docs/deployment.md`
* **Disaster recovery** — `docs/disaster_recovery.md`
* **Резервное копирование** — `docs/backup_strategy.md`
* **Юридический дисклеймер** — `docs/risk_disclaimer.md`

Эти документы описывают продакшн-процессы, SLA, процедуры восстановления и риск-ограничения и дополняют краткий обзор из данного README.

```
