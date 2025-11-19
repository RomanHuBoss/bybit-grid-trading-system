// frontend/static/js/tiles.js

/**
 * Модуль рендеринга плиток сигналов AVI-5.
 *
 * Никакой сетевой логики, только работа с DOM.
 * Вся интеграция с API / SSE делается в main.js.
 */

/**
 * @typedef {Object} TileSignal
 * @property {string|number} id            Уникальный идентификатор сигнала
 * @property {string} symbol               Торговый инструмент (например, "BTCUSDT")
 * @property {"LONG"|"SHORT"|string} side  Направление
 * @property {number} probability          Вероятность (0..1 или 0..100 — интерпретация остаётся за main.js)
 * @property {number} entry_price          Цена входа
 * @property {number|null|undefined} tp    Take Profit
 * @property {number|null|undefined} sl    Stop Loss
 * @property {number|null|undefined} r     Ожидаемый R/R
 * @property {string} status               Статус сигнала (NEW, OPEN, FILLED, CANCELED и т.п.)
 * @property {string|number|null} position_id Связанная позиция, если есть
 * @property {number|undefined} created_ts UNIX timestamp (секунды или миллисекунды)
 */

/**
 * @typedef {Object} TilePosition
 * @property {string|number} id
 * @property {string|number} signal_id
 * @property {string} status
 */

/**
 * @typedef {Object} TilesFilters
 * @property {number|undefined} minProbability      Порог вероятности (в тех же единицах, что и signal.probability)
 * @property {string[]|undefined} allowedSymbols    Список разрешённых символов (если пустой или undefined — все)
 */

/**
 * @typedef {Object} TilesCallbacks
 * @property {(signal: TileSignal) => void} [onTileClick]           Клик по плитке для открытия модала
 * @property {(signal: TileSignal) => void} [onClosePositionClick]  Клик по кнопке "Close" на плитке
 */

/**
 * @typedef {Object} TileRecord
 * @property {TileSignal} signal
 * @property {HTMLDivElement} element
 */

/**
 * Классы для разных статусов/направлений. Реальные имена завязаны на CSS из шаблона.
 */
const SIDE_CLASSES = {
  LONG: "tile--long",
  SHORT: "tile--short",
};

const STATUS_CLASSES = {
  NEW: "tile--status-new",
  OPEN: "tile--status-open",
  FILLED: "tile--status-filled",
  CLOSED: "tile--status-closed",
  CANCELED: "tile--status-canceled",
};

/**
 * Контроллер плиток: управляет коллекцией сигналов и их DOM-представлением.
 */
export class TilesController {
  /**
   * @param {HTMLElement} containerEl
   * @param {TilesCallbacks} [callbacks]
   */
  constructor(containerEl, callbacks = {}) {
    if (!(containerEl instanceof HTMLElement)) {
      throw new TypeError("TilesController: containerEl должен быть HTMLElement");
    }

    /** @type {HTMLElement} */
    this.containerEl = containerEl;

    /** @type {Map<string, TileRecord>} */
    this._tiles = new Map();

    /** @type {TilesFilters} */
    this._filters = {
      minProbability: undefined,
      allowedSymbols: undefined,
    };

    /** @type {TilesCallbacks} */
    this._callbacks = callbacks;
  }

  /**
   * Полная очистка контейнера и стейта.
   */
  clear() {
    this._tiles.clear();
    this.containerEl.innerHTML = "";
  }

  /**
   * Обновить фильтры отображения.
   *
   * @param {Partial<TilesFilters>} filters
   */
  setFilters(filters) {
    this._filters = {
      ...this._filters,
      ...(filters || {}),
    };

    // Применяем фильтры к уже существующим плиткам
    for (const record of this._tiles.values()) {
      this._applyVisibility(record);
    }
  }

  /**
   * Добавить или обновить сигнал.
   *
   * @param {TileSignal} signal
   */
  upsertSignal(signal) {
    const id = String(signal.id);

    let record = this._tiles.get(id);
    if (!record) {
      const el = this._createTileElement(signal);
      record = { signal, element: el };
      this._tiles.set(id, record);
      this.containerEl.prepend(el);
    } else {
      record.signal = { ...record.signal, ...signal };
      this._updateTileElement(record);
    }

    this._applyVisibility(record);
  }

  /**
   * Обновить позицию и связанные плитки по событию позиции.
   *
   * @param {TilePosition} position
   */
  updatePosition(position) {
    const signalId = position.signal_id != null ? String(position.signal_id) : null;
    if (!signalId) {
      return;
    }

    const record = this._tiles.get(signalId);
    if (!record) {
      return;
    }

    record.signal = {
      ...record.signal,
      position_id: position.id,
      status: position.status || record.signal.status,
    };

    this._updateTileElement(record);
  }

