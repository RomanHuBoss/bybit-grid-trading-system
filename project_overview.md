## 1. РЕЗЮМЕ ПОНИМАНИЯ СИСТЕМЫ

### Назначение системы

Система реализует полуавтоматическую grid-/swing-стратегию для деривативов Bybit:

- На вход: рыночные данные (свечи 5m, стакан L2/L50, trade-tape - лента сделок), текущие открытые позиции и параметры стратегии.
- На выход: торговые сигналы и поток ордеров, соответствующий риск-параметрам заказчика.
- Система **не** является high-frequency трейдером и не конкурирует по latency; основной фокус — корректность логики, наблюдаемость и управляемый риск.

### Режим работы

- 24/7, непрерывная работа.
- В случае падения отдельного компонента допускается кратковременная деградация (в пределах RTO/RPO), но:
  - Стратегия **не должна** самопроизвольно увеличивать риск.
  - Состояние позиций в БД и на бирже обязано приходить к консистентности через механизм reconciliation.

### Таймфреймы и источники данных

- Основной рабочий таймфрейм — **5m свечи**.
- Источники:
  - Bybit REST (исторические свечи, справочники, one-shot данные).
  - Bybit WebSocket (live-данные: `kline`, `orderbook`, `user.order`).
- Вся рыночная информация сохраняется в PostgreSQL/TimescaleDB для последующей аналитики и валидации.

### Управление рисками

- Риск измеряется в R-мультипликаторах (1R — фиксированный риск на сделку).
- Ограничения:
  - MAX_CONCURRENT_POSITIONS.
  - MAX_TOTAL_RISK_R (суммарный риск по всем открытым позициям).
  - Per-base лимиты (например, отдельно по BTC, ETH).
- Введены:
  - Защита от чрезмерной активности (anti-churn) (блокировка повторного входа по символу на 15 минут после выхода).
  - Kill-switch условия (например, суммарная дневная просадка > X R).

### Режимы работы

- **Simulation / Backtest** — оффлайн-режим на исторических данных.
- **Paper-trading** — торговля на тестовой среде / демо-аккаунте.
- **Production** — торговля на боевом аккаунте Bybit под контролем риск-лимитов.

Переключение среды задаётся в конфигурации (`settings.yaml`) и через переменные окружения.

### Технический стек

- Backend: Python 3.11+, FastAPI, asyncpg, redis-py, structlog.
- DB: PostgreSQL 15+ с расширением TimescaleDB для time-series.
- Message-layer / кеш: Redis 7+.
- CI/CD: GitHub Actions, Docker, (Kubernetes / Docker-compose — в зависимости от окружения).
- Monitoring: Prometheus + Grafana, системные логи (JSONL), alerting через Alertmanager.

### SLA и нефункциональные требования

**Доступность и отказоустойчивость**

- Целевой уровень доступности API/UI: **≥ 99.5%** в месяц.
- RTO (Recovery Time Objective) = **15 минут**.
- RPO (Recovery Point Objective) = **5 минут** (для торговых данных и позиций).

**Производительность**

- p95 latency REST-эндпоинтов (чтение сигналов, позиций) — не более **500ms**.
- Публикация нового сигнала в UI (от момента генерации до отображения) — p95 ≤ **2 секунды**.
- Обработка fill-event от Bybit WebSocket до обновления позиции в БД — p95 ≤ **1 секунда**.
- Генерация сигналов по каждому символу на 5m-баре должна укладываться ≤ **100ms** на символ при целевом количестве инструментов.

**Масштабируемость**

- Горизонтальное масштабирование backend-инстансов по read-нагрузке (UI, API).
- Возможность вынесения ingestion-воркеров и risk-engine в отдельные процессы/подсистемы.
- Резерв по CPU/RAM в продакшене не менее 30%.

**Наблюдаемость**

- Стандартизированные метрики (Prometheus metrics) для ключевых частей:
  - Win-rate, Profit-factor, MaxDD.
  - Latency критичных операций (signal generation, order placement, reconciliation).
  - Количество ошибок Bybit API по типам.
- Логирование в JSONL с обогащением контекстом (request_id, user_id, symbol, correlation_id).

## 1.1. ГЛОССАРИЙ

| Термин        | Расшифровка                                                                      |
|---------------|----------------------------------------------------------------------------------|
| TP1           | Take Profit 1 — первый уровень фиксации прибыли                                  |
| SL            | Stop Loss — уровень стоп-лосса                                                   |
| BE            | Break-Even — перенос стопа в безубыток                                           |
| UPnL          | Unrealized Profit and Loss — нереализованная прибыль/убыток                      |
| R             | Risk — фиксированный риск на сделку; например, при риске $25 на сделку 1R = $25. |
| Profit Factor | Отношение суммы прибылей к сумме убытков                                         |
| AVI-5         | Adaptive VWAP-Imbalance — кастомная стратегия на основе дисбаланса VWAP          |
| SSE           | Server-Sent Events — технология потоковой передачи данных в UI                   |
| WS            | WebSocket                                                                        |
| REST          | Representational State Transfer (архитектурный стиль для API)                    |
| U/D/U         | Update/Delete/Update (последовательность событий в WebSocket)                    |
| Gap-detection | Обнаружение разрывов в последовательности данных                                 |
| Anti-churn    | Защита от чрезмерной активности/перегрузки торговых операций                     |
| Fill-ratio    | Коэффициент исполнения ордера                                                    |
| Slippage      | Проскальзывание (разница между ожидаемой и фактической ценой исполнения)         |

---

## 2. МОДЕЛЬ ПОЛЬЗОВАТЕЛЕЙ, АУТЕНТИФИКАЦИИ И API-КЛЮЧЕЙ

### 2.1. Модель развертывания и tenancy

* Система проектируется как **single-tenant** инсталляция на один юридический субъект (один клиент / одна компания).
* Много пользователей (операторов, аналитиков, администраторов) внутри одного tenant:

  * Все пользователи живут в одной таблице `users`.
  * В текущей версии **нет** разделения на несколько организаций; при необходимости в будущем добавляется поле `tenant_id` и соответствующая фильтрация.

### 2.2. Роли и права

Ролевая модель централизована в модуле `src/auth/rbac.py` и отражена в таблице `users.role`:

* **viewer**

  * Только чтение: сигналы, открытые/закрытые позиции, агрегированные метрики.
  * Нет доступа к изменению конфигурации, нативной торговле, API-ключам.
* **trader**

  * Все права viewer.
  * Возможность включать/выключать торговлю, управлять позициями (ручное закрытие), запускать калибровку.
* **admin**

  * Все права trader.
  * Управление пользователями (`users`), ролями, API-ключами.
  * Изменение risk-конфигурации (`settings.yaml` / UI), запуск migration, перезапуск сервисов (там, где это делегировано через UI).

Детальная матрица «role → endpoint» описана:

* в коде `src/auth/rbac.py` через утилиту `require_role(*roles)`;
* в документации `docs/api.md` отдельной таблицей.

### 2.3. Жизненный цикл пользователя

1. **Создание пользователя**

   * Создать нового пользователя может только **admin**.
   * Поток:

     1. Admin в UI вводит email, роль.
     2. Backend создаёт запись в `users` со статусом `is_active = true` и пустым `password_hash`.
     3. Генерируется одноразовый токен активации, записывается в `password_reset_token` + `password_reset_expires_at` (например, +48 часов).
     4. На email отправляется ссылка вида:
        `https://<host>/auth/activate?token=...`.

2. **Активация аккаунта**

   * Пользователь переходит по ссылке, задаёт пароль + настраивает 2FA (по политике — см. 2.6).
   * После успешной установки пароля одноразовый токен удаляется.

3. **Аутентификация / вход**

   * По email + паролю с обязательной проверкой 2FA, если включена.
   * При успешном входе:

     * генерируются `access` и `refresh` JWT-токены;
     * обновляется `last_login_at`;
     * в `audit_trail` пишется событие `login`.

4. **Смена пароля**

   * Поток «забыли пароль»:

     * пользователь вводит email;
     * система отправляет одноразовую ссылку на reset (аналогично активации);
     * после смены пароля токен инвалидируется.
   * Поток «смена пароля» из профиля:

     * текущий пароль + новый пароль;
     * при успехе пишется `audit_trail` событие `password_changed`.

5. **Деактивация / удаление**

   * `is_active = false` блокирует вход и получение новых токенов.
   * Для целей GDPR и аудита:

     * запись в `users` может быть «обезличена» (анонимизирована) по запросу субъекта данных;
     * `audit_trail` сохраняется, но user_id может быть заменён на анонимный идентификатор (см. §8.2).

### 2.4. Аутентификация и JWT

Подсистема JWT описана в `src/auth/jwt_manager.py` и `src/auth/middleware.py`.

* **Типы токенов**

  * `access` — живёт 15 минут, используется для доступа к API.
  * `refresh` — живёт 7 дней, используется только для обновления пары токенов.
* **Содержание payload**:

  * `sub` — `user_id` (UUID).
  * `role` — роль пользователя.
  * `iat`, `exp` — время выпуска и истечения.
  * `jti` — уникальный идентификатор токена (для опционального blacklist).
* **Безопасность**

  * Секрет подписи (`JWT_SECRET`) хранится **не в .env в продакшене**, а в KMS/Vault (см. §3/Q-02 и §8.1).
  * Для продакшена рекомендуется алгоритм `RS256` или `ES256` с хранением приватного ключа в HSM / Vault; в MVP допускается `HS256` с надёжным секретом.

Эндпоинты аутентификации (описаны в `docs/api.md`):

* `POST /auth/login` — email + пароль + (опционально) 2FA-код → `access`/`refresh`.
* `POST /auth/refresh` — принимает `refresh` токен → новые `access`/`refresh`.
* `POST /auth/logout` — инвалидирует текущие токены (через blacklist, при включённом режиме).

### 2.5. Пароли и восстановление доступа

**Хранение**

* Пароли **никогда** не хранятся в открытом виде.
* Используется алгоритм `Argon2id` или `bcrypt` с настройками:

  * Argon2: достаточное количество памяти и итераций согласно актуальным рекомендациям OWASP.
* В таблице `users`:

  * `password_hash TEXT NOT NULL` — результат `Argon2id/bcrypt`.
  * `password_reset_token TEXT` — одноразовый токен смены пароля (nullable).
  * `password_reset_expires_at TIMESTAMPTZ` — срок действия токена.

**Политика паролей**

* Минимальная длина — 12 символов.
* Обязательное сочетание букв/цифр; использование списков популярных паролей для блокировки слабых вариантов (по возможности).

### 2.6. Двухфакторная аутентификация (2FA)

2FA реализуется как TOTP (Time-based One-Time Password, Google Authenticator-совместимый):

* В таблицу `users` добавляются поля:

  * `totp_secret TEXT` — зашифрованный секрет TOTP (шифруется через тот же KMS, что и API-ключи).
  * `is_totp_enabled BOOLEAN NOT NULL DEFAULT FALSE`.
* Поток включения 2FA:

  1. Пользователь заходит в профиль.
  2. Backend генерирует секрет и QR-код (URI `otpauth://`).
  3. Пользователь сканирует QR в приложении и вводит текущий код.
  4. При успешной проверке `is_totp_enabled = true`, секрет сохраняется.
* Политика:

  * Для ролей `admin` и `trader` 2FA **обязательно**.
  * Для `viewer` — опционально, но рекомендуется.
* Вход без 2FA при роли `admin`/`trader` запрещён.

### 2.7. Управление API-ключами (Bybit keys)

API-ключи клиентов — один из наиболее критичных компонентов, поэтому:

**2.7.1. Хранение**

* В продакшене **нигде** не хранятся открытые `BYBIT_API_KEY`/`BYBIT_API_SECRET`:

  * секреты шифруются с помощью KMS (`HashiCorp Vault Transit`) перед записью в БД;
  * в таблице `api_keys` хранится только ciphertext и метаданные.
* Для локальной разработки допускается `.env`/`config/secrets.env`, но в бою используется строго Vault.

**2.7.2. Таблица `api_keys`**

Добавляется новая таблица:

* `id UUID PRIMARY KEY`
* `user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE` — владелец ключа.
* `exchange TEXT NOT NULL CHECK (exchange = 'bybit')`
* `label TEXT NOT NULL` — человекочитаемое имя («Основной аккаунт», «Testnet»).
* `key_id TEXT NOT NULL` — публичная часть ключа (Bybit API Key).
* `key_ciphertext BYTEA NOT NULL` — шифротекст секрета (результат Vault Transit).
* `permissions JSONB` — права ключа (read-only, trading, withdrawals=false и т.п.).
* `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
* `last_used_at TIMESTAMPTZ` — для аудита.
* `is_active BOOLEAN NOT NULL DEFAULT TRUE`

Индексы:

* `UNIQUE (user_id, label)` — один label на пользователя.
* `INDEX (user_id, is_active)` — быстрый выбор активных ключей пользователя.

**2.7.3. Потоки**

* **Добавление ключа**

  1. Пользователь (обычно `admin`) вводит `api_key` + `api_secret` + label + env (mainnet/testnet).
  2. Backend:

     * валидирует ключи через пробный запрос к Bybit (например, `GET /v5/account/info`);
     * отправляет `api_secret` в Vault Transit (`/transit/encrypt/bybit`) → ciphertext;
     * сохраняет запись в `api_keys` с ciphertext.
* **Использование ключа**

  * При каждом запросе к Bybit:

    1. Сервис запрашивает в Vault Transit команду `/transit/decrypt/bybit` по `key_ciphertext`.
    2. Transit возвращает секрет в память процесса.
    3. Секрет используется только для подписания текущего запроса и не логируется.
  * Все операции расшифровки логируются в `audit_trail` (user_id, действие `bybit_key_used`, ключ label, timestamp).
* **Ротация ключа**

  * Реализуется как создание новой записи в `api_keys` и деактивация старой (`is_active = false`).
  * Планируется политика: обязательная ротация ключей не реже 1 раза в N дней (настраивается).

---

## 3. РЕШЕНИЯ ЗАКАЗЧИКА ПО КЛЮЧЕВЫМ ВОПРОСАМ

### Q-01. СТАДИЯ ПРОЕКТА

- **Решение**: строим сразу production-grade систему (v2.4+), без «одноразового» прототипа на PineScript/Excel.
- **Следствие**:
  - Есть обязательная спецификация (текущий документ).
  - Есть репозиторий с чёткой структурой.
  - Есть CI/CD, тесты, метрики, логирование.
  - Миграции БД через Alembic.

### Q-02. ХРАНЕНИЕ СЕКРЕТНЫХ ДАННЫХ (API-КЛЮЧИ И ПАРОЛИ)

- **Для продакшена**:
  - Используется **HashiCorp Vault** с Transit-движком как KMS.
  - API-ключи Bybit и TOTP-секреты **не** хранятся в открытом виде ни в БД, ни в файловой системе.
  - БД хранит только ciphertext и ограниченный набор метаданных (таблица `api_keys`).
- **Для dev/test**:
  - Допускается `.env`/`config/secrets.env`, но:
    - файл не коммитится в репозиторий;
    - в README явно прописана разница между dev и prod.
- **Операции шифрования/дешифрования**:
  - выполняются через Vault Transit API;
  - логируются в `audit_trail` (кто и для какого ключа инициировал операцию);
  - подлежат периодическому аудиту.
- **Пароли пользователей**:
  - хэшируются через Argon2id или bcrypt (см. §2.5);
  - соль и параметры хэширования задаются конфигурацией в соответствии с актуальными рекомендациями OWASP.

### Q-03. RECONCILIATION И KILL-SWITCH

- **Решение**: система обязана выполнять регулярный reconciliation позиций между внутренней БД и фактическим состоянием на Bybit.
- **Детали реализации**:
  - периодический опрос биржи по всем отслеживаемым символам и сравнение с внутренними таблицами позиций и ордеров;
  - все расхождения фиксируются в таблице `reconciliation_log` с указанием уровня критичности (`info`/`warning`/`critical`);
  - при критических расхождениях срабатывает kill-switch: подача новых ордеров блокируется, по конфигурации возможна автоматическая ликвидация позиций либо переход в ручной режим.
- **Требования**:
  - восстановление консистентности позиций укладывается в целевые RTO/RPO, описанные в §1;
  - обязанности операторов по реагированию на инциденты и процедурам восстановления описаны в §8.3 и соответствующих runbooks.

### Q-04. ВАЛИДАЦИЯ РЕЗУЛЬТАТОВ И «BEFORE LIVE»

- **Решение**: перед включением реальной торговли стратегия проходит обязательную фазу forward-validation на боевом рынке с полностью отключённой подачей ордеров.
- **Детали**:
  - длительность калибровки — не менее 30 календарных дней;
  - в течение этого периода система генерирует сигналы и моделирует сделки, не отправляя ордера на биржу;
  - по окончании периода рассчитываются и анализируются ключевые метрики:
    - win-rate (WR);
    - profit factor (PF);
    - максимальная просадка (MaxDD);
    - фактический и моделируемый slippage.
- **Ограничение**:
  - решение о переходе в боевой режим принимается только при выполнении заранее согласованных порогов по указанным метрикам;
  - при отсутствии успешно пройденной forward-фазы включение торговли запрещено.

### Q-05. РЕЗЕРВИРОВАНИЕ, BACKUP И DISASTER RECOVERY

- **Решение**: для БД и критичных компонентов действует формальный план резервного копирования и аварийного восстановления (DR-план).
- **Детали**:
  - регулярные резервные копии PostgreSQL по расписанию (минимум ежедневно) с проверкой успешного завершения;
  - off-site хранение резервных копий (отдельный storage/регион);
  - наличие и поддержание в актуальном состоянии документа `docs/disaster_recovery.md` с описанием сценариев отказа и шагов восстановления;
  - тестовые восстановления из backup не реже одного раза в квартал на отдельном стенде с проверкой непротиворечивости и полноты данных.
- **Цели**:
  - соблюдение целевых RTO/RPO, указанных в §1;
  - документируемая и воспроизводимая процедура восстановления в случае потери основного инстанса БД или инфраструктуры.

### Q-06. ПОЛЬЗОВАТЕЛИ, РОЛИ И 2FA

- **Модель**: один tenant (заказчик), несколько пользователей с ролями `viewer`, `trader`, `admin`.
- **Требования**:
  - 2FA обязательно для ролей `admin` и `trader`;
  - управление пользователями (создание, деактивация, смена роли) осуществляется через UI только пользователями с ролью `admin`;
  - любые изменения ролей и статуса пользователя (включая включение/отключение 2FA) логируются в `audit_trail`.

### Сводная таблица дополнительных решений

| ID    | Решение заказчика                                                                 | Детали реализации |
|-------|------------------------------------------------------------------------------------|-------------------|
| **Q-07** | Backtesting реализуется в отдельном сервисе/репозитории, а не внутри продакшен-кода. | Логика сигналов и индикаторов выносится в общий пакет `strategy-core`, который переиспользуется и прод-сервисом, и backtest-сервисом. Прод-сервис не имеет режима загрузки исторических данных и запуска бэктестов, чтобы исключить риски перепутать окружения и не нагружать боевой контур. |
| **Q-08** | Стартовый whitelist состоит из 15 USDT-маржинальных перпетуальных контрактов Bybit. | Фиксированный список символов: `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, `BNBUSDT`, `DOGEUSDT`, `TONUSDT`, `LINKUSDT`, `LTCUSDT`, `TRXUSDT`, `ADAUSDT`, `AVAXUSDT`, `OPUSDT`, `ARBUSDT`, `MATICUSDT`. Автоматический подбор по объёму/волатильности в первой версии не используется; изменение списка происходит только через конфигурацию/миграцию. |
| **Q-09** | На старте используется single-instance режим без HA-кластера, с жёсткими требованиями к восстановлению. | Первый релиз запускается в виде одного инстанса `algo-grid-core` с мониторингом и авто-рестартом. Допустимое время восстановления после падения инстанса (RTO) — до 15 минут; при рестарте обязательна полная ресинхронизация позиций и ордеров с биржей. HA-кластер (active-passive или иной вариант с защитой от split-brain) проектируется и внедряется отдельным этапом по согласованной дорожной карте; текущая архитектура должна заранее учитывать возможность многонодового запуска (состояние и блокировки — во внешних стореджах, а не в локальных файлах). |

