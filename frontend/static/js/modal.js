// frontend/static/js/modal.js

/**
 * Контроллер модального окна с уровнями сигнала.
 *
 * Публичный контракт (по спекации):
 *   - new ModalController(containerId?: string)
 *   - open(signalData)
 *   - close()
 *   - copyLevels()
 *   - renderCalibrationProgress(progress)
 *
 * signalData ожидается объектом с полями:
 *   {
 *     id: string,
 *     symbol: string,
 *     side: 'long' | 'short',
 *     timeframe?: string,
 *     entry: number,
 *     sl: number,
 *     tp1?: number,
 *     tp2?: number,
 *     tp3?: number,
 *     r_multiple?: number,
 *     be_active?: boolean,
 *     be_level?: number | null,
 *     comment?: string,
 *     calibration?: {
 *       current: number,
 *       total: number
 *     }
 *   }
 *
 * Точная форма signalData синхронизируется с tiles.js / backend,
 * здесь только читаем поля без модификации.
 */
class ModalController {
  /**
   * @param {string} containerId - ID корневого контейнера модала (оверлей).
   */
  constructor(containerId = 'modal-container') {
    /** @type {HTMLElement | null} */
    this.container = document.getElementById(containerId);
    if (!this.container) {
      console.warn(
        `[ModalController] Контейнер #${containerId} не найден. Модал работать не будет.`
      );
      return;
    }

    /** @type {HTMLElement | null} */
    this.modalRoot = null;
    /** @type {HTMLElement | null} */
    this.levelsTableBody = null;
    /** @type {HTMLElement | null} */
    this.headerTitle = null;
    /** @type {HTMLElement | null} */
    this.headerSubtitle = null;
    /** @type {HTMLElement | null} */
    this.rMultipleNode = null;
    /** @type {HTMLElement | null} */
    this.beBadgeNode = null;
    /** @type {HTMLElement | null} */
    this.commentNode = null;
    /** @type {HTMLElement | null} */
    this.calibBarFill = null;
    /** @type {HTMLElement | null} */
    this.calibLabel = null;
    /** @type {HTMLElement | null} */
    this.copyButton = null;

    /** @type {any | null} */
    this.currentSignal = null;

    this._handleEsc = this._handleEsc.bind(this);
    this._handleOverlayClick = this._handleOverlayClick.bind(this);
    this._handleCopyClick = this._handleCopyClick.bind(this);
    this._handleCloseClick = this._handleCloseClick.bind(this);

    this._ensureStructure();
  }

  /**
   * Открыть модал и отрендерить данные сигнала.
   * @param {object} signalData
   */
  open(signalData) {
    if (!this.container || !this.modalRoot) return;

    if (typeof signalData !== 'object' || signalData === null) {
      console.warn('[ModalController] open() ожидает объект signalData');
      return;
    }

    this.currentSignal = signalData;
    this._renderHeader(signalData);
    this._renderLevels(signalData);
    this._renderMeta(signalData);
    this.renderCalibrationProgress(signalData.calibration || null);

    this.container.classList.remove('hidden');
    this.container.classList.add('flex');
    document.addEventListener('keydown', this._handleEsc);
  }

  /**
   * Закрыть модальное окно.
   */
  close() {
    if (!this.container) return;

    this.container.classList.add('hidden');
    this.container.classList.remove('flex');
    document.removeEventListener('keydown', this._handleEsc);
    this.currentSignal = null;
  }