  /**
   * Удалить сигнал/плитку.
   *
   * @param {string|number} id
   */
  removeSignal(id) {
    const key = String(id);
    const record = this._tiles.get(key);
    if (!record) {
      return;
    }
    if (record.element.parentElement === this.containerEl) {
      this.containerEl.removeChild(record.element);
    }
    this._tiles.delete(key);
  }

  /**
   * Вернуть текущие сигналы (копия массива).
   *
   * @returns {TileSignal[]}
   */
  getSignals() {
    return Array.from(this._tiles.values()).map((r) => ({ ...r.signal }));
  }

  // ---------------------------------------------------------------------------
  // Внутренняя логика рендеринга
  // ---------------------------------------------------------------------------

  /**
   * @param {TileSignal} signal
   * @returns {HTMLDivElement}
   * @private
   */
  _createTileElement(signal) {
    const el = document.createElement("div");
    el.className = "tile";
    el.dataset.signalId = String(signal.id);
    el.dataset.symbol = signal.symbol;

    const header = document.createElement("div");
    header.className = "tile__header";

    const title = document.createElement("div");
    title.className = "tile__symbol";
    title.textContent = signal.symbol;

    const side = document.createElement("div");
    side.className = "tile__side";
    side.textContent = normalizeSideLabel(signal.side);

    header.appendChild(title);
    header.appendChild(side);

    const body = document.createElement("div");
    body.className = "tile__body";

    const levelsRow = document.createElement("div");
    levelsRow.className = "tile__row tile__row--levels";

    const entry = document.createElement("span");
    entry.className = "tile__level tile__level--entry";
    entry.textContent = formatPrice(signal.entry_price);

    const tp = document.createElement("span");
    tp.className = "tile__level tile__level--tp";
    tp.textContent = signal.tp != null ? formatPrice(signal.tp) : "—";

    const sl = document.createElement("span");
    sl.className = "tile__level tile__level--sl";
    sl.textContent = signal.sl != null ? formatPrice(signal.sl) : "—";

    levelsRow.appendChild(entry);
    levelsRow.appendChild(tp);
    levelsRow.appendChild(sl);

    const metaRow = document.createElement("div");
    metaRow.className = "tile__row tile__row--meta";

    const prob = document.createElement("span");
    prob.className = "tile__prob";
    prob.textContent = formatProbability(signal.probability);

    const rValue = document.createElement("span");
    rValue.className = "tile__r";
    rValue.textContent = signal.r != null ? `R=${signal.r.toFixed(2)}` : "";

    const status = document.createElement("span");
    status.className = "tile__status";
    status.textContent = signal.status;

    metaRow.appendChild(prob);
    metaRow.appendChild(rValue);
    metaRow.appendChild(status);

    const footer = document.createElement("div");
    footer.className = "tile__footer";

    const btnDetails = document.createElement("button");
    btnDetails.type = "button";
    btnDetails.className = "tile__btn tile__btn--details";
    btnDetails.textContent = "Details";

    const btnClose = document.createElement("button");
    btnClose.type = "button";
    btnClose.className = "tile__btn tile__btn--close";
    btnClose.textContent = "Close";

    footer.appendChild(btnDetails);
    footer.appendChild(btnClose);

    body.appendChild(levelsRow);
    body.appendChild(metaRow);

    el.appendChild(header);
    el.appendChild(body);
    el.appendChild(footer);

    // Сохраняем ссылки на важные элементы для быстрых обновлений
    /** @type {any} */
    const refs = {
      header,
      side,
      levelsRow,
      entry,
      tp,
      sl,
      metaRow,
      prob,
      rValue,
      status,
      footer,
      btnDetails,
      btnClose,
    };
    // Храним их на DOM-элементе, чтобы не плодить map’ы.
    // Это не публичный контракт, чисто внутренняя оптимизация.
    // eslint-disable-next-line no-underscore-dangle
    el._tileRefs = refs;

    // Обработчики событий
    btnDetails.addEventListener("click", (evt) => {
      evt.stopPropagation();
      if (typeof this._callbacks.onTileClick === "function") {
        this._callbacks.onTileClick(this._tiles.get(String(signal.id)).signal);
      }
    });

    btnClose.addEventListener("click", (evt) => {
      evt.stopPropagation();
      if (typeof this._callbacks.onClosePositionClick === "function") {
        this._callbacks.onClosePositionClick(this._tiles.get(String(signal.id)).signal);
      }
    });

    el.addEventListener("click", () => {
      if (typeof this._callbacks.onTileClick === "function") {
        this._callbacks.onTileClick(this._tiles.get(String(signal.id)).signal);
      }
    });

    this._applyBaseClasses(signal, el);

    return el;
  }