---

## 3.1. ДОПОЛНИТЕЛЬНЫЕ ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ

| ID     | Требование                                                                                                     | Место реализации                                      |
|--------|----------------------------------------------------------------------------------------------------------------|-------------------------------------------------------|
| **R-01** | Логирование стакана L50 для исследований (BTC/ETH с Дня 8). Таблица `orderbook_l50_log` (TimescaleDB), retention 30 дней, запись только при `config.research_mode: true`. | `src/data/orderbook_logger.py`                        |
| **R-02** | Режим Break-Even настраивается через `config.risk.be_mode: str` (`'full'` или `'visual'`). При `'full'` — SL=Entry после TP1. | `src/core/risk_manager.py`                            |
| **R-03** | Измерение реального slippage: метод `OrderManager.record_slippage()`, таблица `slippage_log`, запись при каждом fill. | `src/execution/order_manager.py`                      |
| **R-04** | Anti-churn queue: статус `'queued'` для повторных сигналов в течение 15 мин. UI показывает таймер.           | `src/risk/anti_churn.py`                              |
| **R-05** | Rate Limiter: бакеты `rest_read: 1200/min`, `rest_order: 10/sec`, `ws: 300 подписок`. Redis-счётчики с TTL. | `src/integration/bybit/rate_limiter.py`               |
| **R-06** | Funding-exit: при `time_to_funding < 10мин` и `UPnL < 0.3R` — алерт в UI. Флаг `config.risk.auto_funding_exit: bool`. | `src/core/risk_manager.py`                            |
| **R-07** | DTO `ConfirmedCandle` с валидацией: прошла временная граница интервала, `confirm=true` по WS, sanity-чек (Close ∈ [Low, High], Volume ≥ 0). Конструктор выбрасывает `InvalidCandleError` при нарушении. | `src/core/models.py`                                  |
| **R-08** | Retention: `signals` — 90 дней, `positions` — 180 дней, `klines_*` — 30 дней. Архивация в S3 по cron.        | `src/core/archiver.py`                                |

Дополнительные требования:

- **R-11 (Security/KMS)**  
  Все секреты (API-ключи, TOTP-секреты, ключи для подписи JWT) в продакшене:
  - либо хранятся в KMS/Vault;
  - либо шифруются на уровне приложения с использованием ключей, хранимых в KMS.

- **R-12 (2FA)**  
  Роли `admin` и `trader` обязаны использовать 2FA. Включение и отключение 2FA фиксируется в `audit_trail`.

- **R-13 (GDPR/Data privacy)**  
  Хранение и обработка персональных данных (email, IP-адреса, финансовые результаты счёта) соответствуют требованиям GDPR:
  - реализован механизм Right to be forgotten (анонимизация записей по запросу);
  - определена и документирована политика хранения (retention) и удаления данных (см. §8.2 и `docs/gdpr_compliance.md`).

- **R-14 (Observability & Alerting)**  
  Система должна иметь:
  - базовый набор технических и бизнес-метрик (latency, error-rate, slippage, PnL, WR, PF, MaxDD);
  - алерты на критические события: отсутствие данных, рост ошибок Bybit API, падение WR, рост slippage.

- **R-15 (Load & Stress)**  
  Перед включением в боевой режим проводится нагрузочное тестирование (см. §8.6):
  - имитация целевой и пиковых нагрузок по WebSocket/REST;
  - проверка устойчивости при деградации внешних сервисов (Bybit API, Redis, PostgreSQL);
  - фиксация достигнутых SLA/порогов, при нарушении которых релиз считается не прошедшим проверку.

---

## 3.2. SQL-СХЕМА И DDL

### 3.2.1. Общие принципы

- База данных: PostgreSQL 15+.
- Time-series таблицы (свечи, стакан, slippage) реализуются через TimescaleDB hypertables.
- Все таблицы имеют:
  - явный `PRIMARY KEY`;
  - индексы по критичным полям (время, символ, `user_id`);
  - внешние ключи для ссылок на `users`, `signals`, `positions`.

### 3.2.2. Ключевые таблицы (DDL-пример)

Ниже указаны эталонные DDL-заготовки; фактические миграции оформляются через Alembic.

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
````

```sql
CREATE TABLE signals (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
    entry_price NUMERIC(18,8) NOT NULL,
    tp1_price NUMERIC(18,8) NOT NULL,
    tp2_price NUMERIC(18,8),
    tp3_price NUMERIC(18,8),
    sl_price NUMERIC(18,8) NOT NULL,
    risk_r NUMERIC(6,3) NOT NULL,
    probability NUMERIC(4,3) NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version VARCHAR(20) NOT NULL,
    queued_until TIMESTAMPTZ,
    error_code INTEGER,
    error_message TEXT
);

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

```sql
CREATE TABLE reconciliation_log (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    severity TEXT CHECK (severity IN ('info','warning','critical')),
    description TEXT NOT NULL,
    details JSONB
);

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

Таблицы рыночных данных (`klines_5m`, `orderbook_l50_log`, `slippage_log`) оформляются как hypertables TimescaleDB с ключом по времени и полю `symbol`.

### 3.2.3. Миграции (Alembic)

* Все изменения схемы БД проходят через Alembic.
* На одну логически цельную задачу создаётся один `revision`.
* На продакшене действует строгий запрет на ручные изменения схемы БД в обход миграций.
* Документация по схеме и миграциям ведётся в `docs/database_schema.md`.