  /**
   * Скопировать уровни в буфер обмена в удобочитаемом виде.
   */
  async copyLevels() {
    if (!this.currentSignal) return;

    const text = this._buildCopyText(this.currentSignal);

    // Основной путь — современный Clipboard API
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        this._showCopyFeedback(true);
        return;
      } catch (err) {
        console.warn('[ModalController] Clipboard API ошибка, пробуем fallback', err);
      }
    }

    // Fallback через временное textarea
    try {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.left = '-9999px';
      textarea.style.top = '0';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(textarea);
      this._showCopyFeedback(ok);
    } catch (err) {
      console.warn('[ModalController] Fallback copy ошибка', err);
      this._showCopyFeedback(false);
    }
  }

  /**
   * Обновить прогресс-калибровки.
   *
   * @param {{current: number, total: number} | null} progress
   */
  renderCalibrationProgress(progress) {
    if (!this.calibBarFill || !this.calibLabel) return;

    if (!progress || progress.total <= 0) {
      this.calibBarFill.style.width = '0%';
      this.calibLabel.textContent = 'Калибровка: —';
      return;
    }

    const ratio = Math.max(0, Math.min(1, progress.current / progress.total));
    const percent = Math.round(ratio * 100);

    this.calibBarFill.style.width = `${percent}%`;
    this.calibLabel.textContent = `Калибровка: ${progress.current}/${progress.total} (${percent}%)`;
  }

  // =========================
  // Внутренняя реализация
  // =========================

  _ensureStructure() {
    if (!this.container) return;

    // Если структура уже есть (повторная инициализация) — ищем узлы.
    if (this.container.firstElementChild) {
      this._bindNodes();
      this._bindEvents();
      return;
    }

    this.container.innerHTML = this._template();
    this._bindNodes();
    this._bindEvents();
  }

  _bindNodes() {
    if (!this.container) return;

    this.modalRoot = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-modal-root]')
    );
    this.levelsTableBody = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-levels-body]')
    );
    this.headerTitle = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-modal-title]')
    );
    this.headerSubtitle = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-modal-subtitle]')
    );
    this.rMultipleNode = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-r-multiple]')
    );
    this.beBadgeNode = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-be-badge]')
    );
    this.commentNode = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-comment]')
    );
    this.calibBarFill = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-calib-fill]')
    );
    this.calibLabel = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-calib-label]')
    );
    this.copyButton = /** @type {HTMLElement|null} */ (
      this.container.querySelector('[data-copy-btn]')
    );
  }

  _bindEvents() {
    if (!this.container || !this.modalRoot) return;

    this.container.addEventListener('click', this._handleOverlayClick);
    const closeBtn = this.container.querySelector('[data-close-btn]');
    if (closeBtn) {
      closeBtn.addEventListener('click', this._handleCloseClick);
    }
    if (this.copyButton) {
      this.copyButton.addEventListener('click', this._handleCopyClick);
    }
  }

  _handleEsc(event) {
    if (event.key === 'Escape') {
      this.close();
    }
  }

  _handleOverlayClick(event) {
    // Закрытие по клику на фон, но не по клику внутри карточки.
    if (event.target === this.container) {
      this.close();
    }
  }

  _handleCopyClick(event) {
    event.preventDefault();
    this.copyLevels();
  }

  _handleCloseClick(event) {
    event.preventDefault();
    this.close();
  }

  /**
   * Рендер заголовка и метаинформации (symbol / side / timeframe / id).
   * @param {any} s
   */
  _renderHeader(s) {
    if (this.headerTitle) {
      const sideLabel = s.side === 'short' ? 'SHORT' : 'LONG';
      const symbol = s.symbol || '—';
      this.headerTitle.textContent = `${symbol} · ${sideLabel}`;
    }

    if (this.headerSubtitle) {
      const parts = [];
      if (s.timeframe) parts.push(s.timeframe);
      if (s.id) parts.push(`#${s.id}`);
      this.headerSubtitle.textContent = parts.join(' · ') || '';
    }
  }

  /**
   * Рендер таблицы уровней.
   * @param {any} s
   */
  _renderLevels(s) {
    if (!this.levelsTableBody) return;

    const rows = [];

    const pushRow = (label, price, note) => {
      if (price == null || Number.isNaN(price)) return;
      rows.push({ label, price, note: note || '' });
    };

    pushRow('Entry', s.entry);
    pushRow('SL', s.sl);
    pushRow('TP1', s.tp1);
    pushRow('TP2', s.tp2);
    pushRow('TP3', s.tp3);
    if (s.be_level != null) {
      pushRow('BE', s.be_level, s.be_active ? 'активен' : '');
    }

    this.levelsTableBody.innerHTML = rows
      .map(
        (row) => `
        <tr class="border-b border-slate-800 last:border-0">
          <td class="px-3 py-1.5 text-xs text-slate-400 whitespace-nowrap">${row.label}</td>
          <td class="px-3 py-1.5 text-xs font-mono text-slate-100 whitespace-nowrap">
            ${this._formatPrice(row.price)}
          </td>
          <td class="px-3 py-1.5 text-[11px] text-slate-500">${row.note}</td>
        </tr>
      `
      )
      .join('');
  }

  /**
   * Рендер R-множителя, BE-бейджа и комментария.
   * @param {any} s
   */
  _renderMeta(s) {
    if (this.rMultipleNode) {
      if (s.r_multiple == null || Number.isNaN(s.r_multiple)) {
        this.rMultipleNode.textContent = 'R: —';
      } else {
        this.rMultipleNode.textContent = `R: ${s.r_multiple.toFixed(2)}`;
      }
    }

    if (this.beBadgeNode) {
      const active = !!s.be_active;
      this.beBadgeNode.textContent = active ? 'BE активен' : 'BE не активен';
      this.beBadgeNode.classList.toggle('bg-emerald-500/10', active);
      this.beBadgeNode.classList.toggle('text-emerald-300', active);
      this.beBadgeNode.classList.toggle('bg-slate-800', !active);
      this.beBadgeNode.classList.toggle('text-slate-400', !active);
    }

    if (this.commentNode) {
      const comment = s.comment && String(s.comment).trim();
      if (comment) {
        this.commentNode.textContent = comment;
        this.commentNode.classList.remove('hidden');
      } else {
        this.commentNode.textContent = '';
        this.commentNode.classList.add('hidden');
      }
    }
  }

  /**
   * Построить текст для копирования.
   * @param {any} s
   * @returns {string}
   */
  _buildCopyText(s) {
    const lines = [];

    const symbol = s.symbol || '—';
    const side = s.side === 'short' ? 'SHORT' : 'LONG';
    const tf = s.timeframe ? ` · ${s.timeframe}` : '';
    const idPart = s.id ? ` · #${s.id}` : '';

    lines.push(`${symbol} ${side}${tf}${idPart}`);
    lines.push('');

    const add = (label, value) => {
      if (value == null || Number.isNaN(value)) return;
      lines.push(`${label}: ${this._formatPrice(value)}`);
    };

    add('Entry', s.entry);
    add('SL', s.sl);
    add('TP1', s.tp1);
    add('TP2', s.tp2);
    add('TP3', s.tp3);
    if (s.be_level != null) {
      const suffix = s.be_active ? ' (BE активен)' : '';
      lines.push(`BE: ${this._formatPrice(s.be_level)}${suffix}`);
    }

    if (s.r_multiple != null && !Number.isNaN(s.r_multiple)) {
      lines.push(`R: ${s.r_multiple.toFixed(2)}`);
    }

    if (s.calibration && s.calibration.total > 0) {
      const { current, total } = s.calibration;
      const ratio = Math.max(0, Math.min(1, current / total));
      const percent = Math.round(ratio * 100);
      lines.push(`Калибровка: ${current}/${total} (${percent}%)`);
    }

    if (s.comment) {
      lines.push('');
      lines.push(`Комментарий: ${String(s.comment)}`);
    }

    return lines.join('\n');
  }

  /**
   * Простое форматирование цены (без знания точности символа).
   * @param {number} price
   * @returns {string}
   */
  _formatPrice(price) {
    if (typeof price !== 'number') return String(price);
    // Простейшая эвристика: меньше 1 — 4 знака, иначе 2.
    const decimals = Math.abs(price) < 1 ? 4 : 2;
    return price.toFixed(decimals);
  }

  /**
   * Показать краткий визуальный фидбек по копированию.
   * Здесь только смена текста кнопки на пару секунд.
   * @param {boolean} ok
   */
  _showCopyFeedback(ok) {
    if (!this.copyButton) return;

    const original = this.copyButton.textContent || 'Copy levels';
    this.copyButton.textContent = ok ? 'Скопировано' : 'Ошибка копирования';
    this.copyButton.disabled = true;

    setTimeout(() => {
      if (!this.copyButton) return;
      this.copyButton.textContent = original;
      this.copyButton.disabled = false;
    }, 1500);
  }

  /**
   * HTML-шаблон модального окна.
   * Использует Tailwind-классы; доп. оформление задаётся в styles.css.
   * @returns {string}
   */
  _template() {
    return `
      <div
        data-modal-root
        class="w-full max-w-lg mx-4 rounded-2xl bg-slate-950 border border-slate-800 shadow-xl overflow-hidden"
      >
        <div class="px-4 py-3 border-b border-slate-800 flex items-start justify-between gap-3">
          <div class="flex flex-col gap-0.5">
            <h2
              data-modal-title
              class="text-sm font-semibold text-slate-50 tracking-tight"
            >
              —
            </h2>
            <p
              data-modal-subtitle
              class="text-[11px] text-slate-500"
            ></p>
          </div>
          <div class="flex items-center gap-2">
            <span
              data-r-multiple
              class="inline-flex items-center rounded-full border border-slate-700 px-2 py-0.5 text-[11px] font-mono text-slate-200 bg-slate-900"
            >
              R: —
            </span>
            <button
              type="button"
              data-close-btn
              class="inline-flex h-7 w-7 items-center justify-center rounded-full border border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-100 hover:border-slate-500 text-xs"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        </div>

        <div class="px-4 py-3 space-y-3 text-xs">
          <div class="flex items-center justify-between gap-2">
            <div
              data-be-badge
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-800 text-[11px] text-slate-400"
            >
              BE не активен
            </div>

            <button
              type="button"
              data-copy-btn
              class="inline-flex items-center gap-1 rounded-full bg-emerald-600 hover:bg-emerald-500 text-[11px] font-medium text-white px-3 py-1 disabled:opacity-60 disabled:cursor-default"
            >
              Copy levels
            </button>
          </div>

          <div class="rounded-xl border border-slate-800 bg-slate-900/80 overflow-hidden">
            <table class="w-full border-collapse">
              <thead class="bg-slate-900/90">
                <tr class="text-[11px] text-slate-400 text-left">
                  <th class="px-3 py-1.5 font-normal">Level</th>
                  <th class="px-3 py-1.5 font-normal">Price</th>
                  <th class="px-3 py-1.5 font-normal">Note</th>
                </tr>
              </thead>
              <tbody data-levels-body class="align-middle text-xs">
                <!-- rows injected here -->
              </tbody>
            </table>
          </div>

          <div class="space-y-1">
            <p
              data-comment
              class="text-[11px] text-slate-400 leading-snug"
            ></p>

            <div class="space-y-1">
              <div
                data-calib-label
                class="text-[11px] text-slate-500"
              >
                Калибровка: —
              </div>
              <div class="w-full h-1.5 rounded-full bg-slate-800 overflow-hidden">
                <div
                  data-calib-fill
                  class="h-full w-0 rounded-full bg-emerald-500 transition-all duration-300"
                ></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  }
}

// Экспорт в глобальную область, чтобы main.js / tiles.js могли использовать.
window.ModalController = ModalController;
