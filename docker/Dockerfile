# --------
# Builder stage: install build deps, Poetry and Python dependencies
# --------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Обновляем пакеты и ставим системные зависимости,
# необходимые для сборки типичных Python-пакетов (asyncpg, psutil и т.п.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем Poetry
RUN pip install --no-cache-dir poetry

# Копируем только файлы зависимостей для кэша слоёв
COPY pyproject.toml poetry.lock* ./

# Экспортируем зависимости в requirements.txt
RUN poetry export -f requirements.txt -o requirements.txt --without-hashes

# Устанавливаем зависимости в user-окружение (см. спецификацию)
RUN pip install --no-cache-dir --user -r requirements.txt


# --------
# Runtime stage: минимальный образ с зависимостями и кодом
# --------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # бинарники из user site (если вдруг появятся – тот же путь, что в builder'е)
    PATH="/root/.local/bin:${PATH}"
# user site-packages из /root/.local будут автоматически доступны Python

# Создаём рабочий каталог
WORKDIR /app

# Копируем установленные зависимости из builder-стейджа
COPY --from=builder /root/.local /root/.local

# Копируем код приложения
COPY . /app

# Приводим права к безопасным для запуска под nobody:
# - разрешаем чтение зависимостей
# - делаем приложение читаемым для непривилегированного пользователя
RUN chmod 755 /root && \
    chmod -R a+rX /root/.local && \
    chown -R nobody:nogroup /app

# Непривилегированный пользователь (по спецификации)
USER nobody

EXPOSE 8000

# Запуск приложения через Uvicorn
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
