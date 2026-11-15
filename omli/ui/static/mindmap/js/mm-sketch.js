/* Hand-drawn render skin, backed by the bundled sketch library
   (MIT, /mindmap/js/vendor/rough.esm.js — no CDN, local-first).

   Sketch mode is a pure render-layer toggle persisted in localStorage:
   relation edges redraw as rough paths and a `sketch` class on <body> lets
   CSS give node cards irregular, hand-drawn borders. Board data never
   changes. Flow edges keep the crisp animated style (packets ride the exact
   path geometry). */

import rough from '/mindmap/js/vendor/rough.esm.js';

const KEY = 'omli-mm-sketch';

export function isSketch(){
  try { return localStorage.getItem(KEY) === '1'; } catch (_) { return false; }
}
export function setSketch(on){
  try { on ? localStorage.setItem(KEY, '1') : localStorage.removeItem(KEY); } catch (_) {}
  document.body.classList.toggle('sketch', !!on);
}
/* apply the persisted preference at boot; returns the active state */
export function initSketch(){
  const on = isSketch();
  document.body.classList.toggle('sketch', on);
  return on;
}

/* deterministic seed per path so edges don't re-wiggle on every render */
function seedOf(d){
  let h = 0;
  for (const ch of d) h = (h * 31 + ch.charCodeAt(0)) | 0;
  return (Math.abs(h) % 2147483646) + 1;
}

/* rough-drawn replacement for one edge path; returns an SVG node to append,
   or null so the caller falls back to the crisp path */
export function roughPath(svgEl, d, { stroke = '#8aa0bd', strokeWidth = 2, dash = null } = {}){
  try {
    const rc = rough.svg(svgEl);
    const g = rc.path(d, {
      stroke, strokeWidth,
      roughness: 1.4, bowing: 1.2,
      seed: seedOf(d),
      fill: 'none',
    });
    g.setAttribute('opacity', '0.9');
    if (dash) g.querySelectorAll('path').forEach(p => p.setAttribute('stroke-dasharray', dash));
    return g;
  } catch (_) {
    return null;
  }
}
