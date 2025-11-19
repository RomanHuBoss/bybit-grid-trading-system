// frontend/static/js/main.js

// ESM-модуль: main.js — точка входа фронтенда AVI-5.
//
// Оркеструет:
//  - APIClient (REST)
//  - TilesController (рендер плиток сигналов)
//  - ModalController (детали сигнала)
//  - SSE /stream (signal, position, metrics, kill_switch)
//  - UI-контролы: stake, prob_threshold, фильтр символов, режим.

// Импорт модулей (из того же каталога /static/js)
import { APIClient } from "./api.js";
import { initTiles } from "./tiles.js";

/** Базовый URL API по спецификации. */
const API_BASE_URL = "/api/v1";

/** Ключ для localStorage для настроек UI. */
const STORAGE_KEY_UI_STATE = "avi5_ui_state";

/** Глобальная (для модуля) конфигурация UI. */
const globalConfig = {
  stake: 25,
  probThreshold: 0.5, // 0..1
  mode: "paper", // "paper" | "live"
  /** @type {Set<string>} */
  selectedSymbols: new Set(),
};

/** Ссылки на контроллеры и состояние SSE. */
let apiClient = null;
let tilesController = /** @type {import("./tiles.js").TilesController | null} */ (null);
let modalController = null;
let eventSource = /** @type {EventSource | null} */ (null);
let reconnectTimerId = /** @type {number | null} */ (null);
let reconnectAttempts = 0;

/** Кэш кнопок фильтра символов. */
const symbolButtons = new Map();

/**
 * Главная функция инициализации UI.
 * Поднимает APIClient, плитки, модал, настраивает контролы и SSE.
 */
function initApp() {
  apiClient = new APIClient(API_BASE_URL);

  const tilesContainer = document.getElementById("tiles-container");
  if (!(tilesContainer instanceof HTMLElement)) {
    console.error("[main] #tiles-container не найден, UI не будет инициализирован");
    return;
  }

  // Модал (глобальный контроллер, экспортируется из modal.js через window)
  if (typeof window.ModalController === "function") {
    modalController = new window.ModalController("modal-container");
  } else {
    console.warn("[main] window.ModalController не найден — модальное окно недоступно");
  }

  // Контроллер плиток
  tilesController = initTiles(tilesContainer, {
    onTileClick: handleTileClick,
    onClosePositionClick: handleClosePositionClick,
  });

  // Восстанавливаем настройки из localStorage (если есть)
  restoreUIStateFromStorage();

  // Настраиваем контролы (stake, prob_threshold, mode, symbols)
  initControls();

  // Подтягиваем начальные данные по REST
  bootstrapFromAPI().catch((err) => {
    console.warn("[main] Ошибка начальной загрузки данных", err);
  });

  // Подключаемся к SSE /stream
  connectSSE();
}

/**
 * Восстановление глобальной конфигурации из localStorage.
 */
function restoreUIStateFromStorage() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY_UI_STATE);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (typeof parsed.stake === "number" && isFinite(parsed.stake)) {
      globalConfig.stake = parsed.stake;
    }
    if (typeof parsed.probThreshold === "number" && isFinite(parsed.probThreshold)) {
      globalConfig.probThreshold = parsed.probThreshold;
    }
    if (parsed.mode === "live" || parsed.mode === "paper") {
      globalConfig.mode = parsed.mode;
    }
    if (Array.isArray(parsed.selectedSymbols)) {
      globalConfig.selectedSymbols = new Set(parsed.selectedSymbols.map(String));
    }
  } catch (err) {
    console.warn("[main] Не удалось прочитать состояние UI из localStorage", err);
  }
}

/**
 * Сохранить глобальную конфигурацию в localStorage.
 */
function persistUIStateToStorage() {
  try {
    const payload = {
      stake: globalConfig.stake,
      probThreshold: globalConfig.probThreshold,
      mode: globalConfig.mode,
      selectedSymbols: Array.from(globalConfig.selectedSymbols),
    };
    window.localStorage.setItem(STORAGE_KEY_UI_STATE, JSON.stringify(payload));
  } catch (err) {
    console.warn("[main] Не удалось сохранить состояние UI в localStorage", err);
  }
}

/**
 * Инициализация контролов в шапке UI.
 * Настраивает обработчики для stake, prob_threshold, mode и фильтров символов.
 */
