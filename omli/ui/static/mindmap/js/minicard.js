/**
 * Mini-card hover/focus tooltips — singleton manager per page.
 * Zero deps; injects its own CSS once (class prefix mc-).
 */

/** Fail-closed preview gate on a node's sensitivity tags (the core's labels): a node with
 *  no tag may show a preview; a tagged node only when every tag is public or internal.
 *  Any other or unknown tag, including local-only, hides the preview. */
const PREVIEW_LABELS = ['public', 'internal'];
export function allowPreview(node) {
  if (node == null) return false;
  const s = node.sensitivity;
  const tags = Array.isArray(s) ? s : (s == null || s === '' ? [] : [s]);
  return tags.every(t => PREVIEW_LABELS.includes(t));
}

const CSS_ID = 'mc-minicard-css';
const STYLE = `
.mc-card{position:fixed;z-index:20;pointer-events:none;background:#0a0e16f2;color:var(--ink,#e7edf5);
  border:1px solid var(--line,#243245);border-radius:10px;padding:8px 12px;font-size:12px;
  display:none;box-shadow:0 6px 18px #000a;max-width:380px;line-height:1.45;
  font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.mc-card.mc-on{display:block}
.mc-title{font-weight:700;word-break:break-all}
.mc-sub{color:var(--dim,#8aa0bd);margin-top:2px;font-size:11.5px}
.mc-row{color:var(--dim,#8aa0bd);display:flex;align-items:baseline;gap:7px;margin-top:2px}
.mc-row .mc-lbl{flex-shrink:0}
.mc-row .mc-val{color:var(--ink,#e7edf5);font-weight:600;font-variant-numeric:tabular-nums;word-break:break-word}
.mc-expand{margin-top:4px;padding-top:4px;border-top:1px solid var(--line,#243245)}
.mc-expand .mc-row:first-child{margin-top:0}
.mc-expand .mc-val{font-weight:500;font-style:italic}
`.trim();

let _singleton = null;

function injectCss() {
  if (document.getElementById(CSS_ID)) return;
  const s = document.createElement('style');
  s.id = CSS_ID;
  s.textContent = STYLE;
  document.head.appendChild(s);
}

function text(el, s) {
  el.textContent = s == null ? '' : String(s);
}

