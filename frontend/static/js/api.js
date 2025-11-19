// frontend/static/js/api.js

/**
 * HTTPError — обёртка над неуспешным HTTP-ответом.
 * Бросается APIClient при status >= 400.
 */
export class HTTPError extends Error {
  /**
   * @param {Response} response
   * @param {any} bodyParsed - уже разобранное тело (JSON или text)
   */
  constructor(response, bodyParsed) {
    const message =
      bodyParsed && typeof bodyParsed === "object" && bodyParsed.message
        ? bodyParsed.message
        : `HTTP ${response.status} ${response.statusText}`;

    super(message);

    this.name = "HTTPError";
    this.status = response.status;
    this.statusText = response.statusText;
    this.url = response.url;
    this.body = bodyParsed;

    // Чтобы instanceof работал после транспиляции
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, HTTPError);
    }
  }
}

/**
 * Утилита debounce: возвращает функцию-обёртку, которая
 * будет вызываться не чаще, чем раз в delay миллисекунд.
 *
 * @template {(...args: any[]) => any} F
 * @param {F} fn
 * @param {number} delay
 * @returns {F}
 */
export function debounce(fn, delay) {
  /** @type {number | undefined} */
  let timerId;

  // @ts-ignore — TS не умеет нормально выводить типы для generic debounce в JS
  return function debounced(...args) {
    if (timerId !== undefined) {
      clearTimeout(timerId);
    }
    timerId = window.setTimeout(() => {
      timerId = undefined;
      fn(...args);
    }, delay);
  };
}

/**
 * Простенький помощник: пытаемся вытащить Bearer-токен из localStorage.
 * Ключ не зафиксирован в спеке, поэтому поддерживаем несколько вариантов.
 *
 * Публичный контракт — только то, что заголовок Authorization вообще появляется,
 * конкретное имя ключа можно будет донастроить при необходимости.
 *
 * @returns {string | null}
 */
function getAuthTokenFromLocalStorage() {
  const possibleKeys = ["auth_token", "access_token", "token", "jwt"];
  for (const key of possibleKeys) {
    const value = window.localStorage.getItem(key);
    if (value) {
      return value;
    }
  }
  return null;
}

/**
 * Нормализация baseURL: убираем лишние слэши в конце.
 *
 * @param {string} baseURL
 * @returns {string}
 */
function normalizeBaseURL(baseURL) {
  return baseURL.replace(/\/+$/, "");
}

/**
 * APIClient — небольшой fetch-wrapper для бэкенд-API AVI-5.
 *
 * Отвечает за:
 *  - подстановку baseURL;
 *  - Authorization: Bearer <token> из localStorage;
 *  - разбор JSON/текста;
 *  - бросание HTTPError при status >= 400.
 */
export class APIClient {
  /**
   * @param {string} baseURL - базовый URL API, например "/api" или "https://example.com/api"
   */
  constructor(baseURL) {
    if (typeof baseURL !== "string") {
      throw new TypeError("APIClient: baseURL должен быть строкой");
    }
    this.baseURL = normalizeBaseURL(baseURL);
  }

  /**
   * Базовый метод для всех запросов.
   *
   * @param {string} path - относительный путь, например "/signals"
   * @param {object} [options]
   * @param {string} [options.method]
   * @param {Record<string, any>} [options.query] - query-параметры
   * @param {any} [options.body] - JSON-тело
   * @param {HeadersInit} [options.headers]
   * @returns {Promise<any>}
   */
  async _request(path, { method = "GET", query, body, headers } = {}) {
    const authToken = getAuthTokenFromLocalStorage();

    /** @type {HeadersInit} */
    const finalHeaders = {
      Accept: "application/json",
      ...(body != null ? { "Content-Type": "application/json" } : {}),
      ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
      ...(headers || {}),
    };

    let url = this.baseURL + (path.startsWith("/") ? path : `/${path}`);

    if (query && typeof query === "object") {
      const params = new URLSearchParams();
      Object.entries(query).forEach(([key, value]) => {
        if (value === undefined || value === null) {
          return;
        }
        if (Array.isArray(value)) {
          value.forEach((v) => params.append(key, String(v)));
        } else {
          params.append(key, String(value));
        }
      });
      const qs = params.toString();
      if (qs) {
        url += (url.includes("?") ? "&" : "?") + qs;
      }
    }

    /** @type {RequestInit} */
    const fetchOptions = {
      method,
      headers: finalHeaders,
    };

    if (body != null && method !== "GET" && method !== "HEAD") {
      fetchOptions.body = JSON.stringify(body);
    }

    const response = await fetch(url, fetchOptions);

    // 204 No Content — просто возвращаем null
    if (response.status === 204) {
      if (!response.ok) {
        throw new HTTPError(response, null);
      }
      return null;
    }

    let parsedBody;
    const contentType = response.headers.get("Content-Type") || "";

    try {
      if (contentType.includes("application/json")) {
        parsedBody = await response.json();
      } else {
        parsedBody = await response.text();
      }
    } catch {
      parsedBody = null;
    }

    if (!response.ok) {
      throw new HTTPError(response, parsedBody);
    }

    return parsedBody;
  }

  /**
   * Получить список сигналов.
   *
   * GET /signals
   *
   * @param {object} [filters] - фильтры для запроса (symbol, status, limit, и т.п.).
   * @returns {Promise<any[]>} Promise<Signal[]>
   */
  async getSignals(filters) {
    return /** @type {Promise<any[]>} */ (this._request("/signals", {
      method: "GET",
      query: filters,
    }));
  }

  /**
   * Получить текущие позиции.
   *
   * GET /positions
   *
   * (Метод явно не расписан в таблице overview, но следует из API-спеки;
   * публичный контракт минимален: вернуть JSON с позициями.)
   *
   * @param {object} [params]
   * @returns {Promise<any[]>} Promise<Position[]>
   */
  async getPositions(params) {
    return /** @type {Promise<any[]>} */ (this._request("/positions", {
      method: "GET",
      query: params,
    }));
  }

  /**
   * Ручное закрытие позиции.
   *
   * POST /positions/{id}/close
   *
   * Тело запроса отсутствует, возвращается обновлённый объект Position.
   *
   * @param {string | number} positionId
   * @returns {Promise<any>} Promise<Position>
   */
  async postClosePosition(positionId) {
    if (positionId === undefined || positionId === null) {
      throw new TypeError("postClosePosition: positionId обязателен");
    }
    const id = String(positionId);
    return this._request(`/positions/${encodeURIComponent(id)}/close`, {
      method: "POST",
    });
  }

  /**
   * Получить конфигурацию приложения/стратегии.
   *
   * GET /config
   *
   * @returns {Promise<any>} Promise<AppConfig>
   */
  async getConfig() {
    return this._request("/config", {
      method: "GET",
    });
  }
}