  /**
   * @param {TileRecord} record
   * @private
   */
  _updateTileElement(record) {
    const { signal, element } = record;
    /** @type {any} */
    // eslint-disable-next-line no-underscore-dangle
    const refs = element._tileRefs || {};

    element.dataset.symbol = signal.symbol;

    if (refs.side) {
      refs.side.textContent = normalizeSideLabel(signal.side);
    }
    if (refs.entry) {
      refs.entry.textContent = formatPrice(signal.entry_price);
    }
    if (refs.tp) {
      refs.tp.textContent = signal.tp != null ? formatPrice(signal.tp) : "—";
    }
    if (refs.sl) {
      refs.sl.textContent = signal.sl != null ? formatPrice(signal.sl) : "—";
    }
    if (refs.prob) {
      refs.prob.textContent = formatProbability(signal.probability);
    }
    if (refs.rValue) {
      refs.rValue.textContent = signal.r != null ? `R=${signal.r.toFixed(2)}` : "";
    }
    if (refs.status) {
      refs.status.textContent = signal.status;
    }

    this._applyBaseClasses(signal, element);
  }

  /**
   * Применить классы для side/status.
   *
   * @param {TileSignal} signal
   * @param {HTMLDivElement} element
   * @private
   */
  _applyBaseClasses(signal, element) {
    // Сбрасываем все side/status-классы
    for (const cls of Object.values(SIDE_CLASSES)) {
      element.classList.remove(cls);
    }
    for (const cls of Object.values(STATUS_CLASSES)) {
      element.classList.remove(cls);
    }

    const sideClass = SIDE_CLASSES[signal.side] || null;
    if (sideClass) {
      element.classList.add(sideClass);
    }

    const upperStatus = typeof signal.status === "string" ? signal.status.toUpperCase() : "";
    const statusClass = STATUS_CLASSES[upperStatus];
    if (statusClass) {
      element.classList.add(statusClass);
    }
  }

  /**
   * Применить фильтры видимости к конкретной плитке.
   *
   * @param {TileRecord} record
   * @private
   */
  _applyVisibility(record) {
    const { signal, element } = record;
    const { minProbability, allowedSymbols } = this._filters;

    let visible = true;

    if (minProbability != null) {
      const p = signal.probability;
      if (typeof p === "number") {
        visible = visible && p >= minProbability;
      }
    }

    if (visible && Array.isArray(allowedSymbols) && allowedSymbols.length > 0) {
      visible = allowedSymbols.includes(signal.symbol);
    }

    element.style.display = visible ? "" : "none";
  }
}

/**
 * Небольшой helper: человекочитаемое направление.
 *
 * @param {string} side
 * @returns {string}
 */
function normalizeSideLabel(side) {
  const s = String(side || "").toUpperCase();
  if (s === "LONG") return "LONG ↑";
  if (s === "SHORT") return "SHORT ↓";
  return s || "?";
}

/**
 * Форматирование цены.
 *
 * @param {number} value
 * @returns {string}
 */
function formatPrice(value) {
  if (typeof value !== "number" || !isFinite(value)) {
    return "—";
  }
  // Дальнейшая точность может быть уточнена в спеке; оставим 2 знака как разумный дефолт.
  return value.toFixed(2);
}

/**
 * Форматирование вероятности.
 *
 * Спека не навязывает, в каких единицах прилетает probability:
 *  - 0..1 или 0..100.
 * Будем адаптивны:
 *  - если p <= 1 — считаем, что это доля, и умножаем на 100;
 *  - если > 1 — считаем, что это уже проценты.
 *
 * @param {number} p
 * @returns {string}
 */
function formatProbability(p) {
  if (typeof p !== "number" || !isFinite(p)) {
    return "—";
  }
  const value = p <= 1 ? p * 100 : p;
  return `${value.toFixed(1)}%`;
}

/**
 * Фабрика-обёртка, если не хочется писать `new TilesController(...)`.
 *
 * @param {HTMLElement} containerEl
 * @param {TilesCallbacks} [callbacks]
 * @returns {TilesController}
 */
export function initTiles(containerEl, callbacks) {
  return new TilesController(containerEl, callbacks);
}