```

## 4. [ASSUMPTION] — ТЕХНИЧЕСКИЕ ДОПУЩЕНИЯ

| ID | Допущение | Обоснование |
|----|-----------|-------------|
| **A‑01** | **Язык — Python 3.11+**, async‑first (aiohttp, asyncio). | ТЗ явно указывает Python, современные версии дают лучшую производительность async‑кода. |
| **A‑02** | **FastAPI запускается с Uvicorn**, 4+ worker‑процесса (от CPU‑cores). | Достижение p95 latency <5с при параллельной обработке multiple WS‑каналов. |
| **A‑03** | **PostgreSQL 15+ с TimescaleDB extension** для гипертейблов `klines_*`, `signals`. | TimescaleDB оптимизировано для временных рядов, что соответствует спеке. |
| **A‑04** | **Redis 7+**, используются Streams (для очереди сигналов) и pub/sub (для realtime UI). | Streams обеспечивают durable queue, pub/sub — низкую латентность для SSE. |
| **A‑05** | **UI хостится на том же домене** (по пути `/ui`), статика раздаётся через `StaticFiles` FastAPI. | Проще CORS, единая аутентификация. |
| **A‑06** | **Конфигурация в `settings.yaml`**, секреты в `.env`, валидация через `pydantic.BaseSettings`. | Стандартная практика, позволяет hot‑reload без перезапуска при изменении non‑secret параметров. |
| **A‑07** | **Мониторинг через Prometheus + Grafana**, метрики экспортируются на `/metrics`. | Упомянуты p95 latency, требуется современная observable система. |
| **A‑08** | **Логи пишутся в JSONL** с ротацией по 100MB, retention 7 дней, уровень `INFO` в проде. | Проще анализ в ELK/Splunk, соответствует требованию аудита. |
| **A‑09** | **Тестовое покрытие ≥80%** для `core`, `strategies`, `risk`. | Критичные модули без тестов недопустимы в торговой системе. |
| **A‑10** | **CI/CD через GitHub Actions**: lint (ruff), type‑check (mypy), unit‑тесты, сборка Docker‑image. | Обеспечивает reproducible deploy и качество кода. |

---

## 5. АРХИТЕКТУРА СИСТЕМЫ И МОДУЛИ

### 5.1. High‑Level Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                               USER UI (Vanilla JS)                           │
│                      (SSE → /stream, REST → /api/v1)                         │
└─────────────────────────────┬───────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────────────┐
│                        FASTAPI API GATEWAY (uvicorn)                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ /signals │  │ /positions│  │ /config  │  │ /health  │  │ /metrics │ ...  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘      │
│       │             │             │             │             │              │
└───────│─────────────│─────────────│─────────────│─────────────│──────────────┘
        │             │             │             │             │
        │             │             │             │             │
┌───────▼─────────────▼─────────────▼─────────────▼─────────────▼──────────────┐
│                        CORE BUSINESS LAYER (async)                          │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │ StrategyEngine   │  │ RiskManager      │  │ OrderManager     │          │
│  │   (AVI‑5)        │  │   (Limits, BE)   │  │   (Manual)       │          │
│  └─────────┬────────┘  └─────────┬────────┘  └─────────┬────────┘          │
│            │                     │                     │                    │
│  ┌─────────▼────────┐  ┌────────▼────────┐  ┌───────▼─────────┐            │
│  │ IndicatorCalc    │  │ PositionTracker │  | FillTracker     │            │
│  │ (VWAP, ATR, ...) │  │  (per‑symbol)   │  | (slippage)      │            │
│  └─────────┬────────┘  └─────────────────┘  └─────────────────┘            │
└────────────┼─────────────────────────────────────────────────────────────────┘
             │
┌────────────▼─────────────────────────────────────────────────────────────────┐
│                         DATA & INTEGRATION LAYER                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐            │
│  │ BybitWSClient    │  │ BybitRESTClient  │  │ RedisStreams     │            │
│  │ (kline, OB)      │  │ (orders, snaps)  │  │ (queue, pub/sub) │            │
│  └─────────┬────────┘  └─────────┬────────┘  └─────────┬────────┘            │
│            │                     │                     │                     │
│  ┌─────────▼─────────────────────▼─────────────────────▼────────┐            │
│  │                    PostgreSQL (TimescaleDB)                  │            │
│  │  klines_5m/15m, signals, positions, metrics,                 │            │
│  │  slippage_log,orderbook_l50_log                              │            │
│  └──────────────────────────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────────────────────────┘
             │
┌────────────▼─────────────────────────────────────────────────────────────────┐
│                      MONITORING & OBSERVABILITY                              │
│  Prometheus (metrics) ← Grafana (dashboard) ← AlertManager (kill‑switch)      │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 5.2. Описание модулей

| Модуль | Назначение | Входы | Выходы | Ключевые инварианты | Нефункциональные требования |
|--------|------------|-------|--------|---------------------|-----------------------------|
| **BybitAdapter** (`integration/bybit`) | Управление WS/REST‑соединениями, нормализация данных, rate‑limiting, reconnect‑логика. | Bybit WS/REST API, конфиг подключения. | Нормализованные `Kline`, `OrderBook`, `OrderStatus`. | 1) Не более 300 WS‑подписок. 2) 429‑retry ≤5 попыток. 3) Gap-detection (Update/Delete/Update - обнаружение разрывов в последовательности обновлений). | Latency подтверждения бара <5с, p95. |
| **DataCollector** (`data/collector.py`) | Сбор сырых данных, дедупликация, публикация в Redis Streams. | Bybit WS messages, REST snapshots. | Streams: `ws_raw:kline:5m`, `ws_raw:ob:L10`. | 1) Каждое сообщение имеет `sequence_id`. 2) Duplicates отбрасываются по `event_time+symbol`. | Throughput ≥10k msg/sec, CPU <30%. |
| **IndicatorEngine** (`strategies/indicators.py`) | Расчёт VWAP, ATR, EMA, Donchian, Imbalance, Microprice. | `Kline`, `OrderBook` slices. | DTO `IndicatorSet`. | 1) VWAP — rolling 20 баров. 2) ATR — EMA 14. 3) Imbalance ∈ [‑1;1]. | Расчёт ≤50мс на бар. |
| **SignalEngine** (`strategies/avi5.py`) | Генерация сигналов по правилам стратегии AVI-5 (Trend filter 15m, Trigger 5m: Close vs VWAP+σ, imbalance, microprice, spread_ok, time_to_funding), фильтрам, walk‑forward калибровке. | `IndicatorSet`, `config:AVI5Config`. | `Signal` DTO (id, symbol, side, entry, tp1‑3, sl, R, p_win). | 1) Только `confirmed=True`. 2) `spread_ok` == True. 3) `time_to_funding ≥15min`. | Max latency 100мс от закрытия бара до появления сигнала. |
| **RiskManager** (`risk/risk_manager.py`) | Проверка лимитов: concurrent positions, per‑base, max total risk, anti‑churn, BE‑trigger. | `Signal`, текущие `Position[]`, `config:RiskLimits`. | `Allow/Deny + reason`, `BEEvent`. | 1) `MAX_TOTAL_RISK_USD` ≤ $250. 2) `MAX_POSITIONS_PER_BASE = 2`. 3) Anti‑churn 15мин. | Решение ≤10мс. |
| **OrderManager** (`execution/order_manager.py`) | Ручное создание ордеров: UI → копирование уровней, валидация, визуализация fill‑rate. | `Signal.id`, `user_action: PlaceOrder`, API‑ключ. | `Order` (status, fill_ratio, slippage). | 1) В текущем релизе **auto-place полностью запрещён**: ордер отправляется только после явного клика трейдера в UI. 2) Fill‑rate ≥95% → позиция открыта. | Запись в БД ≤50мс после fill. |
| **UIPublisher** (`api/stream.py`) | Server‑Sent Events (SSE) для realtime обновлений плиток, BE‑событий, метрик. | Redis pub/sub (channels: `ui:signals`, `ui:be`, `ui:metrics`). | SSE stream → браузер. | 1) P95 latency ≤5с. 2) Reconnect с last‑event‑id. | Коннекшенов ≤500/instance. |
| **CalibrationService** (`strategies/calibration.py`) | Walk‑forward пересчёт `θ*(h)` каждые 30 дней, логика «мёртвых часов», Liquidity guard. | История сигналов 180 дней, `PF_h(θ)` метрика. | `θ_map: Dict[int,float]`, запись в `settings.calibration`. | 1) Без утечек данных. 2) OOS‑PF ≥1.3. 3) PSI‑триггер >0.2 → сокращение окна до 90 дней. | Должен работать offline, не блокировать SignalEngine. |
| **Monitoring & KillSwitch** (`monitoring/alerts.py`) | Сбор метрик (latency, fill‑rate, WR, PF, MaxDD), алерты в Telegram/Slack, автоостановка генерации. | Prometheus metrics, `signals`, `positions`. | AlertManager webhook, `POST /admin/kill_switch`. | 1) `MaxDD >25%` 3 дня → блокировка. 2) `Net Expectancy <‑0.1R` → ALERT. | Polling раз в 60с. |

---

## 6. СТРУКТУРА РЕПОЗИТОРИЯ И ПЕРЕЧЕНЬ ФАЙЛОВ

```
bybit-algo-grid/
├── .github/
│   └── workflows/
│       └── ci.yml                     # GitHub Actions: lint, test, build
├── config/
│   ├── settings.yaml                  # Публичная конфигурация
│   ├── secrets.env.example           # Шаблон секретов
│   └── schema.py                     # Pydantic схемы валидации
├── docs/
│   ├── api.md                        # OpenAPI документация
│   ├── deployment.md                 # Инструкция по деплою
│   ├── disaster_recovery.md
│   ├── risk_disclaimer.md            # Юридический дисклеймер
│   ├── backup_strategy.md            # Стратегия резервного копирования
│   └── images/                       # Диаграммы
├── scripts/
│   ├── run_calibration.py            # CLI калибровки θ*(h)
│   ├── migrate.py                    # Запуск миграций БД
│   ├── backup.py                     # Резервное копирование БД
│   └── restore.py                    # Восстановление из бэкапа
├── tests/
│   ├── __init__.py
│   ├── conftest.py                   # Fixtures (test DB, Redis, Bybit mock)
│   ├── unit/
│   │   ├── test_indicators.py
│   │   ├── test_signal_engine.py
│   │   ├── test_risk_manager.py
│   │   └── test_rate_limiter.py
│   ├── integration/
│   │   ├── test_bybit_ws.py
│   │   ├── test_order_lifecycle.py
│   │   └── test_sse_stream.py
│   └── fixtures/
│       ├── sample_kline.json
│       └── sample_ob.json
├── docker/
│   ├── Dockerfile                    # Мульти‑стейдж сборка Python
│   ├── docker-compose.yml            # Local dev stack (PG, Redis, app)
│   └── docker-compose.prod.yml       # Production stack (Traefik, replicas)
├── frontend/
│   ├── static/
│   │   ├── css/
│   │   │   └── styles.css            # Tailwind‑based стили
│   │   └── js/
│   │       ├── main.js               # Инициализация SSE, рендер плиток
│   │       ├── api.js                # Fetch‑wrapper для REST
│   │       ├── tiles.js              # Virtual scrolling, IntersectionObserver
│   │       └── modal.js              # Модальное окно с уровнями
│   ├── templates/
│   │   └── index.html                # Главная страница
│   └── assets/
│       └── icons/                    # Иконки стрелок, статусов
├── logs/                             # .gitignore, для JSONL‑логов
├── src/
│   ├── __init__.py
│   ├── main.py                       # FastAPI app factory, lifespan
│   ├── auth/
│   │   ├── rbac.py                   # Ролевая модель доступа
│   │   ├── middleware.py             # Обёртка аутентификации FastAPI
│   │   └── jwt_manager.py            # Управление JWT-токенами
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config_loader.py          # Загрузка и валидация конфигурации
│   │   ├── constants.py              # Константы (дни недели, таймауты)
│   │   ├── exceptions.py             # Кастомные исключения
│   │   ├── logging_config.py         # Structlog + JSONL
│   │   ├── models.py                 # Pydantic DTO (Signal, Position, Config)
│   │   ├── distributed_lock.py       # Распределённые блокировки
│   │   └── reconciliation.py         # Сервис сверки состояния
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── signals.py          # GET /signals, GET /signal/{id}
│   │   │   ├── positions.py        # GET /positions, POST /positions/close
│   │   │   ├── config.py           # GET /config, PATCH /config"
│   │   │   ├── health.py           # GET /health, GET /ready
│   │   │   └── admin.py            # POST /admin/kill_switch
│   │   └── middleware/
│   │       ├── __init__.py
│   │       ├── auth.py             # JWT‑аутентификация (Bearer token)
│   │       ├── cors.py             # CORS политика
│   │       └── rate_limit.py       # IP‑based rate limit (100 req/min)
│   ├── data/
│   │   ├── __init__.py
│   │   ├── collector.py            # Подписка WS, публикация в Redis Streams
│   │   └── storage.py              # Запись klines/OB в TimescaleDB — утилита записи в БД (deprecated, см. 7.58).
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── avi5.py                 # SignalEngine: генерация сигналов
│   │   ├── indicators.py           # IndicatorEngine: расчёт VWAP, ATR и т.д.
│   │   └── calibration.py          # CalibrationService: walk‑forward θ*(h)
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── order_manager.py        # Ручное исполнение, валидация, fill‑rate
│   │   ├── fill_tracker.py         # Обработка fill‑events от Bybit
│   │   └── slippage_monitor.py     # Запись slippage в БД
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── risk_manager.py         # Главный Risk‑engine
│   │   ├── position_limits.py      # Проверка per‑base лимитов
│   │   └── anti_churn.py           # Логика блокировки 15мин
│   ├── notifications/
│   │   ├── __init__.py
│   │   ├── webhooks.py             # Отправка BE‑events на внешние URL
│   │   └── ui_notifier.py          # Публикация в Redis pub/sub для SSE
│   ├── monitoring/
│   │   ├── __init__.py
│   │   ├── metrics.py              # Prometheus Counter/Gauge/Histogram
│   │   └── alerts.py               # Проверка kill‑switch условий
│   ├── db/
│   │   ├── __init__.py
│   │   ├── connection.py           # AsyncPG connection pool
│   │   ├── migrations.py           # Alembic‑миграции
│   │   └── repositories/
│   │       ├── __init__.py
│   │       ├── signal_repository.py    # CRUD для signals
│   │       ├── position_repository.py  # CRUD для positions
│   │       └── metrics_repository.py   # Чтение для Grafana
│   └── integration/
│       ├── __init__.py
│       └── bybit/
│           ├── __init__.py
│           ├── ws_client.py        # Управление WebSocket
│           ├── rest_client.py      # Обертка над Bybit REST
│           ├── rate_limiter.py     # Token‑bucket rate limiter
│           └── error_handler.py    # Централизованная обработка ошибок Bybit API
├── .gitignore
├── .dockerignore
├── pyproject.toml                    # Зависимости и скрипты проекта (Poetry)
├── README.md                         # Quick start, архитектура
└── alembic.ini                       # Alembic миграции
```

---

## 7. ВНУТРЕННЯЯ СТРУКТУРА ФАЙЛОВ: КЛАССЫ, ФУНКЦИИ, ИМПОРТЫ

### 7.1. `src/main.py` — FastAPI App Factory

**Назначение**: Создание приложения, управление жизненным циклом, подключение роутов, middleware, запуск воркеров.

**Зона ответственности**:
- Инициализация всех компонентов (Redis pool, PG pool, Bybit clients).
- Регистрация эндпоинтов.
- Graceful shutdown: закрытие соединений, сброс очередей.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `create_app` | функция | `create_app(config_path: str = "config/settings.yaml") -> FastAPI` | Фабрика приложения, загружает конфиг, инициализирует пулы, подключает роуты. | Инстанс FastAPI с подключёнными middleware и роутами. | `ConfigLoadError`, `RedisConnectionError`, `PGConnectionError` | `core.config_loader`, `core.logging_config`, `api.routes.*`, `db.connection` |
| `lifespan` | контекстный менеджер | `lifespan(app: FastAPI): AsyncContextManager` | Управляет startup/shutdown: подключение к Redis, PG, Bybit WS. | None (context manager). | `StartupError` | `db.connection.init_pool`, `integration.bybit.ws_client.connect` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `FastAPI` | внешний (`fastapi`) | `FastAPI` | Создание ASGI‑приложения. |
| `AsyncExitStack` | stdlib (`contextlib`) | `AsyncExitStack` | Композиция асинхронных контекстов (Redis, PG). |
| `ConfigLoader` | внутренний (`src.core.config_loader`) | `ConfigLoader` | Загрузка и валидация `settings.yaml`. |
| `setup_logging` | внутренний (`src.core.logging_config`) | `setup_logging` | Конфигурация structlog. |
| `signal_router` | внутренний (`src.api.routes.signals`) | `router` | Подключение эндпоинтов сигналов. |
| `bybit_ws_client` | внутренний (`src.integration.bybit.ws_client`) | `BybitWSClient` | Инициализация WS‑подписок. |

#### Reconciliation при старте и периодически

- При старте приложения `startup`-хук FastAPI вызывает `ReconciliationService.run_once(startup=True)`:
  - сверяет открытые позиции в БД с фактическими позициями на Bybit;
  - помечает расхождения в таблице `reconciliation_log`;
  - при критических расхождениях может включить kill-switch (см. §3).
- Планируется периодический запуск reconciliation (например, каждые 5 минут) через `apscheduler` или системный cron-job, чтобы удерживать расхождения в пределах RPO.

#### Корректное завершение (graceful shutdown)

При получении SIGTERM/SIGINT:

- FastAPI останавливает приём новых запросов.
- Текущие запросы дожидаются завершения в течение таймаута (например, 30 секунд).
- Останавливаются фоновые задачи (reconciliation, обновление метрик).
- Закрываются соединения с БД и Redis, пишется финальная запись в `audit_trail` о корректном завершении.

Это поведение обязательно для соблюдения RTO=15 минут и предотвращения неконсистентных состояний в БД.

---

### 7.2. `src/core/config_loader.py` — Загрузка и валидация конфигурации

**Назначение**: Чтение `settings.yaml`, `.env`, валидация через Pydantic, предоставление типизированного конфиг‑объекта.

**Зона ответственности**:
- Поддержка hot‑reload (SIGHUP).
- Скрытие секретов из логов.
- Валидация диапазонов (Stake, MAX_POSITIONS и т.д.).

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `ConfigLoader` | класс | `class ConfigLoader:` | Singleton‑загрузчик конфигурации. | Экземпляр с атрибутами `.trading`, `.bybit`, `.risk` и т.д. | `ValidationError` (Pydantic), `FileNotFoundError` | `pydantic.BaseSettings`, `yaml` |
| `load_yaml_config` | метод | `def load_yaml_config(self, path: str) -> Dict[str, Any]` | Читает и парсит YAML, подставляет ENV‑переменные. | dict сырой конфигурации. | `yaml.YAMLError`, `UnicodeDecodeError` | `os.getenv` |
| `get_config` | метод | `def get_config(self) -> AppConfig` | Возвращает валидированный Pydantic‑объект. | `AppConfig` (типизирован). | `ValidationError` | `AppConfig` (из `models.py`) |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `BaseModel`, `Field` | внешний (`pydantic`) | `BaseModel`, `Field`, `validator` | Типизация и валидация конфигурации. |
| `yaml` | внешний (`PyYAML`) | `safe_load` | Парсинг YAML. |
| `Path` | stdlib (`pathlib`) | `Path` | Работа с файловыми путями. |
| `os` | stdlib (`os`) | `environ`, `getenv` | Подстановка секретов. |
| `AppConfig` | внутренний (`src.core.models`) | `AppConfig` | Тип возвращаемого значения. |

---

### 7.3. `src/core/models.py` — Pydantic DTO и domain‑модели

**Назначение**: Централизованное определение всех структур данных (сигналы, позиции, конфиг, метрики).

**Зона ответственности**:
- Валидация на уровне типов.
- Сериализация/десериализация для БД и API.
- Фабрики для создания из сырых данных Bybit.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `AppConfig` | класс | `class AppConfig(BaseSettings):` | Корневая модель конфигурации (`trading`, `bybit`, `risk`, `db`, `ui`). | Экземпляр с валидированными полями. | `ValidationError` | `pydantic` |
| `AVI5Config` | класс | `class AVI5Config(BaseModel):` | Параметры стратегии (θ, ATR multipliers, spread_threshold). | DTO. | `ValidationError` | `pydantic` |
| `ConfirmedCandle` | класс | `class ConfirmedCandle(BaseModel):` | Подтверждённая 5m‑свеча (с sanity‑check). | Экземпляр или исключение. | `InvalidCandleError` (кастомное) | `datetime` |
| `Signal` | класс | `class Signal(BaseModel):` | Сигнал со всеми уровнями (entry, tp, sl, R, p_win). | DTO, готовый к записи в БД. | `ValidationError` | `decimal.Decimal` для цен. |
| `Position` | класс | `class Position(BaseModel):` | Открытая/закрытая позиция с fill‑ratio, slippage, funding. | DTO. | `ValidationError` | `Signal` (содержит id) |
| `RiskLimits` | класс | `class RiskLimits(BaseModel):` | Лимиты риска (MAX_CONCURRENT, MAX_TOTAL_RISK, per‑base). | DTO. | `ValidationError` | `pydantic` |
| `SlippageRecord` | класс | `class SlippageRecord(BaseModel):` | Запись проскальзывания для аналитики. | DTO. | `ValidationError` | `decimal.Decimal` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `BaseModel`, `Field`, `validator` | внешний (`pydantic`) | `BaseModel`, `Field`, `validator` | Определение DTO. |
| `Decimal` | stdlib (`decimal`) | `Decimal` | Точное представление цен. |
| `datetime` | stdlib (`datetime`) | `datetime`, `timezone` | Временные метки. |
| `UUID` | stdlib (`uuid`) | `uuid4` | Генерация id. |
| `Optional`, `List`, `Dict` | stdlib (`typing`) | `Optional`, `List`, `Dict` | Типизация. |

---

### 7.4. `src/core/logging_config.py` — Конфигурация логирования

**Назначение**: Настройка structlog для JSONL‑формата, добавление контекста (request_id, user_id).

**Зона ответственности**:
- Формат: `{"timestamp": "2025-01-01T00:00:00Z", "level": "INFO", "event": "signal_generated", "signal_id": "...", "latency_ms": 45}`.
- Ротация через `logging.handlers.RotatingFileHandler`.
- Уровень в проде — `INFO`, в dev — `DEBUG`.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `setup_logging` | функция | `def setup_logging(log_level: str = "INFO", log_file: str = "logs/app.jsonl") -> None` | Конфигурирует structlog и стандартный logging. | None. | `IOError` (если нет прав на папку). | `structlog`, `logging.handlers.RotatingFileHandler` |
| `add_context_vars` | функция | `def add_context_vars(**kwargs) -> None` | Добавляет ключи в thread‑local context (например, request_id). | None. | `TypeError` (неверный тип значения). | `contextvars.ContextVar` |
| `get_logger` | функция | `def get_logger(name: str) -> structlog.BoundLogger` | Возвращает именованный логгер с преднастроенным форматом. | BoundLogger. | `ValueError` (пустое имя). | `structlog.get_logger` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `structlog` | внешний (`structlog`) | `structlog`, `configure`, `get_logger` | JSONL‑логирование. |
| `logging` | stdlib (`logging`) | `getLogger`, `INFO`, `StreamHandler`, `RotatingFileHandler` | Базовый хендлер для ротации. |
| `contextvars` | stdlib (`contextvars`) | `ContextVar` | Thread‑safe контекст. |
| `Path` | stdlib (`pathlib`) | `Path` | Создание директории logs. |

---

### 7.5. `src/api/routes/signals.py` — REST‑эндпоинты для сигналов

**Назначение**: Отдача списка активных сигналов, деталей по ID, фильтрация по вероятности и символу.

**Зона ответственности**:
- Только чтение из БД, никакой бизнес‑логики.
- Пагинация (offset/limit).
- Авторизация: Bearer JWT (read‑only scope).

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `get_signals` | функция | `async def get_signals(symbol: Optional[str] = None, p_threshold: float = 0.5, limit: int = 50, offset: int = 0) -> List[SignalResponse]` | Возвращает список сигналов (из `signal_repository`). | Список `SignalResponse` (без чувствительных полей). | `HTTPException(404)`, `DatabaseError` | `db.repositories.signal_repository`, `core.models.SignalResponse` |
| `get_signal_by_id` | функция | `async def get_signal_by_id(signal_id: UUID) -> SignalResponse` | Детали одного сигнала. | `SignalResponse`. | `HTTPException(404)` | `signal_repository.get_by_id` |
| `copy_levels` | функция | `async def copy_levels(signal_id: UUID) -> CopyLevelsResponse` | Возвращает уровни Entry/TP/SL в формате для UI (кнопка Copy). | `{ "entry": float, "tp1": float, ... }`. | `HTTPException(404)` | `signal_repository.get_by_id` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `APIRouter` | внешний (`fastapi`) | `APIRouter` | Создание роутера. |
| `HTTPException` | внешний (`fastapi`) | `HTTPException` | Обработка ошибок. |
| `UUID` | stdlib (`uuid`) | `UUID` | Типизация path‑parameter. |
| `Optional`, `List` | stdlib (`typing`) | `Optional`, `List` | Типизация. |
| `SignalRepository` | внутренний (`src.db.repositories.signal_repository`) | `SignalRepository` | Доступ к данным. |
| `SignalResponse` | внутренний (`src.core.models`) | `SignalResponse` | DTO для ответа. |

---

### 7.6. `src/api/routes/positions.py` — Управление позициями (ручное закрытие)

**Назначение**: Позволяет трейдеру закрыть позицию досрочно (time‑exit, manual‑exit), получить список открытых.

**Зона ответственности**:
- Проверка прав: только владелец позиции (либо админ).
- Валидация, что позиция ещё открыта.
- Запись `final_status = 'manual_exit'` в `positions`.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `get_open_positions` | функция | `async def get_open_positions() -> List[PositionResponse]` | Список открытых позиций (fill_ratio ≥50%). | `List[PositionResponse]`. | `DatabaseError` | `position_repository.get_open` |
| `close_position` | функция | `async def close_position(position_id: UUID, close_reason: Literal['manual','time_exit','funding']) -> CloseResponse` | Закрывает позицию, рассчитывает финальный PnL, записывает в БД. | `{ "position_id": UUID, "pnl_usd": float, "status": "closed" }`. | `HTTPException(400, "already_closed")`, `DatabaseError` | `position_repository.close`, `risk_manager.release_limits` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `APIRouter` | внешний (`fastapi`) | `APIRouter` | Роутер. |
| `HTTPException` | внешний (`fastapi`) | `HTTPException` | Ошибки. |
| `UUID` | stdlib (`uuid`) | `UUID` | Типизация. |
| `Literal` | stdlib (`typing`) | `Literal` | Ограничение значений `close_reason`. |
| `PositionRepository` | внутренний (`src.db.repositories.position_repository`) | `PositionRepository` | Работа с позициями. |
| `RiskManager` | внутренний (`src.risk.risk_manager`) | `RiskManager` | Освобождение лимитов после закрытия. |

---

### 7.7. `src/api/routes/admin.py` — Kill‑switch и управление

**Назначение**: Ручная блокировка/разблокировка генерации сигналов, вызов экстренной остановки.

**Зона ответственности**:
- Аутентификация: только роль `admin`.
- Блокировка `SignalEngine` через флаг в Redis (`kill_switch:active`).
- Уведомление в UI и вебхуки.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `kill_switch` | функция | `async def kill_switch(action: Literal['enable','disable']) -> KillSwitchResponse` | Устанавливает флаг в Redis, блокируя SignalEngine. | `{ "status": "enabled|disabled", "at": timestamp }`. | `HTTPException(403, "insufficient_scope")` | `redis.Redis.set`, `notifications.ui_notifier.broadcast` |
| `get_system_status` | функция | `async def get_system_status() -> SystemStatus` | Возвращает текущую метрику (Net Expectancy, WR, MaxDD, kill_switch state). | `SystemStatus` DTO. | `DatabaseError` | `monitoring.metrics.get_summary`, `redis.Redis.get` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `APIRouter`, `Depends` | внешний (`fastapi`) | `APIRouter`, `Depends` | Роутер + dependency injection. |
| `Literal` | stdlib (`typing`) | `Literal` | Ограничение `action`. |
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Доступ к kill‑switch флагу. |
| `UINotifier` | внутренний (`src.notifications.ui_notifier`) | `UINotifier` | Рассылка события в UI. |
| `Metrics` | внутренний (`src.monitoring.metrics`) | `Metrics` | Получение текущих метрик. |

---

### 7.8. `src/api/routes/health.py` — Health‑checks для Kubernetes

**Назначение**: readiness/liveness probe: проверка коннектов к Redis, PG, Bybit WS.

**Зона ответственности**:
- Быстрый ответ (<100мс).
- Не нагружать бизнес‑логику.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `health_check` | функция | `async def health_check() -> HealthResponse` | Liveness: всегда 200. | `{ "status": "alive" }`. | — | — |
| `ready_check` | функция | `async def ready_check(redis: Redis = Depends, pg: AsyncPG = Depends) -> ReadyResponse` | Readiness: проверяет коннекты. | `{ "status": "ready", "checks": { "redis": true, "pg": true } }`. | `HTTPException(503)` при неготовности. | `redis.ping`, `pg.fetchval('SELECT 1')` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `APIRouter` | внешний (`fastapi`) | `APIRouter` | Роутер. |
| `HTTPException` | внешний (`fastapi`) | `HTTPException` | 503 error. |
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Проверка Redis. |
| `AsyncPG` | внешний (`asyncpg.pool`) | `AsyncPG` | Проверка PostgreSQL. |

---

### 7.9. `src/api/middleware/auth.py` — JWT аутентификация

**Назначение**: Проверка Bearer токена, извлечение scope, прикрепление `user_id` к request.state.

**Зона ответственности**:
- Интеграция с внешним auth‑провайдером (Keycloak / Auth0) или self‑hosted JWT (HS256).
- Поддержка трёх ролей:
  - `viewer` (read-only) — чтение сигналов, позиций, метрик
  - `trader` (read + trading) — все права viewer + управление позициями, калибровка  
  - `admin` (full access) — все права trader + управление пользователями, конфигурацией
- Делегировать разбор и проверку JWT в `JWTAuthManager` из `src/auth/jwt_manager.py`.
- Использовать RBAC-правила из `src/auth/rbac.py` для проверки ролей.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `JWTAuthMiddleware` | класс | `class JWTAuthMiddleware(BaseHTTPMiddleware):` | Middleware для проверки заголовка `Authorization: Bearer <token>`. | None (прикрепляет user к request). | `HTTPException(401, "invalid_token")`, `HTTPException(403, "insufficient_scope")` | `jwt.decode`, `core.config_loader` (для секрета JWT) |
| `verify_token` | метод | `async def verify_token(self, token: str) -> Dict[str, Any]` | Декодирует JWT, проверяет exp, signature, scope. | payload dict. | `jwt.ExpiredSignatureError`, `jwt.InvalidSignatureError` | `jwt` |
| `get_current_user` | функция | `def get_current_user(request: Request) -> UserContext` | Dependency для роутов: возвращает `user_id, scopes`. | `UserContext` (pydantic). | `AttributeError` (если middleware не сработал). | `Request.state.user` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `BaseHTTPMiddleware` | внешный (`starlette.middleware.base`) | `BaseHTTPMiddleware` | Базовый класс middleware. |
| `Request` | внешный (`starlette.requests`) | `Request` | Доступ к headers. |
| `jwt` | внешный (`PyJWT`) | `decode` | Проверка JWT. |
| `HTTPException` | внешный (`fastapi`) | `HTTPException` | Ошибки auth. |
| `UserContext` | внутренний (`src.core.models`) | `UserContext` | DTO для user. |

---

### 7.10. `src/data/collector.py` — Сбор и публикация WS‑данных

**Назначение**: Подписка на Bybit WS‑каналы, дедупликация, публикация в Redis Streams.

**Зона ответственности**:
- Несколько воркеров (по тикеру или каналу).
- Sequence‑number контроль (U/D/U).
- Fallback на REST snapshot при разрыве >5с.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `DataCollector` | класс | `class DataCollector:` | Управляет WS‑подписками и публикацией. | Экземпляр. | `WSConnectionError` | `BybitWSClient`, `Redis` |
| `subscribe_klines` | метод | `async def subscribe_klines(self, symbols: List[str], interval: str) -> None` | Подписывается на `kline.{interval}` для списка символов. | None. | `RateLimitError` (превышен лимит подписок). | `bybit_ws_client.subscribe` |
| `subscribe_orderbook` | метод | `async def subscribe_orderbook(self, symbols: List[str], depth: int) -> None` | Подписывается на `orderbook.{depth}@200ms`. | None. | `ValueError` (depth не в [1,50]). | `bybit_ws_client.subscribe` |
| `publish_to_stream` | метод | `async def publish_to_stream(self, stream: str, data: Dict) -> None` | Пишет сообщение в Redis Stream `ws_raw:{stream}`. | `msg_id: str`. | `RedisError` | `redis.xadd` |
| `deduplicate_message` | метод | `def deduplicate_message(self, channel: str, msg: Dict) -> bool` | Проверяет `sequence_id` и `event_time`, возвращает `True` если дубликат. | bool. | `KeyError` (нет sequence). | `redis.get(f"last_seq:{channel}")` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `asyncio` | stdlib (`asyncio`) | `asyncio`, `Task` | Управление корутинами. |
| `List`, `Dict` | stdlib (`typing`) | `List`, `Dict` | Типизация. |
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Публикация в Streams. |
| `BybitWSClient` | внутренний (`src.integration.bybit.ws_client`) | `BybitWSClient` | Подключение к WS. |
| `logger` | внутренний (`src.core.logging_config`) | `logger` | Логирование. |

---

### 7.11. `src/strategies/indicators.py` — Расчёт технических индикаторов

**Назначение**: Чистые функции для VWAP, ATR, EMA, Donchian, Imbalance, Microprice. Оптимизированы под pandas (или чистый Python для realtime).

**Зона ответственности**:
- Поддержка как realtime (на последних 20 барах), так и батч (история).
- Использование `Decimal` для точности.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `calculate_vwap` | функция | `def calculate_vwap(klines: List[Candle], window: int = 20) -> Decimal` | VWAP = Σ(TP×Vol)/ΣVol, rolling. | `Decimal`. | `ValueError` (len(klines) < window). | `decimal.Decimal` |
| `calculate_atr_ema` | функция | `def calculate_atr_ema(klines: List[Candle], period: int = 14) -> Decimal` | ATR с EMA сглаживанием. | `Decimal`. | `ValueError` (len(klines) < period). | `decimal.Decimal` |
| `calculate_ema` | функция | `def calculate_ema(values: List[Decimal], period: int) -> Decimal` | EMA для цен (Close). | `Decimal`. | `ZeroDivisionError` (period==0). | `decimal.Decimal` |
| `donchian_channel` | функция | `def donchian_channel(klines: List[Candle], window: int = 20) -> Tuple[Decimal, Decimal]` | High/Low последних 20 баров. | `(high: Decimal, low: Decimal)`. | `ValueError` (len(klines) < window). | `max`, `min` |
| `orderbook_imbalance` | функция | `def orderbook_imbalance(bids: List[Level], asks: List[Level], levels: int = 10) -> float` | (ΣBidVol‑ΣAskVol)/(ΣBidVol+ΣAskVol) ∈ [‑1;1]. | float. | `ZeroDivisionError` (пустой стакан). | `sum` |
| `microprice` | функция | `def microprice(bid: float, bid_qty: float, ask: float, ask_qty: float) -> float` | (ask*bid_qty + bid*ask_qty)/(bid_qty+ask_qty). | float. | `ZeroDivisionError` (qty==0). | `float` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `Decimal` | stdlib (`decimal`) | `Decimal` | Точные расчёты. |
| `List`, `Tuple` | stdlib (`typing`) | `List`, `Tuple` | Типизация. |
| `Candle` | внутренний (`src.core.models`) | `Candle` | DTO свечи. |
| `Level` | внутренний (`src.core.models`) | `Level` | DTO уровня стакана (price, qty). |

---

### 7.12. `src/strategies/avi5.py` — Signal Engine (AVI‑5)

**Назначение**: Оркестрация всех правил входа (trend, trigger, imbalance, microprice, spread, funding), генерация `Signal`.

**Зона ответственности**:
- Чтение `ConfirmedCandle` из Redis Stream.
- Запрос `IndicatorSet` из `IndicatorEngine`.
- Проверка RiskManager (предварительная).
- Публикация в `signal_queue` Stream и Redis pub/sub для UI.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `SignalEngine` | класс | `class SignalEngine:` | Главный класс‑оркестратор. | Экземпляр. | `StrategyConfigError` | `IndicatorEngine`, `RiskManager`, `Redis` |
| `run` | метод | `async def run(self) -> None` | Бесконечный цикл: читает `ws_raw:kline:5m`, генерирует сигналы. | None. | `asyncio.CancelledError` | `redis.xreadgroup` |
| `process_candle` | метод | `async def process_candle(self, candle: ConfirmedCandle) -> Optional[Signal]` | Проверяет все правила §2.3, возвращает Signal или None. | `Signal` или `None`. | `CalculationError` | `calculate_indicators`, `risk_manager.pre_check` |
| `publish_signal` | метод | `async def publish_signal(self, signal: Signal) -> None` | Пишет в Redis Stream `signal_queue`, публикует в `ui:signals`. | None. | `RedisError` | `redis.xadd`, `redis.publish` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `asyncio` | stdlib (`asyncio`) | `asyncio` | Цикл обработки. |
| `Optional` | stdlib (`typing`) | `Optional` | Типизация. |
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Streams/pub/sub. |
| `IndicatorEngine` | внутренний (`src.strategies.indicators`) | `IndicatorEngine` | Расчёт индикаторов. |
| `RiskManager` | внутренний (`src.risk.risk_manager`) | `RiskManager` | Проверка лимитов. |
| `Signal`, `ConfirmedCandle` | внутренний (`src.core.models`) | `Signal`, `ConfirmedCandle` | DTO. |

---

### 7.13. `src/strategies/calibration.py` — Walk‑forward калибровка θ*(h)

**Назначение**: Оффлайн‑расчёт оптимального `θ` для каждого часа дня, валидация OOS, PSI‑триггер.

**Зона ответственности**:
- Чтение истории сигналов из БД (train 180 дней, OOS 30 дней).
- Grid‑search по `θ ∈ [0.15, 0.50]`.
- Проверка `PF_OOS ≥ 1.3`, иначе fallback `θ_global = 0.35`.
- Обновление `θ_map` в Redis и `settings.yaml`.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `CalibrationService` | класс | `class CalibrationService:` | Управляет циклом калибровки. | Экземпляр. | `InsufficientDataError` | `SignalRepository`, `PositionRepository`, `Redis` |
| `run_calibration` | метод | `async def run_calibration(self, force: bool = False) -> CalibrationResult` | Запускает полный цикл: train, validate, OOS, запись. | `CalibrationResult(theta_map: Dict[int, float], pf_oos: float, fallback_used: bool)`. | `CalculationError` | `calculate_pf_for_theta` |
| `calculate_pf_for_theta` | метод | `def calculate_pf_for_theta(self, signals: List[Signal], theta: float) -> float` | Симуляция торговли по правилам §2.3 с фиксированным θ, возвращает Profit Factor. | float (PF). | `ZeroDivisionError` (нет сделок). | `strategies.avi5.SignalEngine` (для правил). |
| `check_psi_drift` | метод | `def check_psi_drift(self, recent_signals: List[Signal]) -> float` | Расчёт PSI (Population Stability Index) между train и recent. | float. | `ValueError` (пустые выборки). | `scipy.stats` (если используется). |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `List`, `Dict` | stdlib (`typing`) | `List`, `Dict` | Типизация. |
| `AsyncIOScheduler` | внешний (`apscheduler.schedulers.asyncio`) | `AsyncIOScheduler` | Планирование калибровки каждые 30 дней. |
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Запись theta_map. |
| `SignalRepository` | внутренний (`src.db.repositories.signal_repository`) | `SignalRepository` | Чтение истории сигналов. |
| `PositionRepository` | внутренний (`src.db.repositories.position_repository`) | `PositionRepository` | Чтение результатов сделок. |

---

### 7.14. `src/risk/risk_manager.py` — Главный риск‑менеджер

**Назначение**: Проверка всех лимитов (concurrent, per‑base, total risk, anti‑churn, funding‑exit), генерация BE‑events.

**Зона ответственности**:
- Хранит состояние текущих позиций (кэш в Redis).
- Проверяет новый сигнал перед публикацией.
- Мониторит открытые позиции (раз в 60с): funding‑exit, time‑exit.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `RiskManager` | класс | `class RiskManager:` | Центральный риск‑engine. | Экземпляр. | `LimitExceededError` | `Redis` (cache), `PositionRepository` |
| `check_limits` | метод | `async def check_limits(self, signal: Signal) -> Tuple[bool, str]` | Проверяет concurrent, per‑base, total risk, anti‑churn. | `(allowed: bool, reason: str)`. | `DatabaseError` (чтение позиций). | `position_limits.check`, `anti_churn.check` |
| `on_position_open` | метод | `async def on_position_open(self, position: Position) -> None` | Обновляет кэш Redis (`positions:active`). | None. | `RedisError` | `redis.hset` |
| `on_position_close` | метод | `async def on_position_close(self, position_id: UUID) -> None` | Удаляет из кэша, освобождает лимиты. | None. | `RedisError` | `redis.hdel` |
| `check_funding_exit` | метод | `async def check_funding_exit(self) -> list[Position]` | периодически (раз в 60 секунд) пересчитывает `time_to_funding` по всем открытым позициям (на основе `next_funding_time`/`funding_time` из REST-метода Bybit) и при выполнении условий `time_to_funding < 10 минут` и `UPnL < 0.3R` инициирует принудительное закрытие позиции через последовательность market reduce-only IOC-ордеров до полного обнуления размера; возвращает список позиций, для которых был инициирован выход; использует `bybit_rest_client.get_funding_rate`, `position_repository.get_open`. | Список позиций для закрытия. | `APIError` (Bybit funding API). | `bybit_rest_client.get_funding_rate`, `position_repository.get_open` |
| `generate_be_event` | метод | `async def generate_be_event(self, position: Position) -> None` | При TP1 или Mark ≥ Entry+1.0R публикует BE‑event в Redis pub/sub. | None. | `RedisError` | `redis.publish`, `notifications.webhooks.send` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `List`, `Tuple` | stdlib (`typing`) | `List`, `Tuple` | Типизация. |
| `UUID` | stdlib (`uuid`) | `UUID` | Типизация. |
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Кэш позиций. |
| `Signal`, `Position` | внутренний (`src.core.models`) | `Signal`, `Position` | DTO. |
| `PositionLimits` | внутренний (`src.risk.position_limits`) | `PositionLimits` | Проверка per‑base. |
| `AntiChurnGuard` | внутренний (`src.risk.anti_churn`) | `AntiChurnGuard` | Проверка 15‑мин блока. |
| `BybitRESTClient` | внутренний (`src.integration.bybit.rest_client`) | `BybitRESTClient` | Запрос funding rate. |

---

### 7.15. `src/risk/position_limits.py` — Per‑base лимиты

**Назначение**: Логика `MAX_POSITIONS_PER_BASE = 2` (Long+Short как хедж).

**Зона ответственности**:
- Подсчёт количества позиций по базовому активу (`BTC`, `ETH`).
- Разрешение `long + short`, блокировка `long + long` (anti‑churn обрабатывает отдельно).

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `PositionLimits` | класс | `class PositionLimits:` | Статический класс‑утилита. | None. | — | `re` (парсинг символов) |
| `get_base_asset` | метод | `@staticmethod def get_base_asset(symbol: str) -> str` | Извлекает базу из `BTCUSDT` → `BTC`. | str. | `ValueError` (неверный формат). | `re.match(r'^[A-Z]+')` |
| `count_per_base` | метод | `@staticmethod async def count_per_base(positions: List[Position], base: str) -> Tuple[int, int]` | Считает количество long/short по базе. | `(long_count, short_count)`. | — | `filter` |
| `can_open` | метод | `@staticmethod def can_open(existing: Tuple[int, int], side: Literal['long','short']) -> bool` | Проверяет лимит: если long<2 или short<2 → True. | bool. | `ValueError` (side). | — |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `re` | stdlib (`re`) | `re` | Парсинг символа. |
| `List`, `Tuple` | stdlib (`typing`) | `List`, `Tuple` | Типизация. |
| `Position` | внутренний (`src.core.models`) | `Position` | DTO. |

---

### 7.16. `src/risk/anti_churn.py` — Anti‑churn guard (15‑мин блок)

**Назначение**: Блокировка второго однонаправленного сигнала в течение 15 минут после открытия первой позиции.

**Зона ответственности**:
- Хранит `last_signal_time:{symbol}:{side}` в Redis.
- При новом сигнале проверяет разницу времени.
- Возвращает `queued` статус, если условие срабатывает.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `AntiChurnGuard` | класс | `class AntiChurnGuard:` | Статический класс. | None. | — | `Redis` (для хранения таймстемпов) |
| `is_blocked` | метод | `@staticmethod async def is_blocked(redis: Redis, symbol: str, side: Literal['long','short']) -> Tuple[bool, Optional[datetime]]` | Проверяет, прошло ли 15 минут. | `(blocked: bool, block_until: Optional[datetime])`. | `RedisError` | `redis.get` |
| `record_signal` | метод | `@staticmethod async def record_signal(redis: Redis, symbol: str, side: str, event_time: datetime) -> None` | Записывает `event_time` в Redis с TTL=15*60. | None. | `RedisError` | `redis.setex` |
| `clear_block` | метод | `@staticmethod async def clear_block(redis: Redis, symbol: str) -> None` | Сброс блока при закрытии позиции (опционально). | None. | `RedisError` | `redis.delete` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `datetime` | stdlib (`datetime`) | `datetime` | Работа с временем. |
| `Optional` | stdlib (`typing`) | `Optional` | Типизация. |
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Хранение блока. |
| `CHURN_BLOCK_SEC` | внутренний (`src.core.constants`) | `CHURN_BLOCK_SEC = 900` | Константа 15 минут. |

---

### 7.17. `src/execution/order_manager.py` — Ручное создание ордеров

**Назначение**: UI отправляет `POST /position/open` с `signal_id`, OrderManager валидирует, создаёт ордер через Bybit REST, отслеживает fill‑rate.

**Зона ответственности**:
- Никакого auto‑place без явного флага.
- Запись `fill_ratio`, `slippage_entry_bps` в `positions`.
- Валидация: позиция не может быть открыта, если сигнал уже `expired`.

**Обработка ошибок**:
- OrderManager использует `BybitErrorHandler` для обработки ошибок API и определения:
- можно ли повторить попытку (retry);
- какой текст ошибки показать пользователю;
- нужно ли шлёпнуть алерт в ops-канал.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Забисимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `OrderManager` | класс | `class OrderManager:` | Менеджер ручных ордеров. | Экземпляр. | `OrderPlacementError` | `BybitRESTClient`, `PositionRepository`, `RiskManager` |
| `place_order` | метод | `async def place_order(self, signal_id: UUID, user_id: str) -> OrderResult` | Валидирует, создаёт IOC ордер, ждёт fills ≤3с. | `OrderResult(status: Literal['filled','partial','expired'], fill_ratio: float)`. | `SignalExpiredError`, `RiskLimitExceeded` | `signal_repository.get`, `risk_manager.check_limits` |
| `validate_signal_freshness` | метод | `def validate_signal_freshness(self, signal: Signal) -> bool` | Проверяет, что с момента генерации сигнала прошло <5с (grace). | bool. | — | `datetime.utcnow` |
| `wait_for_fills` | метод | `async def wait_for_fills(self, order_id: str, timeout: float = 3.0) -> FillSummary` | Поллит Bybit REST `/v5/order/realtime` до таймаута. | `FillSummary(filled_qty, avg_price, status)`. | `TimeoutError` | `bybit_rest_client.get_order` |
| `record_fill_details` | метод | `async def record_fill_details(self, position: Position, fill_summary: FillSummary) -> None` | Сохраняет `fill_ratio`, `slippage_entry_bps` в БД. | None. | `DatabaseError` | `position_repository.update_fill` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `UUID` | stdlib (`uuid`) | `UUID` | Типизация. |
| `asyncio` | stdlib (`asyncio`) | `asyncio`, `sleep` | Ожидание fills. |
| `BybitRESTClient` | внутренний (`src.integration.bybit.rest_client`) | `BybitRESTClient` | Создание ордера. |
| `SignalRepository` | внутренний (`src.db.repositories.signal_repository`) | `SignalRepository` | Чтение сигнала. |
| `PositionRepository` | внутренний (`src.db.repositories.position_repository`) | `PositionRepository` | Запись результата. |
| `RiskManager` | внутренний (`src.risk.risk_manager`) | `RiskManager` | Проверка лимитов. |

#### Поведение при partial fill

OrderManager обязан обрабатывать частичное исполнение ордеров согласно конфигурации:

- `execution.min_fill_ratio_to_open` — минимальная доля исполнения, при которой позиция считается открытой (по умолчанию 0.5 = 50%).
- `execution.partial_fill_policy` — политика обработки диапазона `min_fill_ratio_to_open ≤ fill_ratio < 0.95`, возможные значения:
  - `"accept"` — принять частичный fill, пропорционально скорректировать TP/SL и сохранить позицию в статусе `partial_accepted`.
  - `"retry"` — отменить неисполненную часть, при необходимости попробовать добрать объём рыночным ордером до `max_retries_on_partial`.
- При `fill_ratio < min_fill_ratio_to_open` позиция считается **не открытой**:
  - отменяется неисполненная часть ордера;
  - позиция помечается статусом `failed_underfill`;
  - клиенту возвращается `OrderResult(status='underfill', fill_ratio=...)` с пояснением.
- При `fill_ratio ≥ 0.95` позиция считается полностью открытой (`status='filled'`).

Конкретные значения порогов (`min_fill_ratio_to_open`, `max_retries_on_partial`) задаются в конфигурации `settings.yaml` и должны быть согласованы с риск-менеджментом.


---

### 7.18. `src/execution/fill_tracker.py` — Отслеживание исполнения

**Назначение**: Подписка на Bybit WS `user.order`, обновление `positions` при fill, обработка частичных fills.

**Зона ответственности**:
- Не блокирующая обработка fill‑events.
- Агрегация частичных fills до `fill_ratio`.
- Запись `slippage_exit_bps` при закрытии.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `FillTracker` | класс | `class FillTracker:` | Слушает WS и обновляет позиции. | Экземпляр. | `WSError` | `BybitWSClient`, `PositionRepository` |
| `run` | метод | `async def run(self) -> None` | Бесконечный цикл: читает `user.order`, фильтрует по `order_id`. | None. | `asyncio.CancelledError` | `bybit_ws_client.subscribe_user_data` |
| `handle_fill` | метод | `async def handle_fill(self, fill_event: Dict) -> None` | Обновляет `executed_size_base`, `fill_ratio`, `slippage_entry_bps` в БД. | None. | `DatabaseError` | `position_repository.update_fill` |
| `handle_close_fill` | метод | `async def handle_close_fill(self, fill_event: Dict) -> None` | При закрытии рассчитывает `slippage_exit_bps` и финальный `pnl_usd`. | None. | `DatabaseError` | `position_repository.close`, `slippage_monitor.record_exit` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `Dict` | stdlib (`typing`) | `Dict` | Типизация. |
| `BybitWSClient` | внутренний (`src.integration.bybit.ws_client`) | `BybitWSClient` | Подписка на user‑stream. |
| `PositionRepository` | внутренний (`src.db.repositories.position_repository`) | `PositionRepository` | Запись fill‑данных. |
| `SlippageMonitor` | внутренний (`src.execution.slippage_monitor`) | `SlippageMonitor` | Расчёт slippage. |

---

### 7.19. `src/execution/slippage_monitor.py` — Мониторинг проскальзывания

**Назначение**: Расчёт `slippage_bps = (avg_fill_price / requested_price - 1) * 10000`, запись в `slippage_log`.

**Зона ответственности**:
- Для entry: `requested_price = Signal.entry_price`.
- Для exit: `requested_price = TP/SL цена`.
- Учёт ATR > p80 и depth < $1M для моделирования повышенного проскальзывания (+0.15% и +0.25% соответственно)..

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `SlippageMonitor` | класс | `class SlippageMonitor:` | Утилитарный класс. | Экземпляр. | — | `position_repository`, `config` |
| `record_entry_slippage` | метод | `async def record_entry_slippage(self, signal: Signal, fill_avg_price: Decimal) -> SlippageRecord` | Сохраняет запись в `slippage_log`. | `SlippageRecord`. | `DatabaseError` | `position_repository.insert_slippage` |
| `record_exit_slippage` | метод | `async def record_exit_slippage(self, position: Position, exit_price_requested: Decimal, exit_price_actual: Decimal) -> SlippageRecord` | Аналогично для выхода. | `SlippageRecord`. | `DatabaseError` | `position_repository.insert_slippage` |
| `adjust_for_atr` | метод | `def adjust_for_atr(self, base_slippage_bps: float, atr_percentile: float) -> float` | Если `atr > p80` → +0.15% (§2.4). | float. | `ValueError` (процентиль вне [0,1]). | `config.atr_percentile_threshold` |
| `adjust_for_depth` | метод | `def adjust_for_depth(self, base_slippage_bps: float, depth_usd: float) -> float` | Если `depth < $1M` → +0.25%. | float. | `ValueError` (depth <0). | `config.depth_threshold_usd` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `Decimal` | stdlib (`decimal`) | `Decimal` | Точность цен. |
| `Optional` | stdlib (`typing`) | `Optional` | Типизация. |
| `Signal`, `Position`, `SlippageRecord` | внутренний (`src.core.models`) | `Signal`, `Position`, `SlippageRecord` | DTO. |
| `PositionRepository` | внутренний (`src.db.repositories.position_repository`) | `PositionRepository` | Запись slippage. |

---

### 7.20. `src/notifications/webhooks.py` — Внешние уведомления (BE‑events)

**Назначение**: Отправка POST‑запроса на настроенный веб‑хук (например, Telegram Bot API) при BE‑событии.

**Зона ответственности**:
- Конфигурируемый URL, timeout, retry‑политика.
- Формат JSON: `{ "event": "be_triggered", "position_id": "...", "symbol": "BTCUSDT", "at": "2025-01-01T00:00:00Z" }`.
- Подпись HMAC‑SHA256 для верификации.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `WebhookNotifier` | класс | `class WebhookNotifier:` | Асинхронный клиент веб‑хуков. | Экземпляр. | `WebhookError` | `aiohttp.ClientSession`, `hmac` |
| `send_be_event` | метод | `async def send_be_event(self, position: Position, be_price: Decimal) -> None` | POST на `config.webhook_be_url`. | None. | `WebhookTimeout`, `WebhookHTTPError` | `aiohttp.post`, `self._sign_payload` |
| `_sign_payload` | метод | `def _sign_payload(self, payload: Dict) -> str` | Считает HMAC‑SHA256 с секретным ключом. | hex‑signature. | `ValueError` (пустой секрет). | `hmac.new`, `hashlib.sha256` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `aiohttp` | внешний (`aiohttp`) | `ClientSession` | HTTP‑клиент. |
| `hmac`, `hashlib` | stdlib (`hmac`, `hashlib`) | `hmac`, `hashlib.sha256` | Подпись. |
| `Dict` | stdlib (`typing`) | `Dict` | Типизация. |
| `Position` | внутренний (`src.core.models`) | `Position` | DTO. |
| `logger` | внутренний (`src.core.logging_config`) | `logger` | Логирование ошибок. |

---

### 7.21. `src/notifications/ui_notifier.py` — Уведомления в UI (SSE)

**Назначение**: Публикация событий в Redis pub/sub, которые транслируются в Server‑Sent Events endpoint.

**Зона ответственности**:
- Каналы: `ui:signals`, `ui:be`, `ui:metrics`, `ui:kill_switch`.
- Формат: `{ "event": "be_triggered", "data": {...}, "timestamp": ... }`.
- Поддержка reconnect с `last_event_id`.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `UINotifier` | класс | `class UINotifier:` | Паблишер для SSE. | Экземпляр. | `RedisError` | `Redis` |
| `publish_signal` | метод | `async def publish_signal(self, signal: Signal) -> None` | Публикует в `ui:signals`. | None. | `RedisError` | `redis.publish` |
| `publish_be_event` | метод | `async def publish_be_event(self, position_id: UUID, be_price: Decimal) -> None` | Публикует в `ui:be`. | None. | `RedisError` | `redis.publish` |
| `publish_kill_switch` | метод | `async def publish_kill_switch(self, enabled: bool) -> None` | Публикует в `ui:kill_switch`. | None. | `RedisError` | `redis.publish` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Pub/sub. |
| `UUID` | stdlib (`uuid`) | `UUID` | Типизация. |
| `Signal` | внутренний (`src.core.models`) | `Signal` | DTO. |

---

### 7.22. `src/monitoring/metrics.py` — Prometheus метрики

**Назначение**: Определение и обновление Counter, Gauge, Histogram для Grafana.

**Зона ответственности**:
- Latency: `signal_generation_latency_ms`, `be_delivery_latency_ms`.
- Бизнес: `signals_generated_total`, `positions_opened_total`, `win_rate`, `profit_factor`, `max_drawdown`.
- Инфра: `ws_reconnects_total`, `rate_limit_hits_total`, `db_query_duration_ms`.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `Metrics` | класс | `class Metrics:` | Синглтон с метриками. | Экземпляр. | — | `prometheus_client` |
| `signal_latency` | метод | `def signal_latency(self, latency_ms: float) -> None` | Обновляет Histogram `signal_generation_latency_ms`. | None. | `TypeError` (latency_ms < 0). | `prometheus.Histogram.observe` |
| `be_delivery_latency` | метод | `def be_delivery_latency(self, latency_ms: float) -> None` | Обновляет Histogram `be_delivery_latency_ms`. | None. | `TypeError` (latency_ms < 0). | `prometheus.Histogram.observe` |
| `increment_signals` | метод | `def increment_signals(self, symbol: str, side: str) -> None` | Увеличивает Counter `signals_generated_total` с лейблами. | None. | `ValueError` (side не в ['long','short']). | `prometheus.Counter.labels().inc` |
| `set_win_rate` | метод | `def set_win_rate(self, window_days: int, value: float) -> None` | Устанавливает Gauge `win_rate_last_{window_days}d`. | None. | `ValueError` (value вне [0,1]). | `prometheus.Gauge.set` |
| `set_profit_factor` | метод | `def set_profit_factor(self, window_days: int, value: float) -> None` | Устанавливает Gauge `profit_factor_last_{window_days}d`. | None. | `ValueError` (value < 0). | `prometheus.Gauge.set` |
| `set_max_drawdown` | метод | `def set_max_drawdown(self, value_pct: float) -> None` | Устанавливает Gauge `max_drawdown_pct`. | None. | `ValueError` (value_pct < 0). | `prometheus.Gauge.set` |
| `increment_ws_reconnects` | метод | `def increment_ws_reconnects(self, channel: str) -> None` | Увеличивает Counter `ws_reconnects_total`. | None. | `ValueError` (пустой channel). | `prometheus.Counter.labels().inc` |
| `increment_rate_limit_hits` | метод | `def increment_rate_limit_hits(self, endpoint: str) -> None` | Увеличивает Counter `rate_limit_hits_total`. | None. | `ValueError` (пустой endpoint). | `prometheus.Counter.labels().inc` |
| `db_query_duration` | метод | `def db_query_duration(self, query_name: str, duration_ms: float) -> None` | Обновляет Histogram `db_query_duration_ms`. | None. | `TypeError` (duration_ms < 0). | `prometheus.Histogram.labels().observe` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `prometheus_client` | внешний (`prometheus_client`) | `Counter`, `Gauge`, `Histogram` | Определение метрик. |
| `Dict` | stdlib (`typing`) | `Dict` | Типизация labels. |

---

### 7.23. `src/monitoring/alerts.py` — Kill‑switch логика и алерты

**Назначение**: Периодический polling метрик, проверка условий kill‑switch, отправка алертов в Telegram/Slack.

**Зона ответственности**:
- Интервал polling: 60 секунд (настраивается).
- Условия: `Net Expectancy < -0.1R` (ALERT), `MaxDD > 25%` 3 дня подряд (KILL).
- Алерты через `WebhookNotifier` (Telegram Bot API).
- Блокировка генерации сигналов: установка флага `kill_switch:active` в Redis.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `AlertManager` | класс | `class AlertManager:` | Управляет алертами и kill‑switch. | Экземпляр. | `AlertDeliveryError` | `Metrics`, `PositionRepository`, `Redis`, `WebhookNotifier` |
| `run_loop` | метод | `async def run_loop(self) -> None` | Запускает бесконечный цикл проверок. | None. | `asyncio.CancelledError` | `asyncio.sleep` |
| `check_kill_conditions` | метод | `async def check_kill_conditions(self) -> Tuple[bool, str]` | Проверяет Net Expectancy и MaxDD. | `(should_kill: bool, reason: str)`. | `DatabaseError` (чтение позиций). | `position_repository.get_last_30d_pnl`, `metrics.get_max_dd` |
| `check_alert_conditions` | метод | `async def check_alert_conditions(self) -> List[Alert]` | Проверяет WARN‑условия (WR < 55%, PF < 1.5). | `List[Alert]`. | `DatabaseError` | `metrics.get_win_rate`, `metrics.get_pf` |
| `send_alert` | метод | `async def send_alert(self, alert: Alert) -> None` | Отправляет через `WebhookNotifier`. | None. | `WebhookError` | `webhook_notifier.send` |
| `engage_kill_switch` | метод | `async def engage_kill_switch(self) -> None` | Устанавливает флаг в Redis, публикует в `ui:kill_switch`, логирует. | None. | `RedisError` | `redis.set`, `ui_notifier.publish_kill_switch` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `asyncio` | stdlib (`asyncio`) | `asyncio` | Цикл проверок. |
| `List`, `Tuple` | stdlib (`typing`) | `List`, `Tuple` | Типизация. |
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Kill‑switch флаг. |
| `Metrics` | внутренний (`src.monitoring.metrics`) | `Metrics` | Получение метрик. |
| `PositionRepository` | внутренний (`src.db.repositories.position_repository`) | `PositionRepository` | Чтение PnL. |
| `WebhookNotifier` | внутренний (`src.notifications.webhooks`) | `WebhookNotifier` | Отправка алертов. |
| `UINotifier` | внутренний (`src.notifications.ui_notifier`) | `UINotifier` | UI уведомление. |

---

### 7.24. `src/db/connection.py` — Управление пулом PostgreSQL

**Назначение**: Создание и хранение глобального пула `asyncpg.Pool`, обработка reconnect.

**Зона ответственности**:
- Пул настроен на `max_size=20`, `min_size=5`.
- Health‑check запросом `SELECT 1`.
- Graceful shutdown: закрытие пула.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `init_pool` | функция | `async def init_pool(dsn: str) -> asyncpg.Pool` | Создаёт пул подключений к PostgreSQL. | `asyncpg.Pool`. | `ConnectionError` (невозможно подключиться). | `asyncpg.create_pool` |
| `get_pool` | функция | `def get_pool() -> asyncpg.Pool` | Возвращает существующий пул (singleton). | `asyncpg.Pool`. | `RuntimeError` (пул не инициализирован). | `globals()['__pg_pool']` |
| `close_pool` | функция | `async def close_pool() -> None` | Закрывает все соединения в пуле. | None. | `RuntimeError` (пул уже закрыт). | `pool.close()` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `asyncpg` | внешний (`asyncpg`) | `asyncpg`, `Pool` | Асинхронный драйвер PostgreSQL. |
| `logger` | внутренний (`src.core.logging_config`) | `logger` | Логирование ошибок подключения. |

---

### 7.25. `src/db/migrations.py` — Управление Alembic миграциями

**Назначение**: Программный запуск upgrade/downgrade миграций при старте приложения (опционально).

**Зона ответственности**:
- Вызов `alembic.command.upgrade(config, "head")`.
- Проверка текущей ревизии.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `run_migrations` | функция | `async def run_migrations(alembic_ini_path: str = "alembic.ini") -> None` | Запускает Alembic upgrade до head. | None. | `MigrationError` (ошибка в миграции). | `alembic.config.Config`, `alembic.command.upgrade` |
| `get_current_revision` | функция | `def get_current_revision(alembic_ini_path: str) -> Optional[str]` | Возвращает текущую ревизию БД. | str (rev hash) или None. | `FileNotFoundError` (нет alembic.ini). | `alembic.script.ScriptDirectory` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `alembic.config` | внешний (`alembic`) | `Config` | Загрузка alembic.ini. |
| `alembic.command` | внешний (`alembic`) | `upgrade` | Запуск миграций. |
| `Optional` | stdlib (`typing`) | `Optional` | Типизация. |
| `logger` | внутренний (`src.core.logging_config`) | `logger` | Логирование миграций. |

---

### 7.26. `src/db/repositories/signal_repository.py` — CRUD для сигналов

**Назначение**: Типизированный доступ к таблице `signals`: вставка, выборка, фильтрация, обновление статусов.

**Зона ответственности**:
- Использование `asyncpg` с prepared statements.
- Маппинг строк в `Signal` DTO.
- Пагинация и фильтры (symbol, p_threshold).

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `SignalRepository` | класс | `class SignalRepository:` | Репозиторий сигналов. | Экземпляр. | `DatabaseError` | `asyncpg.Pool` |
| `insert` | метод | `async def insert(self, signal: Signal) -> UUID` | Вставляет новый сигнал, возвращает id. | `signal.id`. | `UniqueViolationError` (дубликат). | `pool.execute` |
| `get_by_id` | метод | `async def get_by_id(self, signal_id: UUID) -> Optional[Signal]` | Возвращает сигнал по ID. | `Signal` или None. | `DatabaseError` | `pool.fetchrow` |
| `get_active` | метод | `async def get_active(self, symbol: Optional[str] = None, p_threshold: float = 0.5, limit: int = 50, offset: int = 0) -> List[Signal]` | Возвращает активные (не `expired`) сигналы. | List[Signal]. | `DatabaseError` | `pool.fetch` |
| `update_status` | метод | `async def update_status(self, signal_id: UUID, status: str) -> None` | Обновляет `final_status`. | None. | `RecordNotFoundError` (no such id). | `pool.execute` |
| `get_for_calibration` | метод | `async def get_for_calibration(self, start_date: datetime, end_date: datetime) -> List[Signal]` | Возвращает сигналы за период для калибровки. | List[Signal]. | `DatabaseError` | `pool.fetch` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `UUID` | stdlib (`uuid`) | `UUID` | Типизация. |
| `List`, `Optional` | stdlib (`typing`) | `List`, `Optional` | Типизация. |
| `asyncpg` | внешний (`asyncpg`) | `asyncpg` | Исключения БД. |
| `Signal` | внутренний (`src.core.models`) | `Signal` | DTO. |
| `get_pool` | внутренний (`src.db.connection`) | `get_pool` | Пул. |
| `logger` | внутренний (`src.core.logging_config`) | `logger` | Логирование ошибок. |

---

### 7.27. `src/db/repositories/position_repository.py` — CRUD для позиций

**Назначение**: Ведение `positions` открытых и закрытых, заполнение fill‑ratio, slippage, funding, PnL.

**Зона ответственности**:
- Атомарные операции: обновление fill‑данных, закрытие, расчёт финального PnL.
- Поддержка `SELECT ... FOR UPDATE` для предотвращения race condition.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `PositionRepository` | класс | `class PositionRepository:` | Репозиторий позиций. | Экземпляр. | `DatabaseError` | `asyncpg.Pool` |
| `insert` | метод | `async def insert(self, position: Position) -> UUID` | Вставляет новую позицию (при открытии). | `position.id`. | `ForeignKeyViolationError` (нет signal_id). | `pool.execute` |
| `update_fill` | метод | `async def update_fill(self, position_id: UUID, executed_size: Decimal, fill_ratio: float, slippage_bps: float) -> None` | Обновляет fill‑данные. | None. | `RecordNotFoundError` | `pool.execute` |
| `close` | метод | `async def close(self, position_id: UUID, exit_price: Decimal, final_status: str, pnl_usd: float, fees_usd: float, funding_usd: float) -> None` | Закрывает позицию, считает final PnL. | None. | `RecordNotFoundError` | `pool.execute` |
| `get_open` | метод | `async def get_open(self, user_id: Optional[str] = None) -> List[Position]` | Возвращает открытые позиции (fill_ratio ≥50%). | List[Position]. | `DatabaseError` | `pool.fetch` |
| `get_closed_pnl_last_30d` | метод | `async def get_closed_pnl_last_30d(self) -> float` | Σ PnL за последние 30 дней. | float. | `DatabaseError` | `pool.fetchval` |
| `get_win_rate` | метод | `async def get_win_rate(self, days: int) -> float` | WR = TP1/TP2/TP3 hits / total closed. | float. | `DatabaseError` | `pool.fetchval` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `UUID` | stdlib (`uuid`) | `UUID` | Типизация. |
| `List`, `Optional` | stdlib (`typing`) | `List`, `Optional` | Типизация. |
| `Decimal` | stdlib (`decimal`) | `Decimal` | Точность цен. |
| `asyncpg` | внешний (`asyncpg`) | `asyncpg` | Исключения. |
| `Position` | внутренний (`src.core.models`) | `Position` | DTO. |
| `get_pool` | внутренний (`src.db.connection`) | `get_pool` | Пул БД. |
| `logger` | внутренний (`src.core.logging_config`) | `logger` | Логирование. |

---

### 7.28. `src/db/repositories/metrics_repository.py` — Чтение метрик для Grafana

**Назначение**: Оптимизированные запросы для дашбордов: WR, PF, MaxDD, slippage медиана.

**Зона ответственности**:
- Материализованные view или TimescaleDB continuous aggregates.
- Кэширование в Redis на 60 секунд.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `MetricsRepository` | класс | `class MetricsRepository:` | Репозиторий метрик. | Экземпляр. | `DatabaseError` | `asyncpg.Pool`, `Redis` |
| `get_win_rate_last_30d` | метод | `async def get_win_rate_last_30d(self) -> float` | WR = (TP1+TP2+TP3) / total (closed). | float. | `DatabaseError` | `pool.fetchval` |
| `get_profit_factor_last_30d` | метод | `async def get_profit_factor_last_30d(self) -> float` | PF = gross_profit / gross_loss. | float. | `DatabaseError` | `pool.fetchval` |
| `get_max_drawdown_last_30d` | метод | `async def get_max_drawdown_last_30d(self) -> float` | MaxDD от peak equity. | float. | `DatabaseError` | `pool.fetchval` |
| `get_median_slippage_last_24h` | метод | `async def get_median_slippage_last_24h(self) -> float` | Медиана slippage_entry_bps. | float. | `DatabaseError` | `pool.fetchval` |
| `refresh_cache` | метод | `async def refresh_cache(self) -> None` | Сбрасывает Redis‑кэш метрик. | None. | `RedisError` | `redis.delete(metrics:*)` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `Redis` | внешний (`redis.asyncio`) | `Redis` | Кэширование. |
| `get_pool` | внутренний (`src.db.connection`) | `get_pool` | Пул БД. |

---

### 7.29. `src/integration/bybit/ws_client.py` — Bybit WebSocket клиент

**Назначение**: Управление WS‑соединениями, подписки, автоматический reconnect, обработка U/D/U.

**Зона ответственности**:
- Поддержка публичных (kline, orderbook) и приватных (user.order) каналов.
- Reconnect с экспоненциальным бэкоффом (200ms → 3s, + jitter).
- Восстановление подписок после reconnect.
- Gap detection: если `sequence_id` разрывается, делает REST snapshot.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `BybitWSClient` | класс | `class BybitWSClient:` | WS‑клиент Bybit. | Экземпляр. | `WSAuthError`, `WSConnectionError` | `aiohttp.ClientSession`, `RateLimiterBybit` |
| `connect` | метод | `async def connect(self) -> None` | Устанавливает WS‑соединение, шлёт auth (для private). | None. | `WSTimeoutError` (таймаут 5с). | `session.ws_connect` |
| `subscribe` | метод | `async def subscribe(self, channel: str, params: Dict) -> None` | Отправляет SUBSCRIBE сообщение. | None. | `WSRateLimitError` (превышен лимит подписок). | `rate_limiter.consume_ws_subscription` |
| `listen` | метод | `async def listen(self) -> AsyncGenerator[WSMessage, None]` | Генератор входящих сообщений. | `WSMessage` (data, channel, sequence). | `WSConnectionClosed` | `ws.receive` |
| `handle_reconnect` | метод | `async def handle_reconnect(self) -> None` | Логика reconnect + резинк. | None. | `MaxReconnectAttemptsExceeded` (>5 попыток). | `backoff_sleep`, `rest_client.resync_snapshot` |
| `resync_snapshot` | метод | `async def resync_snapshot(self, channel: str) -> None` | REST‑запрос для получения актуального snapshot. | None. | `HTTPError` (5xx). | `bybit_rest_client.get_kline_snapshot`, `get_orderbook_snapshot` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `aiohttp` | внешний (`aiohttp`) | `ClientSession`, `WSMessage` | WS‑клиент. |
| `asyncio` | stdlib (`asyncio`) | `asyncio`, `sleep` | Reconnect бэкофф. |
| `Dict`, `AsyncGenerator` | stdlib (`typing`) | `Dict`, `AsyncGenerator` | Типизация. |
| `RateLimiterBybit` | внутренний (`src.integration.bybit.rate_limiter`) | `RateLimiterBybit` | Контроль лимитов подписок. |
| `BybitRESTClient` | внутренний (`src.integration.bybit.rest_client`) | `BybitRESTClient` | Snapshot fallback. |
| `logger` | внутренний (`src.core.logging_config`) | `logger` | Логирование reconnect. |

---

### 7.30. `src/integration/bybit/rest_client.py` — Bybit REST клиент

**Назначение**: Обертка над Bybit REST API v5: ордера, позиции, funding, kline snapshots.

**Зона ответственности**:
- Автоматическая подпись запросов (HMAC SHA256).
- Rate limiting через `RateLimiterBybit`.
- Обработка 429/5xx с exponential backoff.
- Поддержка `POST /v5/order/create`, `GET /v5/market/kline`, `GET /v5/market/orderbook`, `GET /v5/position/closed-pnl`.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `BybitRESTClient` | класс | `class BybitRESTClient:` | REST‑клиент. | Экземпляр. | `APIKeyInvalidError` | `aiohttp.ClientSession`, `RateLimiterBybit` |
| `create_order` | метод | `async def create_order(self, symbol: str, side: str, qty: float, price: float, order_type: str = "Limit", time_in_force: str = "IOC") -> Dict` | Создаёт ордер. | `{ "orderId": str, "status": str }`. | `OrderRejectedError` (insufficient balance). | `rate_limiter.consume_order`, `self._sign_request` |
| `get_kline_snapshot` | метод | `async def get_kline_snapshot(self, symbol: str, interval: str, limit: int = 200) -> List[Dict]` | REST‑запрос kline (для resync). | List of raw kline dicts. | `HTTPError` (5xx). | `rate_limiter.consume_read`, `session.get` |
| `get_orderbook_snapshot` | метод | `async def get_orderbook_snapshot(self, symbol: str, depth: int = 50) -> Dict` | REST‑запрос orderbook. | `{ "bids": [], "asks": [] }`. | `HTTPError` (5xx). | `session.get` |
| `get_closed_pnl` | метод | `async def get_closed_pnl(self, symbol: Optional[str] = None, start_time: int = None, end_time: int = None) -> List[Dict]` | История закрытых PnL (для funding). | List of PnL items. | `HTTPError` (5xx). | `session.get`, `self._sign_request` |
| `_sign_request` | метод | `def _sign_request(self, params: Dict) -> str` | Считает HMAC‑SHA256 подпись. | signature string. | `ValueError` (пустой API‑ключ). | `hmac.new`, `hashlib.sha256` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `aiohttp` | внешний (`aiohttp`) | `ClientSession` | HTTP‑клиент. |
| `hmac`, `hashlib`, `time` | stdlib (`hmac`, `hashlib`, `time`) | `hmac`, `hashlib.sha256`, `time` | Подпись запросов. |
| `Dict`, `Optional` | stdlib (`typing`) | `Dict`, `Optional` | Типизация. |
| `RateLimiterBybit` | внутренний (`src.integration.bybit.rate_limiter`) | `RateLimiterBybit` | Контроль лимитов. |
| `logger` | внутренний (`src.core.logging_config`) | `logger` | Логирование. |

---

### 7.31. `src/integration/bybit/rate_limiter.py` — Token bucket rate limiter

**Назначение**: Реализация лимитов Bybit: read 1200 req/min, order 10 req/sec, ws subscriptions 300.

**Зона ответственности**:
- Асинхронный token bucket с пополнением каждую секунду.
- Отдельные бакеты для каждой категории.
- Ожидание (backoff) при исчерпании токенов, jitter.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `RateLimiterBybit` | класс | `class RateLimiterBybit:` | Асинхронный rate limiter. | Экземпляр. | `RateLimitExceededError` | `asyncio.Lock`, `time.monotonic` |
| `consume_read` | метод | `async def consume_read(self, n: int = 1) -> None` | Потребляет n токенов из read‑bucket. | None. | `RateLimitTimeoutError` (ожидание >5с). | `self._wait_for_tokens` |
| `consume_order` | метод | `async def consume_order(self) -> None` | Потребляет 1 токен из order‑bucket. | None. | `RateLimitTimeoutError` (ожидание >3с). | `self._wait_for_tokens` |
| `consume_ws_subscription` | метод | `async def consume_ws_subscription(self) -> None` | Потребляет 1 токен из ws‑bucket. | None. | `WSRateLimitError` (30 подписок/сек). | `self._wait_for_tokens` |
| `_wait_for_tokens` | метод | `async def _wait_for_tokens(self, bucket: str, needed: int) -> None` | Ожидает, пока в bucket не будет достаточно токенов. | None. | `asyncio.TimeoutError` | `asyncio.sleep` (jitter) |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `asyncio` | stdlib (`asyncio`) | `asyncio`, `Lock`, `sleep` | Асинхронность. |
| `time` | stdlib (`time`) | `monotonic` | Тайминг пополнения. |
| `random` | stdlib (`random`) | `random.uniform` | Jitter. |

---

### 7.32. `frontend/static/js/main.js` — Инициализация UI (Vanilla JS)

**Назначение**: Точка входа: подключение SSE, инициализация рендерера плиток, настройка фильтров.

**Зона ответственности**:
- Подключение к `/stream` с обработкой reconnect.
- Обработка событий: `signal`, `be`, `kill_switch`, `metrics`.
- Глобальная конфигурация: stake, prob_threshold, selected_symbols.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости (внешние / модули) |
|---------|-----|-----------|------------------|-----------------------|-------------------|-------------------------------|
| `initApp` | функция | `function initApp()` | Главная функция: вызывает `connectSSE()`, `loadTiles()`, `setupFilters()`. | None. | `Error` (если браузер не поддерживает SSE). | `connectSSE`, `TileRenderer`, `FilterController` |
| `connectSSE` | функция | `function connectSSE()` | Создаёт `EventSource('/stream')`, обрабатывает `onmessage`, `onerror`, `onopen`. | None. | `EventSource` error (нет авторизации). | `EventSource` (браузер API) |
| `handleSSEMessage` | функция | `function handleSSEMessage(event)` | Распределяет сообщения по типу: `signal` → `TileRenderer.add()`, `be` → `updateBE()`, etc. | None. | `JSON.parse` error (malformed JSON). | `JSON.parse` |
| `globalConfig` | объект | `const globalConfig = { stake: 25, probThreshold: 0.55, mode: 'conservative', selectedSymbols: new Set() }` | Глобальное состояние UI. | Объект. | — | — |

---

### 7.33. `frontend/static/js/api.js` — API клиент

**Назначение**: Fetch‑wrapper для REST‑запросов: `/signals`, `/positions`, `/config`.

**Зона ответственности**:
- Обработка авторизации (Bearer token из localStorage).
- Обработка ошибок (401, 403, 429).
- Debounce для частых запросов.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости |
|---------|-----|-----------|------------------|-----------------------|-------------------|-------------|
| `APIClient` | класс | `class APIClient { constructor(baseURL, getToken) }` | Клиент для REST API. | Экземпляр. | `TypeError` (baseURL не строка). | `fetch` |
| `getSignals` | метод | `async getSignals(filters) { ... }` | GET `/signals` с query параметрами. | `Promise<Signal[]>`. | `HTTPError` (status >=400). | `fetch` |
| `postClosePosition` | метод | `async postClosePosition(positionId, reason) { ... }` | POST `/positions/close`. | `Promise<{ positionId, pnl }>`. | `HTTPError` (403). | `fetch` |
| `getConfig` | метод | `async getConfig() { ... }` | GET `/config`. | `Promise<AppConfig>`. | `HTTPError` (401). | `fetch` |
| `debounce` | функция | `function debounce(fn, delay) { ... }` | Утилита debounce. | Функция‑обёртка. | — | `setTimeout` |

---

### 7.34. `frontend/static/js/tiles.js` — Рендеринг плиток сигналов

**Назначение**: Virtual scrolling, IntersectionObserver для экономии DOM, обновление плиток при SSE‑событиях.

**Зона ответственности**:
- Рендеринг стрелки (вверх/вниз), уровней, Prob, BE‑статус, таймера до funding.
- Обработка клика → открытие модала.
- Обработка anti‑churn метки "QUEUE".

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости |
|---------|-----|-----------|------------------|-----------------------|-------------------|-------------|
| `TileRenderer` | класс | `class TileRenderer { constructor(container) }` | Управляет DOM‑плитками. | Экземпляр. | `Error` (контейнер не найден). | `IntersectionObserver`, `globalConfig` |
| `addTile` | метод | `addTile(signalData) { ... }` | Создаёт DOM‑элемент плитки, добавляет в контейнер. | HTMLElement. | `Error` (нет signalData.id). | `document.createElement` |
| `updateTile` | метод | `updateTile(signalId, updates) { ... }` | Обновляет существующую плитку (BE‑статус, PnL). | None. | `Error` (элемент не найден). | `document.getElementById` |
| `removeTile` | метод | `removeTile(signalId) { ... }` | Удаляет плитку (при expired). | None. | — | `element.remove()` |
| `renderQueueBadge` | метод | `renderQueueBadge(signalId) { ... }` | Добавляет визуальную плашку "QUEUE". | None. | — | `CSS class` |

---

### 7.35. `frontend/static/js/modal.js` — Модальное окно с уровнями

**Назначение**: Показ деталей сигнала: таблица с Entry, TP1‑3, SL, R‑множитель, кнопка "Copy Levels".

**Зона ответственности**:
- Позиционирование окна (центр экрана).
- Копирование уровней в clipboard.
- Показ BE‑статуса и прогресса калибровки.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости |
|---------|-----|-----------|------------------|-----------------------|-------------------|-------------|
| `ModalController` | класс | `class ModalController { constructor() }` | Управляет модальным окном. | Экземпляр. | `Error` (элемент #modal не найден). | `navigator.clipboard` |
| `open` | метод | `open(signalData) { ... }` | Открывает модал, заполняет данные. | None. | `TypeError` (signalData не объект). | `document.getElementById` |
| `close` | метод | `close() { ... }` | Закрывает модал. | None. | — | `element.classList.add('hidden')` |
| `copyLevels` | метод | `async copyLevels() { ... }` | Копирует строку "Entry: X, TP1: Y, TP2: Z, TP3: W, SL: V" в clipboard. | `Promise<void>`. | `NotAllowedError` (нужен user gesture). | `navigator.clipboard.writeText` |
| `renderCalibrationProgress` | метод | `renderCalibrationProgress(daysDone, totalDays) { ... }` | Показывает прогрессбар "калибровка: X/30 дней". | None. | — | `CSS width` |

---

### 7.36. `docker/Dockerfile` — Мульти‑стейдж сборка

**Назначение**: Production‑ready Docker‑image: сборка Python зависимостей, копирование кода, запуск Uvicorn.

**Зона ответственности**:
- Стадия `builder`: устанавливает Poetry, копирует `pyproject.toml`, устанавливает зависимости.
- Стадия `runtime`: копирует виртуальное окружение, код, запускает `uvicorn src.main:app`.
- USER `nobody` для безопасности.

#### Таблица инструкций

| Инструкция | Описание |
|------------|----------|
| `FROM python:3.11-slim as builder` | Базовый образ для сборки. |
| `RUN pip install poetry` | Установка Poetry. |
| `COPY pyproject.toml poetry.lock ./` | Копирование зависимостей. |
| `RUN poetry export -f requirements.txt -o requirements.txt` | Генерация requirements. |
| `RUN pip install --user -r requirements.txt` | Установка в локальное окружение. |
| `FROM python:3.11-slim as runtime` | Финальный образ. |
| `COPY --from=builder /root/.local /root/.local` | Копирование установленных пакетов. |
| `COPY . /app` | Копирование кода. |
| `WORKDIR /app` | Рабочая директория. |
| `CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]` | Команда запуска. |

---

### 7.37. `docker/docker-compose.yml` — Local dev stack

**Назначение**: Запуск PostgreSQL, Redis, приложения вместе для локальной разработки.

**Зона ответственности**:
- Сервисы: `db` (PG 15), `redis` (Redis 7), `app` (с билдом Dockerfile).
- Прокидывание портов: `5432:5432`, `6379:6379`, `8000:8000`.
- Подключение через `depends_on`.

---

### 7.38. `pyproject.toml` — Poetry зависимости и скрипты

**Назначение**: Декларация зависимостей: `fastapi`, `asyncpg`, `redis`, `alembic`, `prometheus-client`, `structlog`, `pydantic`, `aiohttp`, `apscheduler`.

**Зона ответственности**:
- Группы dev‑зависимостей: `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
- Poetry scripts: `migrate = "alembic upgrade head"`, `test = "pytest"`, `dev = "docker-compose up"`.