function initControls() {
  const stakeInput = /** @type {HTMLInputElement | null} */ (
    document.getElementById("stake-input")
  );
  const probInput = /** @type {HTMLInputElement | null} */ (
    document.getElementById("prob-threshold-input")
  );
  const modeSwitch = document.getElementById("mode-switch");
  const modeLabel = document.getElementById("mode-label");

  if (stakeInput) {
    stakeInput.value = String(globalConfig.stake);
    stakeInput.addEventListener("change", () => {
      const value = Number(stakeInput.value);
      if (!Number.isFinite(value) || value <= 0) {
        // Возвращаем предыдущее значение.
        stakeInput.value = String(globalConfig.stake);
        return;
      }
      globalConfig.stake = value;
      persistUIStateToStorage();
    });
  }

  if (probInput) {
    probInput.value = String(globalConfig.probThreshold);
    probInput.addEventListener("change", () => {
      let value = Number(probInput.value);
      if (!Number.isFinite(value)) {
        value = globalConfig.probThreshold;
      }
      // Ограничиваем диапазон 0..1
      value = Math.min(1, Math.max(0, value));
      probInput.value = String(value);
      globalConfig.probThreshold = value;
      applyTilesFilters();
      persistUIStateToStorage();
    });
  }

  if (modeSwitch && modeLabel) {
    updateModeLabel(modeLabel);

    modeSwitch.addEventListener("click", () => {
      globalConfig.mode = globalConfig.mode === "paper" ? "live" : "paper";
      updateModeLabel(modeLabel);
      persistUIStateToStorage();
    });
  }

  // При восстановлении selectedSymbols из localStorage мы ещё не знаем список тикеров.
  // Кнопки будут создаваться по мере загрузки сигналов (bootstrap + SSE).
  applyTilesFilters();
}

/**
 * Обновление подписи режима (Paper / Live).
 *
 * @param {HTMLElement} modeLabel
 */
function updateModeLabel(modeLabel) {
  if (globalConfig.mode === "live") {
    modeLabel.textContent = "Live";
  } else {
    modeLabel.textContent = "Paper";
  }
}

/**
 * Применить фильтры к плиткам на основе globalConfig.
 */
function applyTilesFilters() {
  if (!tilesController) return;
  tilesController.setFilters({
    minProbability:
      typeof globalConfig.probThreshold === "number" ? globalConfig.probThreshold : undefined,
    allowedSymbols:
      globalConfig.selectedSymbols.size > 0
        ? Array.from(globalConfig.selectedSymbols.values())
        : undefined,
  });
}

/**
 * Создать (если нужно) кнопку фильтра для символа.
 *
 * @param {string} symbol
 */
function ensureSymbolButton(symbol) {
  const symbolsFilterEl = document.getElementById("symbols-filter");
  if (!(symbolsFilterEl instanceof HTMLElement)) return;
  const sym = String(symbol || "").toUpperCase();
  if (!sym || symbolButtons.has(sym)) return;

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "badge-soft text-xs";
  btn.textContent = sym;
  btn.dataset.symbol = sym;

  btn.addEventListener("click", () => {
    if (globalConfig.selectedSymbols.has(sym)) {
      globalConfig.selectedSymbols.delete(sym);
    } else {
      globalConfig.selectedSymbols.add(sym);
    }
    updateSymbolButtonState(sym, btn);
    applyTilesFilters();
    persistUIStateToStorage();
  });

  symbolsFilterEl.appendChild(btn);
  symbolButtons.set(sym, btn);

  updateSymbolButtonState(sym, btn);
}

/**
 * Обновляет внешний вид кнопки символа в зависимости от выбранности.
 *
 * @param {string} symbol
 * @param {HTMLButtonElement} btn
 */
function updateSymbolButtonState(symbol, btn) {
  const selected = globalConfig.selectedSymbols.has(symbol);
  btn.classList.toggle("badge-long", selected);
}

/**
 * Начальная загрузка данных по REST:
 *  - активные сигналы (GET /signals)
 *  - актуальные позиции (GET /positions)
 */
async function bootstrapFromAPI() {
  if (!apiClient || !tilesController) return;

  let signals = [];
  let positions = [];

  try {
    signals = (await apiClient.getSignals()) || [];
  } catch (err) {
    console.warn("[main] Ошибка GET /signals", err);
  }

  try {
    positions = (await apiClient.getPositions()) || [];
  } catch (err) {
    console.warn("[main] Ошибка GET /positions", err);
  }

  // Заполняем плитки сигналами
  for (const raw of signals) {
    const normalized = normalizeSignalPayload(raw);
    tilesController.upsertSignal(normalized);
    ensureSymbolButton(normalized.symbol);
  }

  // Обновляем статусы по позициям
  for (const raw of positions) {
    const pos = normalizePositionPayload(raw);
    tilesController.updatePosition(pos);
  }

  applyTilesFilters();
}

