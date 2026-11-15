/* World-space paint + scratch overlay.

   Stroke / cover records are the source of truth.
   This module only renders them and turns pointer samples into a record.
   Covers are authored stacked coatings (rainbow scratch-off). A new layer
   always goes on top. Scratch punches only the layer named on the stroke
   (a stroke with no layer punches the oldest cover on its clip). */

export const CLIP_BOARD = 'board';
export const MAX_LAYERS = 12;
export const LAYER_FILLS = [
  '#e74c3c', '#e67e22', '#f1c40f', '#2ecc71',
  '#3498db', '#9b59b6', '#1abc9c', '#ecf0f1',
  '#34495e', '#e91e63', '#00bcd4', '#ff5722',
];
export const DEFAULT_BRUSH = { color: '#46b1ff', width: 14, opacity: 1, hardness: 0.85 };
/* Same origin shift the #edges SVG uses: a 40000² box parked at -20000 so
   world (0,0) is in the middle. Paint paths MUST be shifted by this or they
   render 20k px off-screen. */
export const INK_ORIGIN = 20000;
const BRUSH_KEY = 'omli-mm-ink-brush';
const MAX_POINTS = 4000;

export function clampBrush(b){
  const src = b && typeof b === 'object' ? b : {};
  const color = String(src.color || DEFAULT_BRUSH.color).slice(0, 32) || DEFAULT_BRUSH.color;
  const num = (v, lo, hi, d) => {
    const n = +v; return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : d;
  };
  return {
    color,
    width: num(src.width, 0.5, 200, DEFAULT_BRUSH.width),
    opacity: num(src.opacity, 0, 1, DEFAULT_BRUSH.opacity),
    hardness: num(src.hardness, 0, 1, DEFAULT_BRUSH.hardness),
  };
}

export function loadBrush(){
  try { return clampBrush(JSON.parse(localStorage.getItem(BRUSH_KEY) || 'null')); }
  catch (_) { return { ...DEFAULT_BRUSH }; }
}

export function saveBrush(b){
  try { localStorage.setItem(BRUSH_KEY, JSON.stringify(clampBrush(b))); } catch (_) {}
}

export function photoSrc(raw){
  const s = String(raw || '');
  if (s.startsWith('data:image/') || s.startsWith('/') ||
      s.startsWith('http://') || s.startsWith('https://')) return s;
  return '';
}

export function pointsToPath(points, origin = 0){
  if (!points || !points.length) return '';
  const o = +origin || 0;
  let d = `M${(+points[0].x + o).toFixed(2)} ${(+points[0].y + o).toFixed(2)}`;
  for (let i = 1; i < points.length; i++) {
    d += `L${(+points[i].x + o).toFixed(2)} ${(+points[i].y + o).toFixed(2)}`;
  }
  return d;
}

export function boardCoverRect(nodes){
  /* A full-board cover cannot be a 20k canvas (browser pixel caps). Bound it. */
  let minX = -800, minY = -800, maxX = 800, maxY = 800;
  for (const n of nodes || []) {
    const w = n.w || 140, h = n.h || 80;
    minX = Math.min(minX, n.x - w / 2);
    minY = Math.min(minY, n.y - h / 2);
    maxX = Math.max(maxX, n.x + w / 2);
    maxY = Math.max(maxY, n.y + h / 2);
  }
  const pad = 120;
  return { x: minX - pad, y: minY - pad, w: (maxX - minX) + pad * 2, h: (maxY - minY) + pad * 2 };
}

export function clipRect(clip, nodes){
  if (!clip || clip === CLIP_BOARD) return null;
  const n = (nodes || []).find(x => x && x.id === clip);
  if (!n) return null;
  const w = n.w || 600, h = n.h || 400;
  return { x: n.x - w / 2, y: n.y - h / 2, w, h };
}

export function coversForTarget(covers, target){
  return (covers || []).filter(c => c && c.target === target)
    .slice()
    .sort((a, b) => (+a.order || 0) - (+b.order || 0));
}

