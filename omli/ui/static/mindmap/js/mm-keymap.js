/* Declarative keymap + command palette (Ctrl+K) + shortcut cheat sheet (?).

   The canvas registers commands ({id, label, group, keys, when, global, run});
   this module owns key normalization, dispatch, and the two overlays. Styles
   and DOM are injected here so the host page stays clean. No dependencies. */

const CSS = `
#mmkOverlay,#mmkCheat{position:fixed;inset:0;z-index:200;display:none;
  background:#0a0e1488;backdrop-filter:blur(2px)}
#mmkOverlay.open,#mmkCheat.open{display:flex;align-items:flex-start;justify-content:center}
.mmk-box{margin-top:12vh;width:min(560px,92vw);background:var(--panel,#121a24);
  border:1px solid var(--line,#2a3647);border-radius:14px;box-shadow:0 18px 50px #000c;
  overflow:hidden}
.mmk-box input{width:100%;background:#0e1622;border:none;border-bottom:1px solid var(--line,#2a3647);
  color:var(--ink,#e7edf5);padding:13px 16px;font:14px/1.3 "Segoe UI",system-ui,sans-serif;outline:none}
.mmk-list{max-height:46vh;overflow-y:auto;padding:6px}
.mmk-item{display:flex;align-items:center;gap:10px;padding:8px 11px;border-radius:9px;
  cursor:pointer;color:var(--ink,#e7edf5);font-size:13px}
.mmk-item.sel,.mmk-item:hover{background:#0e1622}
.mmk-item .grp{font-size:9px;color:var(--dim,#8aa0bd);text-transform:uppercase;
  letter-spacing:.6px;min-width:58px}
.mmk-item .sp{flex:1}
.mmk-item kbd,.mmk-cheat kbd{background:#0e1622;border:1px solid var(--line,#2a3647);
  border-bottom-width:2px;border-radius:5px;padding:1px 7px;font:11px/1.5 Consolas,monospace;
  color:var(--dim,#8aa0bd);margin-left:4px}
.mmk-empty{padding:18px;text-align:center;color:var(--dim,#8aa0bd);font-size:12px}
.mmk-cheat{align-items:center}
.mmk-cheat .card{margin:0;width:min(680px,94vw);max-height:80vh;overflow-y:auto;
  background:var(--panel,#121a24);border:1px solid var(--line,#2a3647);border-radius:14px;
  box-shadow:0 18px 50px #000c;padding:18px 22px;color:var(--ink,#e7edf5)}
.mmk-cheat h2{margin:0 0 4px;font-size:15px}
.mmk-cheat h2 small{color:var(--dim,#8aa0bd);font-weight:400;font-size:11px;margin-left:8px}
.mmk-cheat h3{margin:14px 0 6px;font-size:10px;color:var(--dim,#8aa0bd);
  text-transform:uppercase;letter-spacing:1px}
.mmk-cheat .row{display:flex;justify-content:space-between;align-items:center;
  padding:4px 0;font-size:12.5px;gap:14px}
.mmk-cheat .cols{display:grid;grid-template-columns:1fr 1fr;gap:0 34px}
/* right-click context menu (registry-driven) + its hover-expand flyouts */
#mmkMenu,.mmk-sub{position:fixed;z-index:210;display:none;min-width:196px;
  background:var(--panel,#121a24);border:1px solid var(--line,#2a3647);border-radius:10px;
  box-shadow:0 14px 40px #000c;padding:5px;color:var(--ink,#e7edf5);
  font:13px/1.3 "Segoe UI",system-ui,sans-serif;user-select:none}
.mmk-sub{z-index:211}
#mmkMenu.open,.mmk-sub.open{display:block}
.mmk-title{font-size:9px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim,#8aa0bd);padding:5px 10px 4px}
.mmk-mrow{display:flex;align-items:center;gap:9px;padding:7px 10px;border-radius:7px;cursor:pointer;white-space:nowrap}
.mmk-mrow:hover,.mmk-mrow.active{background:#0e1622}
.mmk-mrow .mi{width:14px;text-align:center;color:var(--dim,#8aa0bd);flex:none}
.mmk-mrow .sp{flex:1}
.mmk-mrow .arr{color:var(--dim,#8aa0bd);font-size:11px;margin-left:6px}
.mmk-mrow.disabled{opacity:.38;cursor:default}
.mmk-mrow.disabled:hover{background:none}
.mmk-mrow kbd{background:#0e1622;border:1px solid var(--line,#2a3647);border-bottom-width:2px;
  border-radius:5px;padding:1px 6px;font:11px/1.5 Consolas,monospace;color:var(--dim,#8aa0bd);margin-left:6px}
.mmk-sep{height:1px;background:var(--line,#2a3647);margin:4px 6px}
`;

