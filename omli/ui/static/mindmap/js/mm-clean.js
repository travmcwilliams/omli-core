/* Board data from the server is untrusted: a board log can hold any field a save or a copied
   file put there. Every board the page loads passes through cleanBoard() first, so the
   renderers only ever see ids, numbers, colours, image data and icon paths of a known shape.
   Text fields are left as they are; the renderers escape them.

   Pure: no DOM, no globals, so it runs under Node in the test suite. */

export const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;            // as omli/boards/diff.py
const DATA_IMAGE = /^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$/;
const COLOR = /^(?:#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|(?:rgb|hsl)a?\([0-9.,%\s/deg]{1,60}\))$/;
const ICON_SRC = /^\/mindmap\/icons\/[a-z0-9-]{1,64}\.svg$/;
const WORD = /^[a-z0-9-]{1,32}$/;
const DASH = /^[0-9. ]{0,24}$/;
const GLYPH_BAD = /[<>&"'`\\]/;

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
// A short lowercase name that is not also a property of every object (such as
// 'constructor'), so a lookup table indexed by it can only return its own entries.
const isWord = (v) => typeof v === 'string' && WORD.test(v) && !(v in Object.prototype);
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

/* A data: image of a known type, an http(s) address (shown only as an escaped placeholder),
   or nothing. */
export function cleanImage(v) {
  if (typeof v !== 'string') return '';
  if (v.startsWith('data:')) return DATA_IMAGE.test(v) ? v : '';
  return /^https?:\/\//i.test(v) ? v : '';
}

export function cleanColor(v) {
  return typeof v === 'string' && COLOR.test(v) ? v : undefined;
}

function cleanGlyph(v) {
  return typeof v === 'string' && v.length <= 8 && !GLYPH_BAD.test(v) ? v : undefined;
}

function cleanIcon(icon) {
  if (!isObj(icon)) return undefined;
  const out = {};
  if (isWord(icon.kind)) out.kind = icon.kind;
  if (typeof icon.src === 'string' && ICON_SRC.test(icon.src)) out.src = icon.src;
  for (const k of ['hue', 'hue2', 'sat', 'light', 'seed']) if (isNum(icon[k])) out[k] = icon[k];
  const g = cleanGlyph(icon.glyph);
  if (g !== undefined) out.glyph = g;
  if (out.kind === 'library' && !out.src) delete out.kind;
  return out;
}

function cleanStyle(style) {
  if (typeof style === 'string') return isWord(style) ? style : undefined;
  if (!isObj(style)) return undefined;
  const out = {};
  const stroke = cleanColor(style.stroke), fill = cleanColor(style.fill);
  if (stroke) out.stroke = stroke;
  if (fill) out.fill = fill;
  if (typeof style.dash === 'string' && DASH.test(style.dash)) out.dash = style.dash;
  return out;
}

function cleanMeta(meta) {
  if (!isObj(meta)) return undefined;
  const m = { ...meta };
  for (const k of ['photo', 'src']) if (k in m) m[k] = cleanImage(m[k]);
  for (const k of ['fill', 'stroke', 'textColor', 'fillColor']) {
    if (k in m && cleanColor(m[k]) === undefined) delete m[k];
  }
  for (const k of ['fillOpacity', 'scale', 'labelSize']) if (k in m && !isNum(m[k])) delete m[k];
  for (const k of ['fillMode', 'pattern']) {
    if (k in m && !isWord(m[k])) delete m[k];
  }
  if ('shapeStyle' in m) {
    const s = cleanStyle(m.shapeStyle);
    if (s === undefined) delete m.shapeStyle; else m.shapeStyle = s;
  }
  if ('params' in m) {
    if (!isObj(m.params)) delete m.params;
    else {
      m.params = { ...m.params };
      if ('k' in m.params && !isNum(m.params.k)) delete m.params.k;
    }
  }
  return m;
}

export function cleanNode(id, data) {
  if (typeof id !== 'string' || !ID_RE.test(id) || !isObj(data)) return null;
  const n = { ...data, id };
  for (const k of ['x', 'y']) if (!isNum(n[k])) n[k] = 0;
  for (const k of ['w', 'h', 'order']) if (k in n && !isNum(n[k])) delete n[k];
  if ('glyph' in n && cleanGlyph(n.glyph) === undefined) delete n.glyph;
  if ('shape' in n && !isWord(n.shape)) delete n.shape;
  if ('parent' in n && n.parent != null && !(typeof n.parent === 'string' && ID_RE.test(n.parent))) {
    delete n.parent;
  }
  if ('icon' in n) {
    const ic = cleanIcon(n.icon);
    if (ic === undefined) delete n.icon; else n.icon = ic;
  }
  if ('meta' in n) {
    const m = cleanMeta(n.meta);
    if (m === undefined) delete n.meta; else n.meta = m;
  }
  if ('catalog_ref' in n && !isObj(n.catalog_ref)) delete n.catalog_ref;
  if ('params' in n) {
    if (!isObj(n.params)) delete n.params;
    else {
      n.params = { ...n.params };
      if ('k' in n.params && !isNum(n.params.k)) delete n.params.k;
    }
  }
  return n;
}

/* {nodes: {id: data}, links: {id: {from, to, ...}}} -> the same shape, with every node and
   link that cannot be shown safely left out, and every field reduced to a safe form. */
export function cleanBoard(b) {
  const nodes = {}, links = {};
  const rawNodes = isObj(b) && isObj(b.nodes) ? b.nodes : {};
  const rawLinks = isObj(b) && isObj(b.links) ? b.links : {};
  for (const [id, d] of Object.entries(rawNodes)) {
    const n = cleanNode(id, d);
    if (n) { delete n.id; nodes[id] = n; }
  }
  for (const [id, d] of Object.entries(rawLinks)) {
    if (!ID_RE.test(id) || !isObj(d)) continue;
    if (!(typeof d.from === 'string' && Object.hasOwn(nodes, d.from))) continue;
    if (!(typeof d.to === 'string' && Object.hasOwn(nodes, d.to))) continue;
    const l = { ...d };
    for (const k of ['color', 'stroke']) if (k in l && cleanColor(l[k]) === undefined) delete l[k];
    for (const k of ['width']) if (k in l && !isNum(l[k])) delete l[k];
    if ('style' in l && !isWord(l.style)) delete l.style;
    links[id] = l;
  }
  return { nodes, links };  // nothing else from the server reaches the renderers
}