export function topCover(covers, target){
  const stack = coversForTarget(covers, target);
  return stack.length ? stack[stack.length - 1] : null;
}

export function nextLayerOrder(covers, target){
  return coversForTarget(covers, target).reduce((m, c) => Math.max(m, +c.order || 0), 0) + 1;
}

export function nextLayerFill(covers, target, preferred){
  const hex = String(preferred || '');
  if (/^#[0-9a-fA-F]{6}$/.test(hex)) return hex;
  const n = coversForTarget(covers, target).length;
  return LAYER_FILLS[n % LAYER_FILLS.length];
}

export function coverUnderPoint(x, y, covers, nodes){
  /* Topmost frame coating under the point wins; else the top board coating. */
  const framed = (covers || []).filter(c => c && c.target !== CLIP_BOARD)
    .slice()
    .sort((a, b) => (+b.order || 0) - (+a.order || 0));
  for (const c of framed) {
    const r = clipRect(c.target, nodes);
    if (r && x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) return c;
  }
  return topCover(covers, CLIP_BOARD);
}

export function resolveClip(x, y, mode, covers, nodes){
  const c = coverUnderPoint(x, y, covers, nodes);
  if (mode === 'scratch') return c ? c.target : CLIP_BOARD;
  return c ? c.target : CLIP_BOARD;
}

export function resolveLayer(x, y, covers, nodes){
  const c = coverUnderPoint(x, y, covers, nodes);
  return c ? c.id : '';
}

export function strokeHitsCover(stroke, cover, covers){
  if (!stroke || !cover) return false;
  if (stroke.layer) return stroke.layer === cover.id;
  if (stroke.clip !== cover.target) return false;
  const stack = coversForTarget(covers, cover.target);
  return !!(stack[0] && stack[0].id === cover.id);
}

export function makeStroke(id, mode, points, brush, clip, layer){
  const b = clampBrush(brush);
  return {
    id, mode: mode === 'scratch' ? 'scratch' : 'paint',
    points: (points || []).slice(0, MAX_POINTS),
    color: b.color, width: b.width, opacity: b.opacity, hardness: b.hardness,
    clip: clip || CLIP_BOARD, layer: layer || '', t0: Date.now(),
  };
}

function _strokePath(ctx, stroke, ox, oy){
  const pts = stroke.points || [];
  if (pts.length < 2) return;
  ctx.beginPath();
  ctx.moveTo(pts[0].x - ox, pts[0].y - oy);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x - ox, pts[i].y - oy);
  ctx.stroke();
}

function _paintCover(canvas, cover, strokes, covers, nodes, live){
  const board = cover.target === CLIP_BOARD;
  const r = board ? boardCoverRect(nodes) : clipRect(cover.target, nodes);
  if (!r) { canvas.style.display = 'none'; return; }
  canvas.style.display = 'block';
  canvas.style.left = r.x + 'px';
  canvas.style.top = r.y + 'px';
  canvas.style.width = r.w + 'px';
  canvas.style.height = r.h + 'px';
  const w = Math.max(1, Math.round(r.w));
  const h = Math.max(1, Math.round(r.h));
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.fillStyle = cover.fill || '#1b2738';
  ctx.fillRect(0, 0, w, h);
  const ink = (strokes || []).filter(s => s && strokeHitsCover(s, cover, covers));
  if (live && strokeHitsCover(live, cover, covers)) ink.push(live);
  for (const s of ink.filter(s => s.mode === 'paint')) {
    ctx.globalCompositeOperation = 'source-over';
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.lineWidth = Math.max(1, +s.width || 14);
    ctx.globalAlpha = +s.opacity || 1;
    const soft = (1 - (+s.hardness || 0.85)) * ctx.lineWidth;
    ctx.shadowBlur = soft;
    ctx.shadowColor = s.color || '#46b1ff';
    ctx.strokeStyle = s.color || '#46b1ff';
    _strokePath(ctx, s, r.x, r.y);
  }
  ctx.shadowBlur = 0;
  ctx.globalCompositeOperation = 'destination-out';
  for (const s of ink.filter(s => s.mode === 'scratch')) {
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.lineWidth = Math.max(2, +s.width || 14);
    ctx.globalAlpha = Math.max(0.35, +s.opacity || 1);
    const soft = (1 - (+s.hardness || 0.85)) * ctx.lineWidth;
    ctx.shadowBlur = soft;
    ctx.shadowColor = '#000';
    ctx.strokeStyle = '#000';
    _strokePath(ctx, s, r.x, r.y);
  }
}

