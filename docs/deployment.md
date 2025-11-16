# Deployment Guide — Bybit Algo-Grid / AVI-5

Этот документ описывает, как деплоить систему AVI-5 в стэйдж/прод окружения:

- через `docker-compose.prod.yml` (single-node / Swarm);
- через Kubernetes (Deployment + Service + Ingress);
- с учётом требований по безопасности (Vault, разделение сетей);
- с мониторингом на базе Prometheus + Grafana.

Версия приложения и окружения уточняются в CI/CD и в конкретных манифестах.

---

## Requirements

### Аппаратные ресурсы

Рекомендации для одного инстанса приложения (без учёта БД/Redis):

- **Staging**:
  - 2 vCPU;
  - 4 GB RAM;
  - диск: от 20 GB (логи + временные файлы).
- **Production (минимум)**:
  - 4 vCPU;
  - 8 GB RAM (лучше 16 GB при высокой нагрузке);
  - отдельный диск под БД (если БД не managed-сервис).

Фактические значения подбираются по результатам нагрузочного тестирования и SLA.

### Внешние зависимости

- **PostgreSQL**:
  - версия: **15+**;
  - режим: желательно отдельный managed-кластер или выделенный хост;
  - используется основная БД + (опционально) реплика для чтения и метрики репликации.
- **Redis**:
  - версия: **7+**;
  - режим: отдельный инстанс/кластер в том же сегменте сети, что и backend.

### Сетевые требования

- Доступ backend-инстансов к:
  - `wss://stream.bybit.com/...` (WS-стрим),
  - `https://api.bybit.com/...` (REST API),
  - `https://vault.<domain>` (HashiCorp Vault, mTLS).
- Доступ Prometheus/Grafana к `/metrics` сервиса.
- Внешний доступ к:
  - `https://avi5.<domain>` — UI + API (через Traefik / Ingress).

### Безопасность и секреты

- В **production**:
  - торговые ключи Bybit и TOTP-секреты **не** хранятся в `.env` / файлах на диске;
  - все операции шифрования/дешифрования выполняются через **Vault Transit**;
  - доступ к Vault разрешён только с backend-инстансов (mTLS, отдельные роли).
- В **dev/test**:
  - допускается `config/secrets.env` с тестовыми значениями (см. `config/secrets.env.example`);
  - файл не коммитится и не используется в боевом контуре.

---

## Docker-compose

Этот раздел описывает production-ориентированный стек на базе `docker/docker-compose.prod.yml`.

### Структура стека

Основные сервисы:

- `app` — контейнер с приложением (FastAPI + Uvicorn);
- `db` — PostgreSQL 15+ (может быть внешним сервисом вместо контейнера);
- `redis` — Redis 7+;
- `traefik` — reverse-proxy с TLS-терминацией и роутингом к `app`.

Для локальной разработки используется отдельный файл `docker/docker-compose.yml` (dev-стек), здесь речь про стэйдж/прод.

### Пример `docker/docker-compose.prod.yml`

Ниже приведён упрощённый пример. Реальный файл может содержать дополнительные опции (ресурсные лимиты, логирование, healthcheck’и).