function clearKids(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function appendRows(parent, rows) {
  for (const r of rows) {
    const row = document.createElement('div');
    row.className = 'mc-row';
    const lbl = document.createElement('span');
    lbl.className = 'mc-lbl';
    text(lbl, r.label != null && r.label !== '' ? r.label + ' ·' : '');
    const val = document.createElement('span');
    val.className = 'mc-val';
    text(val, r.value);
    row.appendChild(lbl);
    row.appendChild(val);
    parent.appendChild(row);
  }
}

function filterFacts(facts, max) {
  const out = [];
  for (const f of facts || []) {
    if (f && f.sensitive === true) continue;
    out.push(f);
    if (max != null && out.length >= max) break;
  }
  return out;
}

/**
 * @param {{delayL1?: number, delayL2?: number}} opts
 * @returns {ReturnType<typeof buildManager>}
 */
export function createMiniCard(opts = {}) {
  if (_singleton) return _singleton;

  const delayL1 = opts.delayL1 ?? 400;
  const delayL2 = opts.delayL2 ?? 1000;
  const LONG_PRESS_MS = 500;
  const MOVE_THRESH = 10;

  injectCss();

  const card = document.createElement('div');
  card.className = 'mc-card';
  card.setAttribute('role', 'tooltip');
  card.setAttribute('aria-hidden', 'true');
  const elTitle = document.createElement('div');
  elTitle.className = 'mc-title';
  const elSub = document.createElement('div');
  elSub.className = 'mc-sub';
  const elFacts = document.createElement('div');
  elFacts.className = 'mc-facts';
  const elExpand = document.createElement('div');
  elExpand.className = 'mc-expand';
  elExpand.hidden = true;
  card.appendChild(elTitle);
  card.appendChild(elSub);
  card.appendChild(elFacts);
  card.appendChild(elExpand);
  document.body.appendChild(card);

  let suppressed = false;
  let currentId = null;
  let level = 0; // 0 hidden, 1 L1, 2 L2
  let payload = null;
  let l1Timer = null;
  let l2Timer = null;
  let longTimer = null;
  let pressX = null;
  let pressY = null;
  let lastClientX = 0;
  let lastClientY = 0;
  let destroyed = false;
  /** @type {Array<{el: Element, onIn: Function, onKey: Function}>} */
  const focusBinds = [];

  function clearLevelTimers() {
    if (l1Timer != null) { clearTimeout(l1Timer); l1Timer = null; }
    if (l2Timer != null) { clearTimeout(l2Timer); l2Timer = null; }
  }

  function clearTimers() {
    clearLevelTimers();
    if (longTimer != null) { clearTimeout(longTimer); longTimer = null; }
  }

  function place(clientX, clientY) {
    lastClientX = clientX;
    lastClientY = clientY;
    // measure while visible (or briefly force layout)
    const wasOn = card.classList.contains('mc-on');
    if (!wasOn) {
      card.style.visibility = 'hidden';
      card.classList.add('mc-on');
    }
    const w = card.offsetWidth;
    const h = card.offsetHeight;
    if (!wasOn) {
      card.classList.remove('mc-on');
      card.style.visibility = '';
    }
    const margin = 12;
    let x = clientX + 16;
    let y = clientY + 14;
    // flip left / above when overflowing
    if (x + w + margin > innerWidth) x = clientX - w - 16;
    if (y + h + margin > innerHeight) y = clientY - h - 14;
    x = Math.max(margin, Math.min(innerWidth - w - margin, x));
    y = Math.max(margin, Math.min(innerHeight - h - margin, y));
    card.style.left = x + 'px';
    card.style.top = y + 'px';
  }

  function render(p, lvl) {
    text(elTitle, p.title || '');
    if (p.subtitle) {
      elSub.hidden = false;
      text(elSub, p.subtitle);
    } else {
      elSub.hidden = true;
      text(elSub, '');
    }
    clearKids(elFacts);
    appendRows(elFacts, filterFacts(p.facts, 3));
    clearKids(elExpand);
    if (lvl >= 2) {
      const exp = filterFacts(p.expand, null);
      if (exp.length) {
        elExpand.hidden = false;
        appendRows(elExpand, exp);
      } else {
        elExpand.hidden = true;
      }
    } else {
      elExpand.hidden = true;
    }
  }

  function showAt(lvl, p, clientX, clientY) {
    if (destroyed || suppressed) return;
    payload = p;
    level = lvl;
    render(p, lvl);
    card.classList.add('mc-on');
    card.setAttribute('aria-hidden', 'false');
    place(clientX, clientY);
  }

  function showL1(p, clientX, clientY) {
    showAt(1, p, clientX, clientY);
    if (l2Timer != null) clearTimeout(l2Timer);
    l2Timer = setTimeout(() => {
      l2Timer = null;
      if (destroyed || suppressed || currentId !== p.id) return;
      showAt(2, p, lastClientX, lastClientY);
    }, delayL2);
  }

  function hideNow() {
    clearTimers();
    currentId = null;
    payload = null;
    level = 0;
    pressX = pressY = null;
    card.classList.remove('mc-on');
    card.setAttribute('aria-hidden', 'true');
  }

  function armL1(p, clientX, clientY) {
    if (l1Timer != null) clearTimeout(l1Timer);
    if (l2Timer != null) clearTimeout(l2Timer);
    l1Timer = setTimeout(() => {
      l1Timer = null;
      if (destroyed || suppressed || currentId !== p.id) return;
      showL1(p, lastClientX, lastClientY);
    }, delayL1);
  }

  function normalize(raw) {
    if (!raw || raw.id == null) return null;
    if (raw.sensitive === true) return null;
    return {
      id: String(raw.id),
      title: raw.title ?? '',
      subtitle: raw.subtitle ?? '',
      facts: Array.isArray(raw.facts) ? raw.facts : [],
      expand: Array.isArray(raw.expand) ? raw.expand : [],
      sensitive: !!raw.sensitive,
    };
  }

  /**
   * Host calls on pointermove (and optionally pointerdown for long-press).
   * @param {PointerEvent|MouseEvent} e
   * @param {() => (null|{id,title,subtitle,facts,expand,sensitive})} resolveTarget
   */
  function schedule(e, resolveTarget) {
    if (destroyed) return;
    if (suppressed) {
      hideNow();
      return;
    }
    if (!e || typeof resolveTarget !== 'function') return;

    lastClientX = e.clientX;
    lastClientY = e.clientY;

    // long-press cancel on move past threshold
    if (longTimer != null && pressX != null) {
      if (Math.hypot(e.clientX - pressX, e.clientY - pressY) > MOVE_THRESH) {
        clearTimeout(longTimer);
        longTimer = null;
        pressX = pressY = null;
      }
    }

    // pointerdown (or touch contact): arm long-press → L1
    if (e.type === 'pointerdown') {
      pressX = e.clientX;
      pressY = e.clientY;
      if (longTimer != null) clearTimeout(longTimer);
      const resolve = resolveTarget;
      longTimer = setTimeout(() => {
        longTimer = null;
        if (destroyed || suppressed) return;
        let raw;
        try { raw = resolve(); } catch (_) { return; }
        const p = normalize(raw);
        if (!p) return;
        currentId = p.id;
        showL1(p, lastClientX, lastClientY);
      }, LONG_PRESS_MS);
    }

    let raw;
    try { raw = resolveTarget(); } catch (_) { hideNow(); return; }
    const p = normalize(raw);

    if (!p) {
      hideNow();
      return;
    }

    if (p.id === currentId) {
      // same target: keep timers; update position if visible
      if (level >= 1) place(e.clientX, e.clientY);
      return;
    }

    // new id — reset level timers (keep any long-press armed this same event)
    clearLevelTimers();
    currentId = p.id;
    level = 0;
    card.classList.remove('mc-on');
    card.setAttribute('aria-hidden', 'true');
    payload = p;
    armL1(p, e.clientX, e.clientY);
  }

  /** While true (drag/pan/zoom/menus): instant hide, ignore schedule. */
  function suppress(on) {
    suppressed = !!on;
    if (suppressed) hideNow();
  }

  function hide() {
    hideNow();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    hideNow();
    for (const b of focusBinds) {
      b.el.removeEventListener('focusin', b.onIn);
      b.el.removeEventListener('keydown', b.onKey);
    }
    focusBinds.length = 0;
    if (card.parentNode) card.parentNode.removeChild(card);
    if (_singleton === manager) _singleton = null;
  }

  /**
   * Keyboard: focusin → L1 immediately; Escape → hide.
   * @param {Element} el
   * @param {() => (null|object)} resolveTarget
   */
  function attachFocus(el, resolveTarget) {
    if (!el || typeof resolveTarget !== 'function') return;
    const onIn = () => {
      if (destroyed || suppressed) return;
      let raw;
      try { raw = resolveTarget(); } catch (_) { return; }
      const p = normalize(raw);
      if (!p) return;
      clearTimers();
      currentId = p.id;
      const r = (document.activeElement || el).getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.bottom;
      showL1(p, cx, cy);
    };
    const onKey = (ev) => {
      if (ev.key === 'Escape') hideNow();
    };
    el.addEventListener('focusin', onIn);
    el.addEventListener('keydown', onKey);
    focusBinds.push({ el, onIn, onKey });
  }

  const manager = { schedule, suppress, hide, destroy, attachFocus };
  _singleton = manager;
  return manager;
}
