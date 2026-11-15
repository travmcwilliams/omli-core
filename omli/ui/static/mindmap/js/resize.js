/* resize.js — make a fixed/absolute-position surface resizable from a corner grip.
 *
 * Companion to drag.js (which owns position); this module owns size. Any floating
 * panel/popover that wants window-like move-and-resize behavior wires both:
 *
 *   - a bottom-right grip element is injected and kept alive by a
 *     MutationObserver, so hosts that rebuild the surface's innerHTML on every
 *     open (popovers usually do) never lose it,
 *   - pointer events with capture, so pen/touch resize like a mouse and a fast
 *     drag can't outrun the grip,
 *   - grip pointerdown stops propagation, so a resize never starts a surface
 *     drag underneath it or a canvas pan/marquee on the page,
 *   - clamped: never below minWidth/minHeight, never past the viewport's
 *     right/bottom edge from the surface's current top-left; re-clamped on
 *     window resize,
 *   - optional localStorage persistence ({w,h} — same convention as drag.js's
 *     {x,y}).
 *
 * makeResizable(el, {minWidth, minHeight, storageKey, onResizeStart, onResizeEnd})
 *   -> {reset(), destroy()}
 */

const R_MARGIN = 4;

const R_CSS = `
.rz-grip{position:absolute;right:0;bottom:0;width:16px;height:16px;z-index:5;
  cursor:nwse-resize;touch-action:none;opacity:.5;
  background:repeating-linear-gradient(135deg,transparent 0 3px,var(--dim,#8aa0bd) 3px 4.5px);
  clip-path:polygon(100% 0,100% 100%,0 100%);border-bottom-right-radius:inherit}
.rz-grip:hover{opacity:1}
`;

function ensureCss() {
  if (document.getElementById('rz-css')) return;
  const s = document.createElement('style');
  s.id = 'rz-css';
  s.textContent = R_CSS;
  document.head.appendChild(s);
}

export function makeResizable(el, opts = {}) {
  const {
    minWidth = 160,
    minHeight = 100,
    storageKey = null,
    onResizeStart = () => {},
    onResizeEnd = () => {},
    resizeClass = 'resizing',
  } = opts;
  if (!el) return { reset() {}, destroy() {} };

  ensureCss();
  // The grip anchors to the surface's own box; a flex-centred modal card is
  // position:static, so give it a positioning context without moving it.
  try { if (getComputedStyle(el).position === 'static') el.style.position = 'relative'; } catch (e) {}
  const grip = document.createElement('div');
  grip.className = 'rz-grip';
  el.appendChild(grip);
  // Hosts rebuild popover innerHTML on every open — re-adopt the grip when lost.
  const mo = new MutationObserver(() => { if (!el.contains(grip)) el.appendChild(grip); });
  mo.observe(el, { childList: true });

  let size = null;   // {w, h} once we own the size; null while CSS owns it

  function apply(w, h, clampToView = true) {
    w = Math.max(minWidth, w);
    h = Math.max(minHeight, h);
    if (clampToView) {
      const r = el.getBoundingClientRect();
      w = Math.min(w, Math.max(minWidth, innerWidth - r.left - R_MARGIN));
      h = Math.min(h, Math.max(minHeight, innerHeight - r.top - R_MARGIN));
    }
    // Own the size fully: a stylesheet max-width/max-height would fight the
    // explicit size we set here.
    el.style.width = w + 'px';
    el.style.height = h + 'px';
    el.style.maxWidth = 'none';
    el.style.maxHeight = 'none';
    return (size = { w, h });
  }

  if (storageKey) {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
      // no viewport clamp at restore: hidden surfaces have no rect yet
      if (saved && isFinite(saved.w) && isFinite(saved.h)) apply(saved.w, saved.h, false);
    } catch (e) { /* unreadable storage is not worth failing the page over */ }
  }

  const onWinResize = () => { if (size) apply(size.w, size.h); };
  addEventListener('resize', onWinResize);

  let grab = null;

  const down = e => {
    if (e.button !== undefined && e.button !== 0) return;
    const r = el.getBoundingClientRect();
    grab = { w0: r.width, h0: r.height, x0: e.clientX, y0: e.clientY, id: e.pointerId };
    el.classList.add(resizeClass);
    try { grip.setPointerCapture(e.pointerId); } catch (err) {}
    onResizeStart();
    e.preventDefault();
    e.stopPropagation();   // never start a surface drag or a canvas gesture underneath
  };

  const move = e => {
    if (!grab || e.pointerId !== grab.id) return;
    apply(grab.w0 + (e.clientX - grab.x0), grab.h0 + (e.clientY - grab.y0));
  };

  const up = e => {
    if (!grab || e.pointerId !== grab.id) return;
    try { grip.releasePointerCapture(grab.id); } catch (err) {}
    grab = null;
    el.classList.remove(resizeClass);
    onResizeEnd();
    if (storageKey && size) {
      try { localStorage.setItem(storageKey, JSON.stringify(size)); } catch (e) {}
    }
  };

  grip.addEventListener('pointerdown', down);
  grip.addEventListener('pointermove', move);
  grip.addEventListener('pointerup', up);
  grip.addEventListener('pointercancel', up);

  return {
    reset() {
      size = null;
      el.style.width = el.style.height = el.style.maxWidth = el.style.maxHeight = '';
      if (storageKey) { try { localStorage.removeItem(storageKey); } catch (e) {} }
    },
    destroy() {
      mo.disconnect();
      removeEventListener('resize', onWinResize);
      grip.removeEventListener('pointerdown', down);
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up);
      grip.removeEventListener('pointercancel', up);
      grip.remove();
    },
  };
}