```yaml
version: "3.9"

services:
  app:
    image: registry.example.com/bybit-algo-grid:${APP_TAG:-latest}
    restart: always
    depends_on:
      - db
      - redis
    environment:
      APP_ENV: "prod"
      # Подключение к БД и Redis (секреты могут приходить из Vault/CI)
      DATABASE_URL: "postgresql+asyncpg://app:app_password@db:5432/avi5"
      REDIS_URL: "redis://redis:6379/0"
      # Настройки доступа к Vault (пример, конкретные значения зависят от инфраструктуры)
      VAULT_ADDR: "https://vault.example.com"
      VAULT_ROLE_ID: "${VAULT_ROLE_ID}"
      VAULT_SECRET_ID: "${VAULT_SECRET_ID}"
    labels:
      # Traefik: HTTP → app
      - "traefik.enable=true"
      - "traefik.http.routers.avi5.rule=Host(`avi5.example.com`)"
      - "traefik.http.routers.avi5.entrypoints=websecure"
      - "traefik.http.routers.avi5.tls.certresolver=letsencrypt"
      - "traefik.http.services.avi5.loadbalancer.server.port=8000"

  db:
    image: postgres:15
    restart: always
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app_password
      POSTGRES_DB: avi5
    volumes:
      - /var/lib/postgresql/avi5-data:/var/lib/postgresql/data
    ports:
      # Обычно не пробрасывается наружу, только для отладки/миграций
      - "5432:5432"

  redis:
    image: redis:7
    restart: always
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - /var/lib/redis/avi5-data:/data

  traefik:
    image: traefik:v2.10
    restart: always
    command:
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web"
      - "--certificatesresolvers.letsencrypt.acme.email=admin@example.com"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik/letsencrypt:/letsencrypt
````

#### Комментарии к прод-стеку

* **Масштабирование**:

  * при использовании Docker Swarm можно задать `deploy.replicas` для `app`;
  * в обычном `docker-compose` масштабируем через `docker compose up --scale app=2`.
* **БД в проде**:

  * рекомендуется заменить контейнер `db` на managed PostgreSQL (RDS/Cloud SQL/и т.п.) и использовать только внешний DSN;
  * контейнерный `db` хорош для стэйджа/тестов.
* **Секреты**:

  * чувствительные значения (`DATABASE_URL`, креды Vault и т.п.) должны поступать из CI/CD или Vault Agent;
  * не хранить рабочие данные в `docker-compose.prod.yml` в явном виде.

---

## Kubernetes

Этот раздел даёт шаблон манифестов для деплоя AVI-5 в Kubernetes-кластер.

### Общие принципы

* Приложение запускается как `Deployment` с 2+ репликами.
* Доступ наружу — через `Ingress` (класс `traefik` или другой, принятый в кластере).
* Конфигурация (не секьюрная) хранится в `ConfigMap`, секреты — в `Secret` или интегрируются через Vault CSI/Agent.
* БД и Redis обычно предоставляются как managed-сервисы или отдельные StatefulSet’ы.

### Пример ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: avi5-config
  namespace: trading
data:
  APP_ENV: "prod"
  REDIS_URL: "redis://redis.trading.svc.cluster.local:6379/0"
  # DATABASE_URL и секреты обычно не кладутся в ConfigMap
```

### Пример Secret (упрощённо)

> В реальной среде предпочтительнее использовать Vault CSI/Operator; здесь — базовый пример с Secret.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: avi5-secrets
  namespace: trading
type: Opaque
stringData:
  DATABASE_URL: "postgresql+asyncpg://app:app_password@postgres.trading.svc.cluster.local:5432/avi5"
  VAULT_ADDR: "https://vault.example.com"
  VAULT_ROLE_ID: "<role-id>"
  VAULT_SECRET_ID: "<secret-id>"
```

### Пример Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: avi5-backend
  namespace: trading
spec:
  replicas: 2
  selector:
    matchLabels:
      app: avi5-backend
  template:
    metadata:
      labels:
        app: avi5-backend
    spec:
      containers:
        - name: app
          image: registry.example.com/bybit-algo-grid:${APP_TAG:-latest}
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef:
                name: avi5-config
            - secretRef:
                name: avi5-secrets
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 30
            periodSeconds: 30
```

### Пример Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: avi5-backend
  namespace: trading
spec:
  selector:
    app: avi5-backend
  ports:
    - name: http
      port: 80
      targetPort: 8000
  type: ClusterIP
```

### Пример Ingress (Traefik)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: avi5-ingress
  namespace: trading
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
spec:
  ingressClassName: traefik
  tls:
    - hosts:
        - avi5.example.com
      secretName: avi5-tls
  rules:
    - host: avi5.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: avi5-backend
                port:
                  number: 80
```

---

## Monitoring

Этот раздел описывает интеграцию с Prometheus и Grafana. Правила алертинга задаются в `monitoring/alerts.yml`.

### Prometheus: scrape config

Пример фрагмента `prometheus.yml`:

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: "bybit-algo-grid"
    metrics_path: /metrics
    static_configs:
      - targets: ["avi5-backend.trading.svc.cluster.local:8000"]

  - job_name: "bybit-algo-grid-db"
    static_configs:
      - targets: ["postgres-exporter.trading.svc.cluster.local:9187"]

rule_files:
  - "monitoring/alerts.yml"
```

Комментарии:

* `bybit-algo-grid` — основной job для backend’а (совпадает с job, используемым в alert-правилах).
* `bybit-algo-grid-db` — job для Postgres-экспортера (используется, например, для алертов по лагу репликации).
* `monitoring/alerts.yml` подключает преднастроенные алерты (нет WS-данных, высокий error-rate Bybit, kill-switch и т.п.).

### Grafana: datasource

Пример конфигурации datasource (YAML-вариант для `provisioning/datasources/avi5-prometheus.yaml`):

```yaml
apiVersion: 1

datasources:
  - name: Avi5 Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus.monitoring.svc.cluster.local:9090
    isDefault: true
    jsonData:
      timeInterval: 15s
```

На базе этого datasource можно собирать дашборды:

* latency и error-rate API;
* состояние kill-switch;
* метрики WS/REST по Bybit;
* SLA по сигналам/позициям.

Дополнительные детали по DR и backup см. в `docs/disaster_recovery.md` и `docs/backup_strategy.md`.