---

### 7.39. alembic/ — Скрипты миграций Alembic

**Назначение**: Версионирование схемы БД: создание таблиц `signals`, `positions`, `slippage_log`.

**Ключевые миграции**:
- `001_create_signals_table.py`
- `002_create_positions_table.py`
- `003_create_slippage_log_table.py`
- `004_create_timescale_hypertable_klines.py`

## таблица `signals`
-   `id              UUID PRIMARY KEY`.
-   `created_at      TIMESTAMPTZ NOT NULL DEFAULT now()`
-   `symbol          TEXT NOT NULL`.
-   `direction       TEXT CHECK (direction IN ('long', 'short'))`.
-   `entry_price     NUMERIC(18,8) NOT NULL`.
-   `stake_usd       NUMERIC(18,2) NOT NULL`.
-   `probability     NUMERIC(4,3) NOT NULL`.
-   `strategy        TEXT NOT NULL`.
-   `strategy_version VARCHAR(20) NOT NULL`           -- Версия стратегии, сгенерировавшей сигнал
-   `queued_until     TIMESTAMPTZ`                    -- До какого момента сигнал может быть в очереди
-   `error_code       INTEGER`                        -- Код ошибки при обработке (если был)
-   `error_message    TEXT`                            -- Текст ошибки для диагностики

