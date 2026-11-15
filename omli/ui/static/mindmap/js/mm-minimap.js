/* Minimap for the canvas: a small fixed overview (bottom-left) drawing every
   node as a scaled rect plus the current viewport; click or drag to pan the
   camera there. Visibility persists in localStorage; M toggles.

   The host calls update() from render() and applyCam() — drawing ~hundreds
   of rects on a 180x120 canvas is far cheaper than either of those. */

const MINI_KEY = 'omli-mm-minimap';
const MINI_CSS = `
#mmMini{position:fixed;left:12px;bottom:12px;z-index:40;width:180px;height:122px;
  background:#0e1622d9;border:1px solid var(--line,#2a3647);border-radius:10px;
  overflow:hidden;box-shadow:0 8px 24px #0008;cursor:crosshair}
#mmMini.hidden{display:none}
#mmMini canvas{width:100%;height:100%;display:block}`;

export function createMinimap(ctx){
  // ctx: {nodes:()=>[], cam:()=>{x,y,scale}, stage:HTMLElement,
  //       setCam(x,y), colorOf?:(node)=>cssColor}
  const style = document.createElement('style');
  style.textContent = MINI_CSS;
  document.head.appendChild(style);
  const box = document.createElement('div'); box.id = 'mmMini';
  box.title = 'Minimap — click to pan (M toggles)';
  const canvas = document.createElement('canvas');
  box.appendChild(canvas); document.body.appendChild(box);

  let visible = true;
  try { visible = localStorage.getItem(MINI_KEY) !== '0'; } catch (_) {}
  box.classList.toggle('hidden', !visible);

  const NODE_W = 150, NODE_H = 100;   // approximate DOM node footprint

  function bounds(){
    const ns = ctx.nodes().filter(n => !n._hidden);
    if (!ns.length) return null;
    const xs = ns.map(n => n.x), ys = ns.map(n => n.y);
    return {
      minX: Math.min(...xs) - 120, maxX: Math.max(...xs) + NODE_W + 120,
      minY: Math.min(...ys) - 120, maxY: Math.max(...ys) + NODE_H + 120,
    };
  }

  let geo = null;    // {b, scale, ox, oy} of the last draw, for click mapping

  function update(){
    if (!visible) return;
    const g2d = canvas.getContext && canvas.getContext('2d');
    if (!g2d) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 180, h = canvas.clientHeight || 122;
    canvas.width = w * dpr; canvas.height = h * dpr;
    g2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    g2d.clearRect(0, 0, w, h);
    const b = bounds();
    if (!b){ geo = null; return; }
    const scale = Math.min(w / (b.maxX - b.minX), h / (b.maxY - b.minY));
    const ox = (w - (b.maxX - b.minX) * scale) / 2;
    const oy = (h - (b.maxY - b.minY) * scale) / 2;
    geo = { b, scale, ox, oy };
    ctx.nodes().forEach(n => {
      if (n._hidden) return;
      g2d.fillStyle = (ctx.colorOf && ctx.colorOf(n)) || '#46b1ff';
      g2d.globalAlpha = 0.75;
      g2d.fillRect(ox + (n.x - b.minX) * scale, oy + (n.y - b.minY) * scale,
        Math.max(2, (n.w || NODE_W) * scale), Math.max(2, (n.h || NODE_H) * scale));
    });
    g2d.globalAlpha = 1;
    // viewport rectangle
    const cam = ctx.cam();
    const st = ctx.stage.getBoundingClientRect();
    const vx = (-cam.x / cam.scale - b.minX) * scale + ox;
    const vy = (-cam.y / cam.scale - b.minY) * scale + oy;
    const vw = (st.width / cam.scale) * scale;
    const vh = (st.height / cam.scale) * scale;
    g2d.strokeStyle = '#e7edf5'; g2d.lineWidth = 1.2;
    g2d.strokeRect(vx, vy, vw, vh);
  }

  function navigate(e){
    if (!geo) return;
    const r = canvas.getBoundingClientRect();
    const wx = geo.b.minX + (e.clientX - r.left - geo.ox) / geo.scale;
    const wy = geo.b.minY + (e.clientY - r.top - geo.oy) / geo.scale;
    const cam = ctx.cam();
    const st = ctx.stage.getBoundingClientRect();
    ctx.setCam(st.width / 2 - wx * cam.scale, st.height / 2 - wy * cam.scale);
  }
  let dragging = false;
  box.addEventListener('pointerdown', e => {
    dragging = true; box.setPointerCapture(e.pointerId); navigate(e); e.preventDefault();
  });
  box.addEventListener('pointermove', e => { if (dragging) navigate(e); });
  const end = () => { dragging = false; };
  box.addEventListener('pointerup', end); box.addEventListener('pointercancel', end);

  function toggle(){
    visible = !visible;
    try { visible ? localStorage.removeItem(MINI_KEY) : localStorage.setItem(MINI_KEY, '0'); } catch (_) {}
    box.classList.toggle('hidden', !visible);
    update();
    return visible;
  }

  return { update, toggle, visible: () => visible };
}
