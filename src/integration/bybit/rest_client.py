from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Mapping, MutableMapping, Optional

import httpx

from src.integration.bybit.rate_limiter import RateLimiterBybit
from src.integration.bybit.error_handler import raise_for_bybit_rest_error
from src.core.exceptions import NetworkError


class BybitRESTClient:
    """
    Низкоуровневый REST-клиент Bybit.

    Отвечает за:
    - формирование и подпись запросов;
    - применение rate limiter'а;
    - базовый retry/backoff на сетевых и временных ошибках;
    - делегирование разбора бизнес-ошибок в error_handler.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        recv_window_ms: int,
        rate_limiter: RateLimiterBybit,
        timeout: float = 10.0,
        max_retries: int = 3,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not base_url.endswith("/"):
            base_url = base_url + "/"

        self._base_url = base_url
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._recv_window_ms = recv_window_ms
        self._rate_limiter = rate_limiter
        self._timeout = timeout
        self._max_retries = max_retries

        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        """Явное закрытие HTTP-клиента, если он создан внутри."""
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        auth: bool = False,
        is_order: bool = False,
        read_weight: int = 1,
        max_retries: Optional[int] = None,
    ) -> Mapping[str, Any]:
        """
        Универсальный метод для вызова REST-эндпоинтов Bybit.

        method:
            HTTP-метод ('GET', 'POST' и т.д.).
        path:
            Относительный путь эндпоинта, с ведущим слэшем или без.
            Допустимы оба варианта:
                "/v5/market/tickers" или "v5/market/tickers".
            Клиент сам нормализует путь.
        params:
            Query-параметры.
        body:
            JSON-тело для POST/PUT.
        auth:
            Нужна ли подпись и заголовки API-ключа.
        is_order:
            Является ли запрос ордерным (используется отдельный лимит).
        read_weight:
            Вес запроса в лимитах чтения (по умолчанию 1).
        max_retries:
            Локальное переопределение количества ретраев. Если None — берётся
            значение из конфигурации клиента.
        """
        if not path:
            raise ValueError("path must be non-empty")

        # Допускаем как "/v5/..." так и "v5/...": нормализуем к виду без ведущего слэша.
        normalized_path = path.lstrip("/")

        url = self._base_url + normalized_path
        retries = self._max_retries if max_retries is None else max_retries

        attempt = 0
        while True:
            attempt += 1

            # Применяем rate limits до реального запроса.
            await self._apply_rate_limit(is_order=is_order, read_weight=read_weight)

            try:
                request_kwargs: MutableMapping[str, Any] = {
                    "method": method.upper(),
                    "url": url,
                    "params": dict(params or {}),
                }

                if body is not None:
                    # Bybit ожидает compact JSON без лишних пробелов.
                    request_kwargs["content"] = json.dumps(body, separators=(",", ":"))

                headers: dict[str, str] = {}
                if auth:
                    timestamp_ms = int(time.time() * 1000)
                    self._apply_auth_headers(
                        headers=headers,
                        method=method.upper(),
                        path=normalized_path,
                        params=params or {},
                        body=body,
                        timestamp_ms=timestamp_ms,
                    )

                if headers:
                    request_kwargs["headers"] = headers

                response = await self._client.request(**request_kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt > retries:
                    raise NetworkError(
                        f"Bybit REST network error after {attempt} attempts",
                    ) from exc
                await self._sleep_backoff(attempt)
                continue

            # HTTP-статусы 5xx и 429 считаем временными, пробуем ретраи.
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt > retries:
                    # Дальше пусть error_handler маппит ошибку по телу.
                    data = await self._safe_json(response)
                    raise_for_bybit_rest_error(
                        data,
                        http_status=response.status_code,
                        context={"url": url, "method": method, "attempts": attempt},
                    )
                await self._sleep_backoff(attempt)
                continue

            data = await self._safe_json(response)
            # Бизнес-ошибки маппятся в error_handler.
            raise_for_bybit_rest_error(
                data,
                http_status=response.status_code,
                context={"url": url, "method": method, "attempts": attempt},
            )
            return data

        # сюда исполнение никогда не дойдёт — только для тайпчекера
        raise RuntimeError("Unreachable: BybitRESTClient.request")

    async def _apply_rate_limit(self, *, is_order: bool, read_weight: int) -> None:
        """Вызывает соответствующий bucket rate limiter'а."""
        if is_order:
            await self._rate_limiter.consume_order()
        else:
            await self._rate_limiter.consume_read(max(read_weight, 1))

    async def _sleep_backoff(self, attempt: int) -> None:
        """Простейший экспоненциальный backoff с ограничением."""
        # 0.2, 0.4, 0.8, 1.6, ... до ~3 секунд.
        delay = min(0.2 * (2 ** (attempt - 1)), 3.0)
        await asyncio.sleep(delay)

    async def _safe_json(self, response: httpx.Response) -> Mapping[str, Any]:
        """
        Безопасное извлечение JSON из ответа.

        Если тело не JSON — возвращаем словарь с сырым текстом.
        """
        try:
            return response.json()
        except ValueError:
            return {
                "raw_text": response.text,
                "status_code": response.status_code,
            }

    def _apply_auth_headers(
        self,
        *,
        headers: MutableMapping[str, str],
        method: str,
        path: str,
        params: Mapping[str, Any],
        body: Optional[Mapping[str, Any]],
        timestamp_ms: int,
    ) -> None:
        """
        Формирует заголовки аутентификации Bybit V5.

        Детали алгоритма подписи соответствуют официальной документации:
        sign = HMAC_SHA256(secret, timestamp + api_key + recv_window + payload)
        где payload — query string или JSON-строка тела.
        """
        recv_window = str(self._recv_window_ms)

        # Формирование payload для подписи.
        if method.upper() == "GET":
            payload = self._build_query_string(params)
        else:
            payload = json.dumps(body or {}, separators=(",", ":"))

        sign_str = f"{timestamp_ms}{self._api_key}{recv_window}{payload}"
        signature = hmac.new(
            self._api_secret,
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        headers.update(
            {
                "X-BAPI-API-KEY": self._api_key,
                "X-BAPI-SIGN": signature,
                "X-BAPI-TIMESTAMP": str(timestamp_ms),
                "X-BAPI-RECV-WINDOW": recv_window,
                "Content-Type": "application/json",
            },
        )

    @staticmethod
    def _build_query_string(params: Mapping[str, Any]) -> str:
        """
        Сериализация query-параметров в строку, совместимую с требованиями Bybit.

        Параметры сортируются по ключу, значения приводятся к строке.
        """
        if not params:
            return ""
        items = sorted(params.items(), key=lambda kv: kv[0])
        return "&".join(f"{k}={str(v)}" for k, v in items)