#### Таблица `users`

- `user_id UUID PRIMARY KEY` — идентификатор пользователя (совпадает с тем, что шьётся в JWT).
- `email TEXT UNIQUE NOT NULL` — логин.
- `role TEXT NOT NULL CHECK (role IN ('viewer', 'trader', 'admin'))` — роль для RBAC.
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` — когда пользователь создан.
- `is_active BOOLEAN NOT NULL DEFAULT TRUE` — активен ли пользователь.

#### Таблица `audit_trail`

- `id BIGSERIAL PRIMARY KEY`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `user_id UUID NOT NULL` — кто совершил действие.
- `action TEXT NOT NULL` — тип действия (`login`, `place_order`, `close_position`, `change_config` и т.п.).
- `details JSONB` — произвольные детали (symbol, size, старое/новое значение).
- Индексы по `user_id`, `created_at` для поиска.

#### Таблица `reconciliation_log`

- `id BIGSERIAL PRIMARY KEY`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `severity TEXT CHECK (severity IN ('info','warning','critical'))`
- `description TEXT NOT NULL` — что именно нашёл reconciliation.
- `details JSONB` — конкретные расхождения (ID позиций, суммы и т.п.).

#### Таблица `order_rejections`

- `id BIGSERIAL PRIMARY KEY`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `user_id UUID` — для кого пытались поставить ордер.
- `signal_id UUID` — по какому сигналу.
- `reason TEXT NOT NULL` — причина отклонения (например, лимит риска, ошибка биржи).
- `bybit_ret_code INTEGER` — код ошибки Bybit (если применимо).
- `bybit_ret_msg TEXT` — текст сообщения Bybit.



---

### 7.40. `tests/unit/test_indicators.py` — Тесты индикаторов

**Назначение**: Юнит‑тесты `calculate_vwap`, `calculate_atr_ema` с фиксированными данными.

---

### 7.41. `tests/unit/test_signal_engine.py` — Тесты генерации сигналов

**Назначение**: Моки `IndicatorEngine`, `RiskManager`, проверка правил генерации сигналов (Trend filter, Trigger, imbalance/microprice/spread).

---

### 7.42. `tests/unit/test_risk_manager.py` — Тесты риск‑менеджера

**Назначение**: Проверка лимитов, anti‑churn, BE‑триггера.

---

### 7.43. `tests/unit/test_rate_limiter.py` — Тесты rate limiter

**Назначение**: Проверка token bucket, jitter, timeout.

---

### 7.44. `tests/integration/test_bybit_ws.py` — Интеграционные тесты WS

**Назначение**: Поднятие mock‑сервера WS, проверка reconnect.

---

### 7.45. `tests/integration/test_order_lifecycle.py` — Жизненный цикл ордера

**Назначение**: Полный flow: сигнал → позиция → fill → close.

---

### 7.46. `tests/integration/test_sse_stream.py` — SSE streaming

**Назначение**: Проверка realtime обновлений в UI.

---

### 7.47. `config/settings.yaml` — Конфигурация

**Содержание**: `trading.max_stake: 100`, `risk.max_concurrent: 5`, `bybit.api_key: "${BYBIT_API_KEY}"`. 
Используется для локальной разработки и sandbox-окружений; в продакшене реальные торговые ключи клиентов не задаются через этот файл, а читаются из внешнего секрет-хранилища (Vault) согласно разделу 3.

---

### 7.48. `config/secrets.env.example` — Шаблон секретов

**Содержание**: `BYBIT_API_KEY=your_key_here`, `BYBIT_SECRET=your_secret`, `JWT_SECRET=your_jwt_secret`. 
Файл предназначен только для примеров и локального/dev-запуска; в боевом контуре реальные ключи хранятся во внешнем Vault, а не в `.env`.
---

### 7.49. `config/schema.py` — Pydantic схемы валидации YAML

**Зона ответственности**: `AppConfig`, `TradingConfig`, `RiskConfig`, `BybitConfig`.

---

### 7.50. `README.md` — Quick start

**Содержание**: `pip install poetry`, `poetry install`, `docker-compose up`, `alembic upgrade head`, `uvicorn src.main:app --reload`.

---

### 7.51. `.gitignore` — Правила Git

**Содержание**: `__pycache__/`, `*.pyc`, `.env`, `logs/*.jsonl`, `frontend/static/node_modules/`.

---

### 7.52. `.dockerignore` — Правила Docker

**Содержание**: `.git/`, `tests/`, `docs/`, `frontend/static/node_modules/`, `logs/`.

---

### 7.53. `.github/workflows/ci.yml` — GitHub Actions

**Содержание**: `on: [push]`, jobs: `lint` (ruff), `test` (pytest), `build` (docker build).

**ДОПОЛНЕНИЕ: Полное табличное описание для всех оставшихся файлов (формальные требования аудита)**

---

### 7.54. `src/core/constants.py` — Глобальные константы системы

**Назначение**: Централизованное хранение строковых и числовых литералов, URL‑адресов, лимитов, таймаутов.

**Зона ответственности**:
- Только константы, без логики или функций.
- Используется во всех модулях для избежания хардкода.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| (отсутствуют) | — | — | — | — | — | — |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| (нет импортов) | — | — | — |

#### Содержание файла (для справки)
```python
WS_PUBLIC_URL = "wss://stream.bybit.com/v5/public/linear"
WS_PRIVATE_URL = "wss://stream.bybit.com/v5/private"
MAX_WS_SUBSCRIPTIONS = 300
RATE_LIMIT_READ_PER_MIN = 1200
RATE_LIMIT_ORDER_PER_SEC = 10
CHURN_BLOCK_SEC = 900
BE_DELIVERY_P95_TARGET_MS = 5000
CONFIRM_LATENCY_P95_TARGET_MS = 5000
```

---

### 7.55. `src/core/exceptions.py` — Иерархия кастомных исключений

**Назначение**: Базовый класс и конкретные исключения для всех бизнес‑ и инфра‑ошибок системы.

**Зона ответственности**:
- Наследование от `AlgoGridBaseException` для возможности централизованной обработки.
- Каждое исключение передаёт сообщение и опционально `details` (dict).

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `AlgoGridBaseException` | класс | `class AlgoGridBaseException(Exception):` | Базовое исключение всей системы. | None (Exception). | Стандартное поведение `Exception`. | (inheritance) |
| `InvalidCandleError` | класс | `class InvalidCandleError(AlgoGridBaseException):` | Свеча не прошла sanity‑check (Close ∉ [Low,High] или Volume < 0 или confirm=false). | None. | — | `AlgoGridBaseException` |
| `SignalExpiredError` | класс | `class SignalExpiredError(AlgoGridBaseException):` | Сигнал устарел (>5с после закрытия бара). | None. | — | `AlgoGridBaseException` |
| `RiskLimitExceeded` | класс | `class RiskLimitExceeded(AlgoGridBaseException):` | Превышен риск‑лимит (concurrent, per‑base, total). | None. | — | `AlgoGridBaseException` |
| `OrderPlacementError` | класс | `class OrderPlacementError(AlgoGridBaseException):` | Bybit отклонил ордер (insufficient margin, invalid price). | None. | — | `AlgoGridBaseException` |
| `RateLimitExceededError` | класс | `class RateLimitExceededError(AlgoGridBaseException):` | Превышен rate limit (Bybit или IP). | None. | — | `AlgoGridBaseException` |
| `WebhookError` | класс | `class WebhookError(AlgoGridBaseException):` | Ошибка отправки webhook‑уведомления. | None. | — | `AlgoGridBaseException` |
| `DatabaseError` | класс | `class DatabaseError(AlgoGridBaseException):` | Ошибка БД (connection, query). | None. | — | `AlgoGridBaseException` |
| `ConfigLoadError` | класс | `class ConfigLoadError(AlgoGridBaseException):` | Ошибка загрузки/валидации конфигурации. | None. | — | `AlgoGridBaseException` |
| `WSConnectionError` | класс | `class WSConnectionError(AlgoGridBaseException):` | Ошибка WebSocket‑соединения. | None. | — | `AlgoGridBaseException` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| (нет импортов) | — | — | — |

---

### 7.56. `src/api/middleware/cors.py` — CORS middleware

**Назначение**: Настройка Cross‑Origin Resource Sharing для доступа UI к API.

**Зона ответственности**:
- Динамическое чтение `config.ui.allowed_origins` (список или `*`).
- Применение middleware к FastAPI приложению.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `add_cors_middleware` | функция | `def add_cors_middleware(app: FastAPI, allowed_origins: List[str]) -> None` | Регистрирует CORSMiddleware в FastAPI приложении. | None. | `TypeError` (`app` не FastAPI). | `CORSMiddleware` (Starlette) |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `FastAPI` | внешний (`fastapi`) | `FastAPI` | Типизация параметра. |
| `CORSMiddleware` | внешний (`starlette.middleware.cors`) | `CORSMiddleware` | Middleware для CORS. |
| `List` | stdlib (`typing`) | `List` | Типизация `allowed_origins`.|

---

### 7.57. `src/api/middleware/rate_limit.py` — IP‑based rate limiting (опциональный)

**Назначение**: Дополнительная защита от перегрузки API со стороны UI.

**Зона ответственности**:
- Sliding window для каждого IP.
- Лимит: 100 запросов в минуту (настраивается).

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `IPRateLimitMiddleware` | класс | `class IPRateLimitMiddleware(BaseHTTPMiddleware):` | Middleware для ограничения запросов по IP. | Экземпляр. | `ValueError` (max_requests <=0). | `BaseHTTPMiddleware`, `defaultdict`, `time` |
| `dispatch` | метод | `async def dispatch(self, request: Request, call_next: Callable) -> Response` | Проверяет лимит, возвращает 429 или пропускает запрос. | `Response` (200 или 429). | — | `call_next(request)` |
| `_cleanup_window` | метод | `def _cleanup_window(self, ip: str) -> None` | Удаляет старые timestamp из sliding window. | None. | — | `time.time()`, `self._requests[ip]` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `BaseHTTPMiddleware` | внешний (`starlette.middleware.base`) | `BaseHTTPMiddleware` | Базовый класс middleware. |
| `Request`, `Response` | внешний (`starlette.requests`, `starlette.responses`) | `Request`, `Response` | Типизация. |
| `Callable` | stdlib (`typing`) | `Callable` | Типизация `call_next`. |
| `defaultdict` | stdlib (`collections`) | `defaultdict` | Хранение запросов по IP. |
| `time` | stdlib (`time`) | `time` | Таймстемпы. |

---

### 7.58. `src/data/storage.py` — Утилита записи в БД (deprecated/вырожденный)

**Назначение**: Тривиальная обёртка `INSERT INTO klines_5m`. Вся логика перенесена в `SignalRepository` и `PositionRepository`.

**Зона ответственности**:
- Файл содержит одну функцию `save_kline`, которая вызывает `pool.execute`.
- **Рекомендация**: инлайнить в `collector.py`, удалить файл.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `save_kline` | функция | `async def save_kline(pool: asyncpg.Pool, kline: Dict) -> None` | Вставляет свечу в TimescaleDB. | None. | `DatabaseError` (duplicate). | `asyncpg.Pool.execute` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `asyncpg` | внешний (`asyncpg`) | `asyncpg` | Типизация параметра. |
| `Dict` | stdlib (`typing`) | `Dict` | Типизация `kline`. |

---

### 7.59. `src/__init__.py` — Инициализатор пакета `src`

**Назначение**: Пустой файл, обозначающий директорию как Python‑пакет.

**Зона ответственности**:
- Не содержит кода.
- **Аудит**: подтверждает структуру пакета.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| (отсутствуют) | — | — | — | — | — | — |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| (нет импортов) | — | — | — |

---

### 7.60. `src/core/__init__.py`, `src/api/__init__.py`, `src/api/routes/__init__.py`, `src/api/middleware/__init__.py`, `src/data/__init__.py`, `src/strategies/__init__.py`, `src/execution/__init__.py`, `src/risk/__init__.py`, `src/notifications/__init__.py`, `src/monitoring/__init__.py`, `src/db/__init__.py`, `src/db/repositories/__init__.py`, `src/integration/__init__.py`, `src/integration/bybit/__init__.py`

**Назначение**: Все эти файлы — пустые `__init__.py`, обозначающие иерархию пакетов.

**Зона ответственности**:
- Не содержат кода.
- **Аудит**: структурированный пакетный импорт.

#### Таблицы классов и функций (для всех `__init__.py`)

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| (отсутствуют) | — | — | — | — | — | — |

#### Таблицы импортов (для всех `__init__.py`)

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| (нет импортов) | — | — | — |

---

### 7.61. `.gitignore` — Исключения для Git

**Назначение**: Правила игнорирования файлов (кэш, логи, секреты).

**Зона ответственности**:
- Не содержит Python‑кода.
- **Аудит**: предотвращение утечек секретов.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| (отсутствуют) | — | — | — | — | — | — |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| (нет импортов) | — | — | — |

#### Содержимое файла (для справки)
```
__pycache__/
*.pyc
*.pyo
*.pyd
.env
*.env.*
logs/*.jsonl
frontend/static/node_modules/
.DS_Store
.vscode/
```

---

### 7.62. `.dockerignore` — Исключения для Docker‑build

**Назначение**: Уменьшение контекста сборки, ускорение, безопасность.

#### Таблицы

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| (отсутствуют) | — | — | — | — | — | — |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| (нет импортов) | — | — | — |

#### Содержимое файла
```
.git
.github
tests/
docs/
frontend/static/node_modules/
logs/
*.md
.env*
__pycache__
*.pyc
```

---

### 7.63. `alembic.ini` — Конфигурация Alembic

**Назначение**: INI‑файл с путями к migrations, DSN, логированием.

**Зона ответственности**:
- Не содержит Python‑кода.
- **Аудит**: контроль версий схемы БД.

#### Таблицы

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| (отсутствуют) | — | — | — | — | — | — |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| (нет импортов) | — | — | — |

---

### 7.64. `frontend/templates/index.html` — HTML‑скелет UI

**Назначение**: Single‑Page Application skeleton с подключением CSS/JS, контейнерами для плиток и модала.

**Зона ответственности**:
- Meta viewport, charset.
- Подключение Tailwind CDN (или локальный CSS).
- Подключение `main.js` (entry point).
- Контейнер `#tiles-container` и `#modal-container`.
- Панель настроек (`#settings-panel`).

#### Таблица элементов (HTML‑теги)

| Элемент | Тип | ID/Класс | Описание |
|---------|-----|----------|----------|
| `<!DOCTYPE html>` | тег | — | HTML5 документ. |
| `<head>` | блок | `<head>` | Meta, title, CSS. |
| `<body>` | блок | `<body>` | Контент. |
| `#tiles-container` | div | `id="tiles-container"` | Контейнер для плиток сигналов. |
| `#modal-container` | div | `id="modal-container"` | Контейнер для модального окна. |
| `#settings-panel` | div | `id="settings-panel"` | Слайдер stake, prob threshold, mode switch. |
| `<script src="static/js/main.js">` | скрипт | `src="static/js/main.js"` | Entry point JavaScript. |

#### Таблица импортов (CDN/локальные ресурсы)

| Ресурс | Откуда (CDN / локальный) | Для чего используется |
|--------|--------------------------|-----------------------|
| `Tailwind CSS` | CDN `https://cdn.tailwindcss.com` | Стилизация UI. |
| `main.js` | Локальный `/static/js/main.js` | Логика приложения. |
| `api.js` | Локальный `/static/js/api.js` | REST‑клиент. |
| `tiles.js` | Локальный `/static/js/tiles.js` | Рендеринг плиток. |
| `modal.js` | Локальный `/static/js/modal.js` | Модальное окно. |

---

### 7.65. `scripts/run_calibration.py` — CLI для запуска калибровки

**Назначение**: Ручной запуск `CalibrationService.run_calibration()` с аргументами командной строки.

**Зона ответственности**:
- Парсинг `--start-date`, `--end-date`, `--force`.
- Инициализация приложения (загрузка config, пул БД).
- Прогресс‑бар через `tqdm` или логи.
- Вывод результата в консоль.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `main` | функция | `def main():` | Entry point скрипта. | None (exit code 0/1). | `argparse.ArgumentError` (неверные аргументы). | `argparse.ArgumentParser`, `CalibrationService` |
| `setup_logging` | функция | `def setup_logging(verbose: bool) -> None` | Конфигурация структурированного логирования для CLI. | None. | — | `structlog`, `logging` |
| `load_config_and_pool` | функция | `async def load_config_and_pool(config_path: str) -> Tuple[AppConfig, asyncpg.Pool]` | Загружает конфиг и инициализирует пул БД. | `(config, pool)`. | `ConfigLoadError`, `ConnectionError` | `ConfigLoader`, `asyncpg.create_pool` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `argparse` | stdlib (`argparse`) | `ArgumentParser` | Парсинг CLI. |
| `asyncio` | stdlib (`asyncio`) | `asyncio.run` | Запуск асинхронного main. |
| `sys` | stdlib (`sys`) | `sys.exit` | Выход с кодом ошибки. |
| `structlog` | внешний (`structlog`) | `structlog` | Логирование. |
| `asyncpg` | внешний (`asyncpg`) | `asyncpg` | Пул БД. |
| `CalibrationService` | внутренний (`src.strategies.calibration`) | `CalibrationService` | Запуск калибровки. |
| `ConfigLoader` | внутренний (`src.core.config_loader`) | `ConfigLoader` | Загрузка конфигурации. |

---

### 7.66. `scripts/migrate.py` — CLI для Alembic миграций

**Назначение**: Запуск `alembic upgrade head` из командной строки.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `main` | функция | `def main(revision: str = "head"):` | Запускает `alembic.command.upgrade()`. | None (exit code). | `CommandError` (Alembic ошибка). | `alembic.config.Config`, `alembic.command.upgrade` |
| `setup_alembic_config` | функция | `def setup_alembic_config(ini_path: str) -> Config` | Загружает alembic.ini. | `alembic.config.Config`. | `FileNotFoundError`. | `Config` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `alembic.config` | внешний (`alembic`) | `Config` | Конфигурация Alembic. |
| `alembic.command` | внешний (`alembic`) | `upgrade` | Запуск миграций. |

---

### 7.67. `scripts/backup.py` — Резервное копирование БД

**Назначение**: Запуск `pg_dump`, архивация в `.tar`, загрузка в S3.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `main` | функция | `def main(bucket: str, retention_days: int = 30):` | Entry point: dump → tar → upload S3. | None. | `subprocess.CalledProcessError` (pg_dump failed). | `subprocess.run`, `boto3.client` |
| `run_pg_dump` | функция | `def run_pg_dump(dsn: str, output_path: str) -> None` | Выполняет `pg_dump`. | None. | `CalledProcessError` | `subprocess.run(["pg_dump", ...])` |
| `upload_to_s3` | функция | `def upload_to_s3(file_path: str, bucket: str, key: str) -> None` | Загружает в S3. | None. | `ClientError` (S3). | `boto3.client("s3").upload_file` |
| `cleanup_old_backups` | функция | `def cleanup_old_backups(bucket: str, retention_days: int) -> None` | Удаляет старые backup‑файлы в S3. | None. | `ClientError` (S3). | `boto3.client("s3").list_objects_v2`, `delete_objects` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `subprocess` | stdlib (`subprocess`) | `subprocess.run` | Выполнение pg_dump. |
| `datetime` | stdlib (`datetime`) | `datetime` | Формирование имени файла. |
| `boto3` | внешний (`boto3`) | `boto3` | S3‑клиент. |
| `botocore.exceptions` | внешний (`botocore.exceptions`) | `ClientError` | Ошибки S3. |

---

### 7.68. `scripts/restore.py` — Восстановление БД из backup

**Назначение**: Скачивание из S3, `pg_restore`.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `main` | функция | `def main(bucket: str, backup_key: str, dsn: str):` | Entry point: download → restore. | None. | `CalledProcessError` (pg_restore failed). | `boto3.client`, `subprocess.run` |
| `download_from_s3` | функция | `def download_from_s3(bucket: str, key: str, local_path: str) -> None` | Скачивает backup. | None. | `ClientError` (S3). | `boto3.client("s3").download_file` |
| `run_pg_restore` | функция | `def run_pg_restore(dsn: str, backup_path: str) -> None` | Выполняет `pg_restore`. | None. | `CalledProcessError` | `subprocess.run(["pg_restore", ...])` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `subprocess` | stdlib (`subprocess`) | `subprocess.run` | Выполнение pg_restore. |
| `boto3` | внешний (`boto3`) | `boto3` | S3‑клиент. |

---

### 7.69. `tests/conftest.py` — Pytest fixtures

**Назначение**: Переиспользуемые fixtures для всех тестов: тестовая БД, Redis, mock‑клиенты, сэмплы.

**Зона ответственности**:
- Фикстуры имеют scope `session` (для БД) и `function` (для моков).
- Автоматический rollback после каждого теста.

#### Таблица классов и функций

| Элемент | Тип | Сигнатура | Краткое описание | Возвращаемое значение | Исключения/ошибки | Зависимости внутри проекта |
|---------|-----|-----------|------------------|-----------------------|-------------------|---------------------------|
| `pytest_sessionstart` | хук | `def pytest_sessionstart(session):` | Инициализирует тестовую БД перед сессией. | None. | `ConnectionError` (не удалось подключиться к тест‑БД). | `asyncpg.connect` |
| `event_loop` | fixture | `@pytest.fixture(scope="session") def event_loop():` | Возвращает event loop для async тестов. | `asyncio.AbstractEventLoop`. | — | `asyncio.new_event_loop` |
| `test_db_pool` | fixture | `@pytest.fixture(scope="session") async def test_db_pool():` | Создаёт пул к тестовой PostgreSQL. | `asyncpg.Pool`. | `ConnectionError`. | `asyncpg.create_pool` |
| `test_redis` | fixture | `@pytest.fixture async def test_redis():` | Создаёт Redis‑клиент к тестовому инстансу. | `redis.Redis`. | `ConnectionError`. | `redis.from_url` |
| `mock_bybit_client` | fixture | `@pytest.fixture def mock_bybit_client():` | Mock для `BybitRESTClient` и `BybitWSClient`. | `MagicMock` (aiohttp‑подобный). | — | `unittest.mock.MagicMock` |
| `sample_signal` | fixture | `@pytest.fixture def sample_signal():` | Возвращает `Signal` DTO из фикстуры JSON. | `Signal`. | `ValidationError` (если JSON broken). | `Signal.parse_raw` |

#### Таблица импортов

| Модуль/пакет | Откуда (stdlib / внешний / внутренний) | Что импортируется | Для чего используется |
|--------------|----------------------------------------|-------------------|-----------------------|
| `pytest` | внешний (`pytest`) | `pytest.fixture` | Декораторы fixtures. |
| `asyncio` | stdlib (`asyncio`) | `new_event_loop` | Event loop. |
| `asyncpg` | внешний (`asyncpg`) | `create_pool` | Тест‑пул БД. |
| `redis` | внешний (`redis.asyncio`) | `from_url` | Тест‑Redis. |
| `MagicMock` | stdlib (`unittest.mock`) | `MagicMock` | Моки клиентов. |
| `Signal` | внутренний (`src.core.models`) | `Signal` | DTO для фикстур. |

---

### 7.70. `tests/fixtures/sample_kline.json` — JSON‑фикстура свечи

**Назначение**: Пример закрытой 5m‑свечи для тестов индикаторов.

**Зона ответственности**:
- Файл содержит статический JSON‑объект.
- **Аудит**: воспроизводимость тестов.

#### Таблица элементов (JSON‑структура)

| Поле | Тип | Пример | Описание |
|------|-----|--------|----------|
| `symbol` | string | `"BTCUSDT"` | Тикер. |
| `interval` | string | `"5m"` | Таймфрейм. |
| `timestamp` | int | `1704067200000` | Unix ms. |
| `open` | float | `60000.0` | Цена открытия. |
| `high` | float | `60200.0` | Максимум. |
| `low` | float | `59900.0` | Минимум. |
| `close` | float | `60100.0` | Цена закрытия. |
| `volume` | float | `1000.0` | Объём. |
| `confirm` | bool | `true` | Подтверждённая свеча. |

---

### 7.71. `tests/fixtures/sample_ob.json` — JSON‑фикстура стакана

**Назначение**: Пример L10 стакана для тестов imbalance и microprice.

#### Таблица элементов (JSON‑структура)

| Поле | Тип | Пример | Описание |
|------|-----|--------|----------|
| `symbol` | string | `"BTCUSDT"` | Тикер. |
| `timestamp` | int | `1704067200000` | Unix ms. |
| `bids` | array | `[{"price":60000,"qty":10.5}, ...]` | Массив bid‑уровней. |
| `asks` | array | `[{"price":60001,"qty":8.3}, ...]` | Массив ask‑уровней. |

---

### 7.72. `docs/api.md` — OpenAPI документация (описание REST/SSE API)

**Разделы:**

1. **Аутентификация**
   - `POST /auth/login` — приём email/пароля или другого метода логина, выдача `access_token` и `refresh_token`.
   - `POST /auth/refresh` — обновление access-токена по refresh-токену.
   - `POST /auth/logout` — выход из сессии: инвалидирует текущие access/refresh токены (через blacklist при включённом режиме).
   - Формат JWT, поля `sub` (user_id) и `role`, время жизни токенов.

2. **Работа с пользователями** 
   - `GET /users (admin)` — получение списка пользователей (доступно для admin-роли)
   - `POST /users` — создание пользователя (доступно для admin-роли) 
   - `PATCH /users/{user_id}` —  смена роли/статуса (доступно для admin-роли)

3. **Сигналы**
   - `GET /signals` — список активных сигналов.
   - Фильтры: `symbol`, `direction`, `min_probability`.
   - Формат ответа, примеры.

4. **Позиции**
   - `GET /positions` — список открытых позиций пользователя.
   - `POST /positions/{id}/close` — закрытие позиции (минимальная роль: `trader`).
   - Коды ошибок: `404` (нет позиции), `403` (нет прав), `409` (уже закрыта).

5. **Конфигурация**
   - `GET /config` — чтение read-only конфигурации.
   - `PATCH /config` — изменение части параметров (только admin), с обязательной записью в audit_trail

6. **Администрирование**
   - `POST /admin/kill_switch` — принудительное выключение торговли.
   - `GET /admin/reconciliation/status` — последние записи из `reconciliation_log`.

7. **Health & Метрики**
   - `GET /health` — проверка базы, Redis, соединения с биржей.
   - `GET /metrics` — Prometheus-метрики (latency, ошибки и т.п.).

8. **Streaming**
   - `GET /stream` (SSE) — realtime-стрим сигналов и статуса позиций.

9. **2FA:**
   - `POST /auth/2fa/setup` — генерация TOTP секрета и QR-кода. 
   - `POST /auth/2fa/confirm` — подтверждение настройки 2FA. 
   - `POST /auth/2fa/disable` — отключение 2FA (с дополнительной проверкой).
   
10. **API-ключи:**
    - `GET /api-keys` — список ключей текущего пользователя.
    - `POST /api-keys` — создание ключа (label, exchange, права). 
    - `DELETE /api-keys/{id}` — деактивация/удаление.

Все эндпоинты аннотированы ролями (viewer/trader/admin) в отдельной таблице.

#### Матрица доступа к эндпоинтам
  
  | Эндпоинт | Метод | viewer | trader | admin |
  |----------|-------|--------|--------|-------|
  | /signals | GET   | ✅ | ✅ | ✅ |
  | /positions | GET   | ✅ | ✅ | ✅ |
  | /positions/{id}/close | POST  | ❌ | ✅ | ✅ |
  | /config | GET   | ✅ | ✅ | ✅ |
  | /config | PATCH | ❌ | ❌ | ✅ |
  | /admin/kill_switch | POST  | ❌ | ❌ | ✅ |
  | /users | GET   | ❌ | ❌ | ✅ |
  | /users | POST  | ❌ | ❌ | ✅ |
  | /api-keys | GET   | ✅* | ✅* | ✅ |
  | /api-keys | POST  | ❌ | ❌ | ✅ |
  
  *только свои ключи

---

### 7.73. `docs/deployment.md` — Инструкция по деплою

**Назначение**: IaC‑документация: Docker‑compose, Kubernetes манифесты, Traefik, SSL.

#### Таблица секций

| Секция | Описание |
|--------|----------|
| `## Requirements` | CPU, RAM, PostgreSQL 15+, Redis 7+. |
| `## Docker‑compose` | Пример `docker-compose.prod.yml`. |
| `## Kubernetes` | Deployment, Service, Ingress, ConfigMap, Secret. |
| `## Monitoring` | Prometheus scrape config, Grafana datasource. |

---

### 7.74. `docs/risk_disclaimer.md` — Юридический дисклеймер

**Назначение**: Текст из §0 ТЗ в Markdown‑формате.

#### Таблица секций

| Секция | Содержание |
|--------|------------|
| `## Disclaimer` | Система не гарантирует прибыль... |
| `## Liability` | Ограничение ответственности $55,000. |
| `## Acceptance` | Место для подписей заказчика/исполнителя. |

### 7.75. `src/auth/jwt_manager.py` — Управление JWT-токенами

**Назначение**: Централизованная работа с JWT: генерация, валидация, обновление.

**Зона ответственности**:
- Генерация access- и refresh-токенов на основе `user_id`, `role` и настроек TTL.
- Валидация и декодирование JWT, маппинг ошибок в понятные исключения.
- Черный список (опционально) для принудительного логаута.
- Поддержка токенов типа access и refresh с разными TTL.
- Обработка jti и интеграция с optional blacklist (например, Redis-set).
- Логирование событий login, logout, refresh в audit_trail.
---

### 7.76. `src/auth/middleware.py` — Обёртка аутентификации FastAPI

**Назначение**: Инкапсулировать JWT-аутентификацию в переиспользуемую зависимость/класс для FastAPI.

**Зона ответственности**:
- Извлечь `Authorization: Bearer` заголовок.
- Проверить JWT через `JWTAuthManager`.
- Пробросить в `request.state` или `Depends` объект `CurrentUser` (id, role).
- Проверка is_active для пользователя: если флаг false, запрос отклоняется.
- Встроенная поддержка ролей через CurrentUser.role и интеграцию с RBAC-утилитами.
- Возможность маркировать запросы read-only (viewer) и блокировать доступ к mutating-эндпоинтам.
---

### 7.77. `src/auth/rbac.py` — Ролевая модель доступа

**Назначение**: Централизованное описание ролей и прав доступа.

**Зона ответственности**:
- Определение ролей: `viewer`, `trader`, `admin`.
- Таблица правил: какие endpoints доступны каждой роли.
- Утилита `require_role(*roles)` для использования в эндпоинтах FastAPI.
- Таблица соответствия «role → список разрешённых scopes/endpoints».

**Вспомогательные функции:**
- require_role(*roles) — декоратор/Depends для FastAPI;
- Интеграция с docs/api.md: генерация документации по RBAC (по возможности, автоматическая).

### 7.78. `src/core/distributed_lock.py` — Распределённые блокировки

**Назначение**: Гарантировать, что критичные операции (reconciliation, массовое закрытие позиций) выполняются в единственном экземпляре даже при нескольких инстансах приложения.

**Зона ответственности**:
- `acquire_lock(name: str, ttl: int)` — попытка взять lock в Redis.
- `release_lock(name: str)` — освобождение lock.
- Контроль TTL и автоматическое продление при долгих операциях (опционально).

### 7.79. `src/core/reconciliation.py` — Сервис сверки состояния

**Назначение**: Сверка позиций и сигналов между БД и биржей.

**Зона ответственности**:
- Загрузка текущих позиций из БД и с Bybit.
- Выявление расхождений и запись результатов в `reconciliation_log`.
- При критических расхождениях — активация kill-switch и/или алерт ops-команды.
- Использование `DistributedLock` для исключения параллельного запуска.

### 7.80. `src/integration/bybit/error_handler.py` — Централизованная обработка ошибок Bybit API

**Назначение**: Свести все ошибки Bybit в единый механизм с понятной реакцией.

**Зона ответственности**:
- Маппинг `ret_code` в перечисление `BybitErrorCode`.
- Таблица действий `ErrorAction` (retry, уровень логирования, сообщение пользователю, необходимость алерта ops).
- Метод `handle_api_error(error_code, error_msg, context) -> ErrorAction`, логирующий ошибку, при необходимости шлющий алерт и возвращающий рекомендацию для `OrderManager`.

### 7.81. `docs/disaster_recovery.md` — План восстановления после аварий

**Содержание**:
- Цели по доступности:
  - RTO = 15 минут (как зафиксировано в Q-05).
  - RPO = 5 минут.
- Классы инцидентов (отказ БД, отказ узла приложения, проблемы с биржей).
- Процедура восстановления БД из бэкапа.
- Процедура перезапуска приложения и проверки здоровья (`/health`, `/metrics`).
- Регулярные DR-учения (рекомендуется не реже 1 раза в квартал).

### 7.82. `docs/backup_strategy.md` — Стратегия резервного копирования

**Содержание**:
- **Частота бэкапов**:
  - Полный бэкап БД: ежедневно в 03:00 UTC.
  - Инкрементальный бэкап: PostgreSQL WAL-архивирование (непрерывно).
- **Хранение**:
  - Локально: последние 7 дней.
  - S3: последние 90 дней (с переходом в Glacier после 30 дней).
- **Проверка целостности**: автоматическое восстановление тестового бэкапа раз в неделю.
- **RPO** (Recovery Point Objective): 5 минут (через WAL archiving)
- **Процедура восстановления**: `scripts/restore_db.sh <backup_timestamp>`.

**Техническая реализация:**
  - Continuous WAL archiving в S3/MinIO
  - Point-in-time recovery (PITR)
  - Автоматическая ротация архивов (хранение 90 дней)

### 7.83. `src/auth/passwords.py` — Работа с паролями

Назначение: централизованное управление хэшированием и проверкой паролей.

**Зона ответственности:**
- hash_password(plain: str) -> str — создать хэш (Argon2id/bcrypt).
- verify_password(plain: str, hashed: str) -> bool — проверить соответствие.
- Настройки алгоритма конфигурируемы через settings.yaml.

### 7.84. `src/auth/totp.py` — Двухфакторная аутентификация

Назначение: генерация/проверка TOTP-кодов, формирование otpauth:// URI.

**Зона ответственности:**
- generate_secret() -> str
- generate_uri(secret: str, email: str) -> str
- verify(code: str, secret: str) -> bool

### 7.85. `docs/database_schema.md` — Документация по схеме БД

Назначение: держать в одном месте:
- ER-диаграмму таблиц.
- Описание полей и индексов.
- Список Alembic-revision с краткими комментариями.

### 7.86. `docs/gdpr_compliance.md` — GDPR и защита данных

Назначение:
- Политика хранения и удаления персональных данных.

Описание процедур:
- экспорт персональных данных по запросу;
- анонимизация/удаление по Right to be forgotten;
- реагирование на инциденты в случае утечки (incident response).

### 7.87. `docs/test_plan.md` — План тестирования

Назначение: формальный перечень тестов по модулям:
- unit-tests для индикаторов, risk-engine, репозиториев.
- integration-tests с PostgreSQL/Redis/Bybit sandbox.
- e2e-сценарии (полный путь: сигнал → позиция → закрытие → метрики).

### 7.88. `docs/load_testing.md` — Нагрузочное тестирование

Назначение:
- Сценарии имитации нагрузки:
  - высокий поток WebSocket-данных;
  - шипы latency Bybit API;
  - деградация Redis/DB.
- Целевые метрики и пороговые значения, при нарушении которых релиз считается не прошедшим проверку.

### 7.89. `docs/alerting_rules.md` + `monitoring/alerts.yml`

Назначение:
- Каталог бизнес- и тех-алертов:
  - no_ws_data — нет свечей > N минут;
  - high_error_rate_bybit — рост 5xx/4xx;
  - kill_switch_triggered — срабатывание kill-switch;
  - db_replication_lag — при наличии репликации (на будущее).
- Связь с runbooks (docs/runbooks/*.md).

### 7.90. `docs/runbooks/*.md` — Операционные инструкции

Назначение:
- Пошаговые инструкции для:
  - потери WebSocket-данных (gap-recovery, переключение на REST);
  - сбоя Redis (переключение на резервный инстанс, восстановление блокировок);
  - деградации PostgreSQL (перевод на реплику/standby, ограничение write-нагрузки); 
  - ручного выключения торговли при критических инцидентах.

---

## 8. БЕЗОПАСНОСТЬ, COMPLIANCE И ОПЕРАЦИОНКА

### 8.1. Общая модель безопасности

* **Секреты и ключи**:

  * продакшен использует Vault Transit как KMS;
  * секреты не хранятся в plain-text.
* **Least privilege**:

  * сервисные аккаунты имеют минимальные права в БД и Vault;
  * каждый компонент имеет свой логин/роль в PostgreSQL.
* **Сетевое разделение**:

  * веб-часть (API/UI) разворачивается в DMZ, доступ к БД/Redis ограничен внутренней сетью;
  * доступ к Vault — только с backend-инстансов через mTLS.

### 8.2. GDPR и работа с персональными данными

* Персональные данные:

  * email пользователей;
  * потенциально IP-адреса, user-agent (если логируются);
  * привязка финансовых результатов к учётным записям.
* Меры:

  * минимизация логирования персональных данных;
  * анонимизация по Right to be forgotten:

    * email заменяется на псевдонимизированное значение;
    * записи в `audit_trail` остаются, но привязка к конкретному человеку убирается.
  * чёткая политика retention (сроки хранения) для:

    * логов,
    * audit_trail,
    * сигналов и позиций (согласуется с заказчиком и юридическим департаментом).

### 8.3. Операционные обязанности и DR

* Регулярные тесты восстановления из backup (не реже раза в квартал).
* Регулярная проверка работоспособности kill-switch и reconciliation.
* Обязательные runbooks для типовых аварий (см. §7.90).

### 8.4. Мониторинг, алертинг и наблюдаемость

* Стандартные метрики Prometheus:

  * HTTP latency/throughput по эндпоинтам;
  * error-rate Bybit интеграции;
  * глубина очередей/задержки WebSocket обработки;
  * коэффициенты стратегии (WR, PF, MaxDD).
* Алерты описаны в `docs/alerting_rules.md` и `monitoring/alerts.yml`, привязаны к runbooks.

### 8.5. Тестирование и качество

* Стратегия тестирования описана в `docs/test_plan.md`.
* Минимально допустимый набор:

  * **unit**: индикаторы, risk-engine, репозитории.
  * **integration**: взаимодействие с PostgreSQL, Redis, внешним Bybit sandbox.
  * **e2e**: полный путь сигнал → позиция → закрытие → обновление метрик.
* Порог покрытия тестами (по строкам/веткам) обсуждается с командой, но для core-логики (risk, execution, indicators) целевое покрытие ≥ 80%.

### 8.6. Нагрузочное тестирование

* Описано в `docs/load_testing.md`.
* Основные цели:

  * убедиться, что система выдерживает целевой и пиковый объём событий;
  * измерить деградацию при отказах внешних сервисов;
  * зафиксировать апдейты в SLA перед выходом в прод.

---

## 9. ПЛАН ПРОЕКТА (ROADMAP V2.4)

### 9.1. Этапы

1. **Phase 0 — Финализация спецификации (текущий документ)**

   * Результат: согласованный проектный документ v2.4.
2. **Phase 1 — Core-backend + DB + integrations**

   * Модули: ingestion, indicators, signal-engine, execution, risk, DB-схема, Alembic, базовый UI.
3. **Phase 2 — Observability, security, user-management**

   * 2FA, API-keys via Vault, метрики, алерты, runbooks.
4. **Phase 3 — Forward-validation (без торговли)**

   * Отключена подача ордеров; идёт запись сигналов и позиций в «бумажном» режиме.
5. **Phase 4 — Soft-launch и боевой режим**

   * Включение торговли с ограниченными лимитами;
   * постепенное увеличение лимитов после успешного хода.

### 9.2. Оценка трудозатрат (укрупнённо)

*(Примерно, для команды 2–3 опытных разработчиков; оценка потребует уточнения по фактическому составу команды и процессу разработки.)*

* Phase 1: 6–8 недель.
* Phase 2: 3–4 недели.
* Phase 3: минимум 4 недели (forward-validation на реальном рынке).
* Phase 4: итеративно, с ревью каждые 2 недели.