function fmtKey(k){
  return k.split('+').map(p => ({
    ctrl: 'Ctrl', alt: 'Alt', shift: 'Shift', space: 'Space',
    arrowup: '↑', arrowdown: '↓', arrowleft: '←', arrowright: '→',
    escape: 'Esc', delete: 'Del', backspace: '⌫',
  }[p] || (p.length === 1 ? p.toUpperCase() : p))).join('+');
}

export function initCommandCenter(opts = {}){
  const cmds = [];
  const byKey = new Map();
  const isTyping = opts.isTyping || (() => {
    const a = document.activeElement;
    return !!(a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.isContentEditable));
  });

  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);
  const overlay = document.createElement('div'); overlay.id = 'mmkOverlay';
  overlay.innerHTML = '<div class="mmk-box"><input placeholder="Type a command…" spellcheck="false">'
    + '<div class="mmk-list"></div></div>';
  const cheat = document.createElement('div'); cheat.id = 'mmkCheat';
  cheat.innerHTML = '<div class="card"></div>';
  document.body.appendChild(overlay); document.body.appendChild(cheat);
  const input = overlay.querySelector('input'), list = overlay.querySelector('.mmk-list');

  let selIx = 0, matches = [];

  function available(){ return cmds.filter(c => !c.hidden && (!c.when || c.when())); }

  function renderList(){
    const q = input.value.trim().toLowerCase();
    matches = available().filter(c =>
      !q || c.label.toLowerCase().includes(q) || (c.group || '').toLowerCase().includes(q));
    selIx = Math.max(0, Math.min(selIx, matches.length - 1));
    list.innerHTML = matches.length ? matches.map((c, i) =>
      `<div class="mmk-item${i === selIx ? ' sel' : ''}" data-i="${i}">`
      + `<span class="grp">${c.group || ''}</span><span>${c.label}</span><span class="sp"></span>`
      + (c.keys || []).slice(0, 1).map(k => `<kbd>${fmtKey(k)}</kbd>`).join('') + '</div>'
    ).join('') : '<div class="mmk-empty">No matching command</div>';
    list.querySelectorAll('.mmk-item').forEach(el => {
      el.onmousedown = e => { e.preventDefault(); runIndex(+el.dataset.i); };
    });
    const sel = list.querySelector('.mmk-item.sel');
    if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
  }
  function runIndex(i){
    const c = matches[i]; closePalette();
    if (c) setTimeout(() => c.run(), 0);   // run after the overlay is gone
  }
  function openPalette(){
    closeCheat(); overlay.classList.add('open');
    input.value = ''; selIx = 0; renderList(); input.focus();
  }
  function closePalette(){ overlay.classList.remove('open'); input.blur(); }
  input.addEventListener('input', () => { selIx = 0; renderList(); });
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape'){ e.preventDefault(); closePalette(); }
    else if (e.key === 'ArrowDown'){ e.preventDefault(); selIx++; renderList(); }
    else if (e.key === 'ArrowUp'){ e.preventDefault(); selIx--; renderList(); }
    else if (e.key === 'Enter'){ e.preventDefault(); runIndex(selIx); }
    e.stopPropagation();
  });
  overlay.addEventListener('mousedown', e => { if (e.target === overlay) closePalette(); });

  function openCheat(){
    closePalette();
    const groups = new Map();
    available().filter(c => (c.keys || []).length).forEach(c => {
      if (!groups.has(c.group)) groups.set(c.group, []);
      groups.get(c.group).push(c);
    });
    let html = '<h2>Keyboard shortcuts <small>Esc closes · Ctrl+K opens the command palette</small></h2>'
      + '<div class="cols">';
    groups.forEach((items, g) => {
      html += `<div><h3>${g || 'General'}</h3>` + items.map(c =>
        `<div class="row"><span>${c.label}</span><span>`
        + c.keys.map(k => `<kbd>${fmtKey(k)}</kbd>`).join('') + '</span></div>').join('') + '</div>';
    });
    cheat.querySelector('.card').innerHTML = html + '</div>';
    cheat.classList.add('open');
  }
  function closeCheat(){ cheat.classList.remove('open'); }
  cheat.addEventListener('mousedown', e => { if (e.target === cheat) closeCheat(); });

  /* ---- right-click context menu: every command, grouped, hover-expanding ---- */
  const menu = document.createElement('div'); menu.id = 'mmkMenu';
  const sub = document.createElement('div'); sub.className = 'mmk-sub';
  document.body.appendChild(menu); document.body.appendChild(sub);
  let menuOpen = false;
  const ICON = { Tools:'✎', Edit:'✂', Frames:'▭', View:'👁', Board:'▤', Help:'?' };

  // Commands grouped, preserving the order groups were first registered in.
  function groupsAvailable(){
    const order = [], map = new Map();
    cmds.forEach(c => { if (c.hidden) return; const g = c.group || 'General';
      if (!map.has(g)) { map.set(g, []); order.push(g); } map.get(g).push(c); });
    return order.map(g => ({ group: g, items: map.get(g) }));
  }
  const clampX = (x, w) => Math.max(6, Math.min(x, window.innerWidth - w - 6));
  const clampY = (y, h) => Math.max(6, Math.min(y, window.innerHeight - h - 6));

  function hideSub(){ sub.classList.remove('open');
    menu.querySelectorAll('.mmk-mrow.active').forEach(r => r.classList.remove('active')); }

  function showSub(rowEl, grp){
    sub.innerHTML = `<div class="mmk-title">${grp.group}</div>` + grp.items.map((c, i) => {
      const dis = c.when && !c.when();
      const kbd = (c.keys && c.keys[0]) ? `<kbd>${fmtKey(c.keys[0])}</kbd>` : '';
      return `<div class="mmk-mrow${dis ? ' disabled' : ''}" data-i="${i}"><span class="sp">${c.label}</span>${kbd}</div>`;
    }).join('');
    sub.classList.add('open');
    const rr = rowEl.getBoundingClientRect(), sw = sub.offsetWidth, sh = sub.offsetHeight;
    let sx = rr.right - 3; if (sx + sw > window.innerWidth - 6) sx = rr.left - sw + 3;
    sub.style.left = clampX(sx, sw) + 'px'; sub.style.top = clampY(rr.top - 5, sh) + 'px';
    sub.querySelectorAll('.mmk-mrow').forEach(el => {
      const c = grp.items[+el.dataset.i];
      if (c.when && !c.when()) return;                       // disabled: no handler
      el.onmousedown = e => { e.preventDefault(); closeMenu(); setTimeout(() => c.run(), 0); };
    });
    menu.querySelectorAll('.mmk-mrow.active').forEach(r => r.classList.remove('active'));
    rowEl.classList.add('active');
  }

  // extras: optional context-bound entries ({icon, label, run}) pinned above the
  // group rows — e.g. "Edit frame properties…" when a frame sits under the cursor.
  function openMenu(x, y, extras){
    closeCheat(); closePalette();
    const groups = groupsAvailable();
    const pins = (extras || []).filter(p => p && p.label && typeof p.run === 'function');
    menu.innerHTML = `<div class="mmk-title">Tools &amp; settings</div>`
      + `<div class="mmk-mrow" data-act="palette"><span class="mi">⌕</span><span class="sp">Command palette…</span><kbd>${fmtKey('ctrl+k')}</kbd></div>`
      + `<div class="mmk-mrow" data-act="cheat"><span class="mi">⌨</span><span class="sp">Keyboard shortcuts…</span><kbd>?</kbd></div>`
      + `<div class="mmk-sep"></div>`
      + pins.map((p, i) => `<div class="mmk-mrow" data-pin="${i}"><span class="mi">${p.icon || '•'}</span>`
        + `<span class="sp">${p.label}</span></div>`).join('')
      + (pins.length ? '<div class="mmk-sep"></div>' : '')
      + groups.map((g, i) => `<div class="mmk-mrow" data-g="${i}"><span class="mi">${ICON[g.group] || '•'}</span>`
        + `<span class="sp">${g.group}</span><span class="arr">▸</span></div>`).join('');
    menu.classList.add('open'); menuOpen = true; hideSub();
    menu.style.left = clampX(x, menu.offsetWidth) + 'px';
    menu.style.top = clampY(y, menu.offsetHeight) + 'px';
    menu.querySelectorAll('.mmk-mrow[data-act]').forEach(el => {
      el.onmouseenter = hideSub;
      el.onmousedown = e => { e.preventDefault(); const a = el.dataset.act; closeMenu();
        setTimeout(() => (a === 'palette' ? openPalette() : openCheat()), 0); };
    });
    menu.querySelectorAll('.mmk-mrow[data-pin]').forEach(el => {
      const p = pins[+el.dataset.pin];
      el.onmouseenter = hideSub;
      el.onmousedown = e => { e.preventDefault(); closeMenu(); setTimeout(() => p.run(), 0); };
    });
    menu.querySelectorAll('.mmk-mrow[data-g]').forEach(el => {
      const g = groups[+el.dataset.g];
      el.onmouseenter = () => showSub(el, g);
      el.onmousedown = e => { e.preventDefault(); showSub(el, g); };   // click also expands (touch)
    });
  }
  function closeMenu(){ menu.classList.remove('open'); hideSub(); menuOpen = false; }
  document.addEventListener('mousedown', e => {
    if (menuOpen && !menu.contains(e.target) && !sub.contains(e.target)) closeMenu();
  });
  window.addEventListener('keydown', e => {
    if (menuOpen && e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(); }
  }, true);
  window.addEventListener('scroll', () => { if (menuOpen) closeMenu(); }, true);
  window.addEventListener('blur', () => { if (menuOpen) closeMenu(); });

  function combo(e){
    let k = e.key === ' ' ? 'space' : e.key.toLowerCase();
    let s = '';
    if (e.ctrlKey || e.metaKey) s += 'ctrl+';
    if (e.altKey) s += 'alt+';
    // include shift only for keys where it isn't already baked into e.key
    if (e.shiftKey && (/^[a-z]$/.test(k) || k.length > 1)) s += 'shift+';
    return s + k;
  }

  window.addEventListener('keydown', e => {
    if (overlay.classList.contains('open')){
      // the input handler owns most keys, but Escape must work even when
      // focus has left the input (e.g. after clicking the list)
      if (e.key === 'Escape'){ e.preventDefault(); closePalette(); }
      return;
    }
    if (cheat.classList.contains('open')){
      if (e.key === 'Escape'){ e.preventDefault(); closeCheat(); }
      return;
    }
    const c = byKey.get(combo(e));
    if (!c) return;
    if (isTyping() && !c.global) return;
    if (c.when && !c.when()) return;
    e.preventDefault();
    c.run();
  });

  return {
    register(list){
      list.forEach(c => { cmds.push(c); (c.keys || []).forEach(k => byKey.set(k, c)); });
    },
    openPalette, openCheat, openMenu, closeMenu,
    run(id){ const c = cmds.find(x => x.id === id); if (c) c.run(); },
  };
}
