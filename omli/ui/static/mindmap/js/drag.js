/* drag.js — make a fixed-position element draggable by a handle.
 *
 * Every floating panel on the page wants the same behavior, so it lives here once:
 *
 *   - pointer events, so pen/touch drag like a mouse and capture survives the
 *     pointer leaving the element mid-drag,
 *   - clamped to the viewport on all four sides, and re-clamped on resize, so a
 *     surface can never be stranded off-screen,
 *   - optional slop threshold, so an element that is itself a button can tell a
 *     click from a drag (below slop it stays a click; past it the trailing click
 *     is swallowed),
 *   - optional localStorage persistence.
 *
 * Hosts must pass onDragStart/onDragEnd to suppress hover mini-cards during a
 * drag: a hover card never shows while something moves.
 *
 * makeDraggable(el, {handle, ignore, slop, storageKey, onDragStart, onDragEnd})
 *   -> {reset(), destroy()}
 */

const MARGIN = 4;

export function makeDraggable(el, opts = {}) {
  const {
    handle = el,
    ignore = null,          // selector: a pointerdown inside it never starts a drag
    slop = 0,               // px of movement before it counts as a drag
    storageKey = null,
    onDragStart = () => {},
    onDragEnd = () => {},
    dragClass = 'dragging',
  } = opts;
  if (!el || !handle) return { reset() {}, destroy() {} };

  let pos = null;   // {x, y} once placed by us; null while CSS owns the position

  function place(x, y) {
    const w = el.offsetWidth, h = el.offsetHeight;
    x = Math.min(Math.max(MARGIN, x), Math.max(MARGIN, innerWidth - w - MARGIN));
    y = Math.min(Math.max(MARGIN, y), Math.max(MARGIN, innerHeight - h - MARGIN));
    // Take full ownership of placement: a leftover right/bottom from the
    // stylesheet would fight the left/top we set here.
    el.style.left = x + 'px';
    el.style.top = y + 'px';
    el.style.right = 'auto';
    el.style.bottom = 'auto';
    return (pos = { x, y });
  }

  if (storageKey) {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
      if (saved && isFinite(saved.x) && isFinite(saved.y)) place(saved.x, saved.y);
    } catch (e) { /* unreadable storage is not worth failing the page over */ }
  }

  const onResize = () => { if (pos) place(pos.x, pos.y); };
  addEventListener('resize', onResize);

  let grab = null, moved = false;

  const down = e => {
    if (e.button !== 0) return;
    if (ignore && e.target.closest && e.target.closest(ignore)) return;
    const r = el.getBoundingClientRect();
    grab = { dx: e.clientX - r.left, dy: e.clientY - r.top, x0: e.clientX, y0: e.clientY, id: e.pointerId };
    moved = false;
    // Capture on the handle: a fast drag outpaces the element without it.
    try { handle.setPointerCapture(e.pointerId); } catch (err) {}
    if (!slop) start();
    e.preventDefault();
  };

  const start = () => { moved = true; el.classList.add(dragClass); onDragStart(); };

  const move = e => {
    if (!grab || e.pointerId !== grab.id) return;
    if (!moved) {
      if (Math.hypot(e.clientX - grab.x0, e.clientY - grab.y0) < slop) return;
      start();
    }
    place(e.clientX - grab.dx, e.clientY - grab.dy);
  };

  const up = e => {
    if (!grab || e.pointerId !== grab.id) return;
    try { handle.releasePointerCapture(grab.id); } catch (err) {}
    grab = null;
    if (!moved) return;
    el.classList.remove(dragClass);
    onDragEnd();
    if (storageKey && pos) {
      try { localStorage.setItem(storageKey, JSON.stringify(pos)); } catch (e) {}
    }
  };

  // Capture phase: swallow the click that trails a real drag before it reaches
  // the element's own click handler (a handle may also act as a button).
  const clickGuard = e => { if (moved) { e.stopPropagation(); moved = false; } };

  handle.addEventListener('pointerdown', down);
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up);
  handle.addEventListener('pointercancel', up);
  el.addEventListener('click', clickGuard, true);

  return {
    reset() {
      pos = null;
      el.style.left = el.style.top = el.style.right = el.style.bottom = '';
      if (storageKey) { try { localStorage.removeItem(storageKey); } catch (e) {} }
    },
    destroy() {
      removeEventListener('resize', onResize);
      handle.removeEventListener('pointerdown', down);
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      el.removeEventListener('click', clickGuard, true);
    },
  };
}