export function createInk(world){
  if (!world) return { render() {}, noteLatency() {}, stats() { return { n: 0, p50: 0, p95: 0 }; }, layer: null };
  let layer = world.querySelector('#ink');
  if (!layer) {
    layer = document.createElement('div');
    layer.id = 'ink';
    const edges = world.querySelector('#edges');
    if (edges && edges.nextSibling) world.insertBefore(layer, edges.nextSibling);
    else world.appendChild(layer);
  }
  let svg = world.querySelector('#inkPaint');
  if (!svg) {
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('id', 'inkPaint');
    svg.setAttribute('overflow', 'visible');
    /* Unlayered paint sits UNDER the cover stack so a new coating hides it. */
    world.insertBefore(svg, layer);
  }
  const samples = [];
  function noteLatency(ms){
    const n = +ms;
    if (!Number.isFinite(n) || n < 0) return;
    samples.push(n);
    if (samples.length > 60) samples.shift();
  }
  function stats(){
    if (!samples.length) return { n: 0, p50: 0, p95: 0 };
    const s = [...samples].sort((a, b) => a - b);
    const at = p => s[Math.min(s.length - 1, Math.max(0, Math.ceil(p * s.length) - 1))];
    return { n: s.length, p50: at(0.5), p95: at(0.95) };
  }
  function render(strokes, covers, nodes, live){
    const list = covers || [];
    const keep = new Set(list.map(c => c && c.id).filter(Boolean));
    [...layer.querySelectorAll('canvas.ink-cover')].forEach(el => {
      if (!keep.has(el.dataset.cid)) el.remove();
    });
    const stacked = list.slice().sort((a, b) => (+a.order || 0) - (+b.order || 0));
    stacked.forEach((cover, i) => {
      if (!cover || !cover.id) return;
      let cv = layer.querySelector(`canvas.ink-cover[data-cid="${cover.id}"]`);
      if (!cv) {
        cv = document.createElement('canvas');
        cv.className = 'ink-cover';
        cv.dataset.cid = cover.id;
        layer.appendChild(cv);
      }
      cv.style.zIndex = String(i + 1);
      _paintCover(cv, cover, strokes, list, nodes, live);
    });
    /* Ghost leftover: only unlayered strokes (no coating to live on). */
    const paints = (strokes || []).filter(s => s && !s.layer);
    if (live && !live.layer) paints.push(live);
    let html = '';
    for (const s of paints) {
      const d = pointsToPath(s.points, INK_ORIGIN);
      if (!d) continue;
      const scratch = s.mode === 'scratch';
      const blur = (1 - (+s.hardness || 0.85)) * ((+s.width || 14) / 2);
      html += `<path d="${d}" fill="none" stroke="${scratch ? '#c8d6e5' : (s.color || '#46b1ff')}" `
        + `stroke-width="${+s.width || 14}" stroke-linecap="round" stroke-linejoin="round" `
        + `stroke-opacity="${scratch ? 0.35 : (+s.opacity || 1)}"`
        + (blur > 0.4 ? ` style="filter:blur(${blur.toFixed(1)}px)"` : '')
        + `/>`;
    }
    svg.innerHTML = html;
  }
  return { render, noteLatency, stats, layer };
}