// ============================================================================
// SSE: подключение к /stream, обработка событий и reconnect
// ============================================================================

/**
 * Установить статус подключения в header (индикатор connection-status).
 *
 * @param {"connecting"|"ok"|"error"} state
 * @param {string} label
 */
function setConnectionStatus(state, label) {
  const el = document.getElementById("connection-status");
  if (!el) return;
  el.dataset.state = state;
  const textSpan = el.querySelector("span:last-child");
  if (textSpan) {
    textSpan.textContent = label;
  }
}

/**
 * Подключение к SSE /stream и настройка обработчиков.
 */
function connectSSE() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (reconnectTimerId != null) {
    window.clearTimeout(reconnectTimerId);
    reconnectTimerId = null;
  }

  setConnectionStatus("connecting", "Connecting…");

  const es = new EventSource("/stream");
  eventSource = es;

  es.addEventListener("open", () => {
    reconnectAttempts = 0;
    setConnectionStatus("ok", "Live");
  });

  es.addEventListener("error", () => {
    // Если поток окончательно закрыт, инициируем reconnect с backoff.
    if (es.readyState === EventSource.CLOSED) {
      setConnectionStatus("error", "Disconnected");
      scheduleReconnect();
    } else {
      setConnectionStatus("connecting", "Reconnecting…");
    }
  });

  es.addEventListener("signal", (event) => {
    if (!tilesController) return;
    const payload = safeParseJSON(event.data);
    if (!payload) return;
    const signal = normalizeSignalPayload(payload);
    tilesController.upsertSignal(signal);
    ensureSymbolButton(signal.symbol);
  });

  es.addEventListener("position", (event) => {
    if (!tilesController) return;
    const payload = safeParseJSON(event.data);
    if (!payload) return;
    const position = normalizePositionPayload(payload);
    tilesController.updatePosition(position);
  });

  // Дополнительные события по расширенной спецификации: metrics, kill_switch.
  es.addEventListener("metrics", (event) => {
    const payload = safeParseJSON(event.data);
    if (!payload) return;
    updateMetricsUI(payload);
  });

  es.addEventListener("kill_switch", (event) => {
    const payload = safeParseJSON(event.data);
    if (!payload) return;
    updateKillSwitchIndicator(payload);
  });
}

/**
 * Планирование переподключения к SSE с экспоненциальным backoff + кап.
 */
function scheduleReconnect() {
  if (reconnectTimerId != null) return;

  reconnectAttempts += 1;
  const baseDelay = 1000;
  const maxDelay = 30000;
  const delay = Math.min(maxDelay, baseDelay * Math.pow(2, reconnectAttempts));

  reconnectTimerId = window.setTimeout(() => {
    reconnectTimerId = null;
    connectSSE();
  }, delay);
}

/**
 * Безопасный JSON.parse для SSE-пейлоадов.
 *
 * @param {string} data
 * @returns {any | null}
 */
function safeParseJSON(data) {
  try {
    return JSON.parse(data);
  } catch (err) {
    console.warn("[main] Некорректный JSON из SSE", err, data);
    return null;
  }
}

// ============================================================================
// Нормализация моделей backend → TileSignal / TilePosition
// ============================================================================

/**
 * Нормализация сигнала из backend в формат TileSignal.
 *
 * @param {any} raw
 * @returns {import("./tiles.js").TileSignal}
 */
function normalizeSignalPayload(raw) {
  const sideRaw = String(raw.direction || raw.side || "").toUpperCase();
  const side = sideRaw === "SHORT" ? "SHORT" : "LONG"; // по умолчанию long

  const status = typeof raw.status === "string" ? raw.status.toUpperCase() : "NEW";

  let createdTs;
  if (typeof raw.created_ts === "number") {
    createdTs = raw.created_ts;
  } else if (raw.created_at) {
    const ts = Date.parse(raw.created_at);
    createdTs = Number.isFinite(ts) ? Math.round(ts / 1000) : undefined;
  }

  return {
    id: raw.id,
    symbol: raw.symbol,
    side,
    probability: typeof raw.probability === "number" ? raw.probability : undefined,
    entry_price: raw.entry_price,
    tp: raw.tp ?? raw.tp_price ?? null,
    sl: raw.sl ?? raw.sl_price ?? null,
    r: typeof raw.r === "number" ? raw.r : raw.r_multiple ?? null,
    status,
    position_id:
      raw.position_id ??
      (raw.position && raw.position.id != null ? raw.position.id : null),
    created_ts: createdTs,
  };
}

