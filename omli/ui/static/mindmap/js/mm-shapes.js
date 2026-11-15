/* Architecture / flowchart shape library for the canvas.

   SHAPE_PALETTE feeds the shape-palette flyout; shapeSVG draws any shape key
   (including the basic rect/ellipse/diamond/rounded cycle) sized exactly to
   w×h. Pure rendering — no DOM lookups, no state.

   SHAPE_STYLES is the tint palette: color + line-pattern combos, each with a
   semi-opaque fill. shapeSVG's optional 4th arg selects one (by key, or an
   inline {stroke,fill,dash} object); omit it for the default blue accent. */

export const SHAPE_PALETTE=[
  {key:'process',    label:'Process',    group:'flow'},
  {key:'decision',   label:'Decision',   group:'flow'},
  {key:'terminator', label:'Start/End',  group:'flow'},
  {key:'data',       label:'Data',       group:'flow'},
  {key:'document',   label:'Document',   group:'flow'},
  {key:'database',   label:'Database',   group:'flow'},
  {key:'predefined', label:'Subprocess', group:'flow'},
  {key:'connector',  label:'Connector',  group:'flow'},
  {key:'operational',label:'Op Node',    group:'arch'},
  {key:'system',     label:'System',     group:'arch'},
  {key:'activity',   label:'Activity',   group:'arch'},
  {key:'exchange',   label:'Info Exch.', group:'arch'},
];

/* hex → rgba string at alpha a (semi-opaque tint from a shape's stroke hue). */
function hexRGBA(hex,a){
  const h=hex.replace('#','');
  const n=h.length===3
    ? h.split('').map(c=>parseInt(c+c,16))
    : [0,2,4].map(i=>parseInt(h.slice(i,i+2),16));
  return `rgba(${n[0]},${n[1]},${n[2]},${a})`;
}
/* rgba() from a color that may be a #hex or already a CSS color string. */
function rgbaOf(c,a){ return (typeof c==='string' && c[0]==='#') ? hexRGBA(c,a) : c; }

/* Per-shape fill patterns (dots/grid/…). Each is a
   self-contained <pattern> tiled in the shape's own local coordinate space, so it
   fills and scales with that one shape. (Revealing a board-wide graphic through a
   shape is the 'seethrough' fill mode.)
   mk(color, opacity) → the tile's SVG content. Local-first inline SVG. */
export const SHAPE_PATTERNS={
  dots:      {label:'Dots',      tile:22, mk:(c,o)=>`<circle cx="11" cy="11" r="2.1" fill="${c}" fill-opacity="${o}"/>`},
  grid:      {label:'Grid',      tile:30, mk:(c,o)=>`<path d="M30 0H0V30" fill="none" stroke="${c}" stroke-opacity="${o}" stroke-width="1"/>`},
  diagonal:  {label:'Diagonal',  tile:18, mk:(c,o)=>`<path d="M0 18 18 0M-4 4 4 -4M14 22 22 14" stroke="${c}" stroke-opacity="${o}" stroke-width="1.4"/>`},
  crosshatch:{label:'Crosshatch',tile:18, mk:(c,o)=>`<path d="M0 18 18 0M-4 4 4 -4M14 22 22 14M0 0 18 18M-4 14 4 22M14 -4 22 4" stroke="${c}" stroke-opacity="${o}" stroke-width="1"/>`},
  plus:      {label:'Plus',      tile:26, mk:(c,o)=>`<path d="M13 8V18M8 13H18" stroke="${c}" stroke-opacity="${o}" stroke-width="1.6" stroke-linecap="round"/>`},
  rings:     {label:'Rings',     tile:38, mk:(c,o)=>`<circle cx="19" cy="19" r="12" fill="none" stroke="${c}" stroke-opacity="${o}" stroke-width="1.4"/>`},
};

/* Tint palette: a color paired with a signature line pattern, so shapes read
   apart by BOTH hue and stroke (useful in print / for color-vision safety).
   fill is a semi-opaque wash of the same hue. dash '' = solid stroke. */
export const SHAPE_STYLES=[
  {key:'blue',   label:'Blue',   stroke:'#46b1ff', dash:''},
  {key:'green',  label:'Green',  stroke:'#46d39a', dash:''},
  {key:'amber',  label:'Amber',  stroke:'#ffb454', dash:'8 4'},
  {key:'red',    label:'Red',    stroke:'#ff6b6b', dash:'8 4'},
  {key:'purple', label:'Purple', stroke:'#c792ea', dash:'2 5'},
  {key:'teal',   label:'Teal',   stroke:'#33d6d6', dash:'2 5'},
  {key:'yellow', label:'Yellow', stroke:'#ffd866', dash:'13 6'},
  {key:'slate',  label:'Slate',  stroke:'#8aa0bd', dash:'13 6'},
];
const STYLE_BY_KEY=Object.fromEntries(SHAPE_STYLES.map(s=>[s.key,s]));
const DEFAULT_STYLE={stroke:'#46b1ff', dash:''};