/**
 * Нормализация позиции из backend в формат TilePosition.
 *
 * @param {any} raw
 * @returns {{id: string|number, signal_id: string|number|null, status: string}}
 */
function normalizePositionPayload(raw) {
  const id = raw.id;
  const signalId =
    raw.signal_id != null
      ? raw.signal_id
      : raw.signal && raw.signal.id != null
      ? raw.signal.id
      : null;
  const status = String(raw.status || "").toUpperCase();
  return {
    id,
    signal_id: signalId,
    status,
  };
}

// ============================================================================
// Обработчики UI-событий от плиток и обновление метрик / kill-switch
// ============================================================================

/**
 * Клик по плитке — открыть модальное окно с уровнями.
 *
 * @param {import("./tiles.js").TileSignal} tileSignal
 */
function handleTileClick(tileSignal) {
  if (!modalController) return;

  // Пробрасываем в модал то, что он ожидает, максимально из доступных полей.
  const sideLower = String(tileSignal.side || "").toUpperCase() === "SHORT" ? "short" : "long";

  const modalPayload = {
    id: String(tileSignal.id),
    symbol: tileSignal.symbol,
    side: sideLower,
    timeframe: null,
    entry: tileSignal.entry_price,
    sl: tileSignal.sl ?? null,
    tp1: tileSignal.tp ?? null,
    tp2: null,
    tp3: null,
    r_multiple: typeof tileSignal.r === "number" ? tileSignal.r : null,
    be_active: false,
    be_level: null,
    comment: "", // комментарии специфичны для backend; здесь пока ничего
    calibration: null,
  };

  modalController.open(modalPayload);
}

/**
 * Клик по кнопке "Close" на плитке — ручное закрытие позиции.
 *
 * @param {import("./tiles.js").TileSignal} tileSignal
 */
async function handleClosePositionClick(tileSignal) {
  if (!apiClient || !tilesController) return;

  const positionId = tileSignal.position_id;
  if (positionId == null) {
    console.warn("[main] Нет position_id для сигнала, нечего закрывать", tileSignal);
    return;
  }

  try {
    const updated = await apiClient.postClosePosition(positionId);
    const pos = normalizePositionPayload(updated);
    tilesController.updatePosition(pos);
  } catch (err) {
    console.error("[main] Ошибка ручного закрытия позиции", err);
    // Здесь можно было бы показать toast/alert, но по спеке UI минималистичный → только лог.
  }
}

/**
 * Обновить панель метрик (WR, PF, Max DD).
 *
 * @param {any} data
 */
function updateMetricsUI(data) {
  const wrEl = document.getElementById("metric-wr");
  const pfEl = document.getElementById("metric-pf");
  const ddEl = document.getElementById("metric-maxdd");

  if (wrEl && typeof data.wr === "number") {
    wrEl.textContent = `${(data.wr * 100).toFixed(1)}%`;
  }
  if (pfEl && typeof data.pf === "number") {
    pfEl.textContent = data.pf.toFixed(2);
  }
  if (ddEl && typeof data.max_dd === "number") {
    ddEl.textContent = `${(data.max_dd * 100).toFixed(1)}%`;
  }
}

/**
 * Обновить индикатор kill-switch в metrics-bar.
 *
 * @param {any} data
 */
function updateKillSwitchIndicator(data) {
  const el = document.getElementById("kill-switch-indicator");
  if (!el) return;

  const active = !!(data.kill_switch_active ?? data.active);
  el.dataset.state = active ? "tripped" : "ok";

  const labelSpan = el.querySelector("span:last-child");
  if (labelSpan) {
    labelSpan.textContent = active ? "TRIPPED" : "OK";
  }
}

// Инициализация приложения после загрузки DOM.
// Скрипт подключён с defer, но на всякий случай используем DOMContentLoaded.
document.addEventListener("DOMContentLoaded", () => {
  try {
    initApp();
  } catch (err) {
    console.error("[main] Ошибка инициализации приложения", err);
  }
});