/* Resolve the style arg (key string | {stroke,fill,dash} | falsy) to concrete
   stroke / fill / dash values for the SVG. */
function resolveStyle(style){
  const s=(typeof style==='string'? STYLE_BY_KEY[style] : style) || DEFAULT_STYLE;
  const stroke=s.stroke||DEFAULT_STYLE.stroke;
  const fill=s.fill || hexRGBA(stroke, 0.15);
  return {stroke, fill, dash:s.dash||''};
}

/* Geometry for one shape sized to w×h. `sty` is the style-attribute string for
   the primary silhouette; `A` is the stroke color for secondary detail strokes
   (database seam, subprocess bars, system inner border). Single-sources the
   shape math so shapeSVG (styled) and the graphic-fill layer share it. */
function shapeBody(shape,w,h,sty,A){
  const m=3, W=w-2*m, H=h-2*m;
  let b='';
  switch(shape){
    // basic shapes (from the tShape cycle tool: rect/ellipse/diamond/rounded)
    case 'ellipse': b=`<ellipse cx="${w/2}" cy="${h/2}" rx="${W/2}" ry="${H/2}" ${sty}/>`; break;
    case 'diamond': b=`<polygon points="${w/2},${m} ${w-m},${h/2} ${w/2},${h-m} ${m},${h/2}" ${sty}/>`; break;
    case 'rounded': b=`<rect x="${m}" y="${m}" width="${W}" height="${H}" rx="16" ${sty}/>`; break;
    case 'decision': b=`<polygon points="${w/2},${m} ${w-m},${h/2} ${w/2},${h-m} ${m},${h/2}" ${sty}/>`; break;
    case 'terminator': b=`<rect x="${m}" y="${m}" width="${W}" height="${H}" rx="${H/2}" ${sty}/>`; break;
    case 'data':{ const sk=Math.min(W*0.22,26);
      b=`<polygon points="${m+sk},${m} ${w-m},${m} ${w-m-sk},${h-m} ${m},${h-m}" ${sty}/>`; break; }
    case 'document':{ const wv=Math.min(H*0.18,13);
      b=`<path d="M${m} ${m} H${w-m} V${h-m-wv} q${-W/4} ${wv*1.7} ${-W/2} 0 q${-W/4} ${-wv*1.7} ${-W/2} 0 Z" ${sty}/>`; break; }
    case 'database':{ const ry=Math.min(H*0.18,13);
      b=`<path d="M${m} ${m+ry} a${W/2} ${ry} 0 0 1 ${W} 0 V${h-m-ry} a${W/2} ${ry} 0 0 1 ${-W} 0 Z" ${sty}/>`
       +`<path d="M${m} ${m+ry} a${W/2} ${ry} 0 0 0 ${W} 0" fill="none" stroke="${A}" stroke-width="2"/>`; break; }
    case 'predefined': b=`<rect x="${m}" y="${m}" width="${W}" height="${H}" ${sty}/>`
       +`<line x1="${m+8}" y1="${m}" x2="${m+8}" y2="${h-m}" stroke="${A}" stroke-width="2"/>`
       +`<line x1="${w-m-8}" y1="${m}" x2="${w-m-8}" y2="${h-m}" stroke="${A}" stroke-width="2"/>`; break;
    case 'connector': b=`<ellipse cx="${w/2}" cy="${h/2}" rx="${W/2}" ry="${H/2}" ${sty}/>`; break;
    case 'operational': b=`<rect x="${m}" y="${m}" width="${W}" height="${H}" rx="14" ${sty}/>`; break;
    case 'system': b=`<rect x="${m}" y="${m}" width="${W}" height="${H}" rx="3" ${sty}/>`
       +`<rect x="${m+4}" y="${m+4}" width="${W-8}" height="${H-8}" rx="2" fill="none" stroke="${A}" stroke-width="1"/>`; break;
    case 'activity': b=`<rect x="${m}" y="${m}" width="${W}" height="${H}" rx="${Math.min(H/2,22)}" ${sty}/>`; break;
    case 'exchange':{ const tip=Math.min(W*0.2,22);
      b=`<polygon points="${m},${m} ${w-m-tip},${m} ${w-m},${h/2} ${w-m-tip},${h-m} ${m},${h-m} ${m+tip},${h/2}" ${sty}/>`; break; }
    default: b=`<rect x="${m}" y="${m}" width="${W}" height="${H}" rx="4" ${sty}/>`;   // process
  }
  return b;
}

/* Draw a shape sized exactly to w×h. viewBox == box. `style` sets the stroke
   (color + dash — see SHAPE_STYLES); both `style` and the two fill args are
   optional.

   `bg` is the world-anchored board graphic a see-through shape reveals: `{id,def}`
   where def is a <pattern> already offset to world coords by the caller. Passed
   only for see-through shapes over an active board background.

   `fill` is the per-shape fill descriptor (all optional):
     {mode, color, opacity, pattern, tint, uid}
   mode ∈ 'tint' (custom semi-opaque wash) | 'pattern' (a SHAPE_PATTERNS tile) |
   'seethrough' (reveal `bg`, or the canvas `tint` when no board graphic) | 'neon'
   (an illuminated glow outline). `uid` makes this shape's <defs> ids unique.

   With no `fill`, mode defaults to 'seethrough' when a `bg` is given
   and 'tint' otherwise. */
export function shapeSVG(shape,w,h,style,bg,fill){
  const {stroke:A, fill:F, dash}=resolveStyle(style);
  // 'operational' is an intrinsically dashed node; fall back to its signature
  // dash when the chosen style is solid, otherwise honor the style's pattern.
  const d=dash || (shape==='operational'?'6 4':'');
  const da=d?` stroke-dasharray="${d}"`:'';
  const strokeSty=`fill="none" stroke="${A}" stroke-width="2" stroke-linejoin="round"${da}`;
  const mode=(fill&&fill.mode) || (bg&&bg.def ? 'seethrough' : 'tint');
  const uid=(fill&&fill.uid) || 's';
  let defs='', inner='';
  if(mode==='neon'){
    // Neon tube: a blurred colored glow under a hot white core line, faint interior.
    const c=(fill&&fill.color)||A, fid='neon-'+uid;
    defs=`<filter id="${fid}" x="-50%" y="-50%" width="200%" height="200%">`
      +`<feGaussianBlur stdDeviation="3.4" result="b"/>`
      +`<feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>`;
    inner=shapeBody(shape,w,h,`fill="${rgbaOf(c,0.07)}" stroke="none"`,'none')
      +`<g filter="url(#${fid})">`+shapeBody(shape,w,h,`fill="none" stroke="${c}" stroke-width="2.6" stroke-linejoin="round"`,c)+`</g>`
      +shapeBody(shape,w,h,`fill="none" stroke="#eaf6ff" stroke-width="1" stroke-linejoin="round"`,c);
  }else if(mode==='pattern'){
    const c=(fill&&fill.color)||A, o=(fill&&fill.opacity!=null)?fill.opacity:0.9;
    const pk=(fill&&fill.pattern)||'dots', p=SHAPE_PATTERNS[pk]||SHAPE_PATTERNS.dots, pid='pat-'+uid;
    defs=`<pattern id="${pid}" patternUnits="userSpaceOnUse" width="${p.tile}" height="${p.tile}">${p.mk(c,o)}</pattern>`;
    inner=shapeBody(shape,w,h,`fill="${rgbaOf(c,0.10)}" stroke="none"`,'none')     // faint base so the tile reads on dark
      +shapeBody(shape,w,h,`fill="url(#${pid})" stroke="none"`,'none')
      +shapeBody(shape,w,h,strokeSty,A);
  }else if(mode==='seethrough'){
    // Layer 1: reveal the board graphic (or the canvas tint when there is none).
    // Layer 2: the tinted glass + stroke on top, so the shape reads as a window.
    let base;
    if(bg&&bg.def){ defs=bg.def; base=shapeBody(shape,w,h,`fill="url(#${bg.id})" stroke="none"`,'none'); }
    else { const t=(fill&&fill.tint)||'#0e1622'; base=shapeBody(shape,w,h,`fill="${t}" stroke="none"`,'none'); }
    inner=base+shapeBody(shape,w,h,`fill="${F}" stroke="${A}" stroke-width="2" stroke-linejoin="round"${da}`,A);
  }else{ // tint — a customizable semi-opaque wash of the fill color
    const c=(fill&&fill.color)||A, o=(fill&&fill.opacity!=null)?fill.opacity:0.15;
    inner=shapeBody(shape,w,h,`fill="${rgbaOf(c,o)}" stroke="${A}" stroke-width="2" stroke-linejoin="round"${da}`,A);
  }
  const dd=defs?`<defs>${defs}</defs>`:'';
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">${dd}${inner}</svg>`;
}
