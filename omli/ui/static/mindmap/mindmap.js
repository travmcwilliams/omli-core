import {initCommandCenter} from '/mindmap/js/mm-keymap.js';
import {cleanBoard,cleanImage} from '/mindmap/js/mm-clean.js';
import {svgIcon,startLoading,stopLoading} from '/mindmap/js/mm-icons.js';
import {SHAPE_PALETTE,SHAPE_STYLES,SHAPE_PATTERNS,shapeSVG} from '/mindmap/js/mm-shapes.js';
import {initSketch,isSketch,setSketch,roughPath} from '/mindmap/js/mm-sketch.js';
import {createMinimap} from '/mindmap/js/mm-minimap.js';
import {createInk,loadBrush,saveBrush,clampBrush,resolveClip,resolveLayer,makeStroke,topCover,nextLayerOrder,nextLayerFill,coversForTarget,MAX_LAYERS,CLIP_BOARD as INK_BOARD} from '/mindmap/js/mm-ink.js';
import {createMiniCard,allowPreview} from '/mindmap/js/minicard.js';
import {makeDraggable} from '/mindmap/js/drag.js';
import {makeResizable} from '/mindmap/js/resize.js';
// degrade gracefully when a module binding is absent: no-op manager, fail-closed preview gate
const cards=(typeof createMiniCard==='function')?createMiniCard()
  :{schedule(){},suppress(){},hide(){},destroy(){},attachFocus(){}};
const canPreview=(typeof allowPreview==='function')?allowPreview:()=>false;
const _inkOk=typeof createInk==='function';
const inkFns={
  createInk:_inkOk?createInk:()=>({render(){},noteLatency(){},stats(){return {n:0,p50:0,p95:0};},layer:null}),
  loadBrush:typeof loadBrush==='function'?loadBrush:()=>({color:'#46b1ff',width:14,opacity:1,hardness:0.85}),
  saveBrush:typeof saveBrush==='function'?saveBrush:()=>{},
  clampBrush:typeof clampBrush==='function'?clampBrush:b=>b||{},
  resolveClip:typeof resolveClip==='function'?resolveClip:()=>'board',
  resolveLayer:typeof resolveLayer==='function'?resolveLayer:()=>'',
  topCover:typeof topCover==='function'?topCover:(cs,t)=>(cs||[]).filter(c=>c&&c.target===t).at(-1)||null,
  nextLayerOrder:typeof nextLayerOrder==='function'?nextLayerOrder:(cs,t)=>((cs||[]).filter(c=>c&&c.target===t).length)+1,
  nextLayerFill:typeof nextLayerFill==='function'?nextLayerFill:()=>'#e74c3c',
  coversForTarget:typeof coversForTarget==='function'?coversForTarget:(cs,t)=>(cs||[]).filter(c=>c&&c.target===t),
  MAX_LAYERS:typeof MAX_LAYERS==='number'?MAX_LAYERS:12,
  makeStroke:typeof makeStroke==='function'?makeStroke:(id,mode,pts,b,clip,layer)=>({id,mode,points:pts||[],color:(b&&b.color)||'#46b1ff',width:(b&&b.width)||14,opacity:(b&&b.opacity)==null?1:b.opacity,hardness:(b&&b.hardness)==null?0.85:b.hardness,clip:clip||'board',layer:layer||'',t0:0}),
  CLIP_BOARD:typeof INK_BOARD==='string'?INK_BOARD:'board',
};
const API='';
const STARTER_BOARD='how-omli-works';
const MODEL_OFF_LINE='No model is configured, so Ask and Generate are off. Add a model for the '
  +'role named in the line below to models/registry.yaml in your OMLi home folder, then reload.';
// Model status from the server: {configured, reason}. Until it answers, the model
// actions stay off (fail closed).
let modelStatus={configured:false,reason:''};
let boardHead=null;                          // the board's last operation id, sent as `base`

/* ---------- images: local by default ----------
   A board image shows only from a local source: a file the user dropped or pasted
   (kept as a data: URL inside the board). An image address on another host is never
   fetched by the page: it shows a placeholder, and a click opens the address in a new
   tab, which is the user's own choice to contact that host. */
function localImage(raw){
  const s=cleanImage(raw);   // a data: image of a known type, base64 only
  return s.startsWith('data:image/')?s:'';
}
function outsideImage(raw){
  const s=String(raw||'');
  if(!/^https?:\/\//i.test(s)) return null;
  try{ return new URL(s); }catch(_){ return null; }
}
function imagePlaceholder(raw,cls){
  const u=outsideImage(raw); if(!u) return '';
  return `<a class="img-ph ${cls||''}" href="${escapeHtml(u.href)}" target="_blank" rel="noopener noreferrer"`
    +` title="Opens ${escapeHtml(u.host)} in a new tab">Image on ${escapeHtml(u.host)}<br><i>click to load it in a new tab</i></a>`;
}

/* ---------- board <-> server shape ----------
   The page keeps nodes and edges as arrays; the server keeps a board as nodes and links
   keyed by id (omli.boards). A link is {from,to,...}. Ladder rungs keep their folder or
   model binding and their parameters at the top level, as the core's ladder check reads
   them; the page keeps them under meta. Fields starting with _ are transient and never
   leave the page. */
const RUNG_TO_SERVER={'context-group':'context'}, RUNG_FROM_SERVER={context:'context-group'};
function toServer(){
  const outN={}, outL={};
  nodes.forEach(n=>{
    const c={};
    // the icon is derived from the text by the server's pack rules on every load, so it is
    // never stored: storing it would record an edit each time the rules change
    Object.keys(n).forEach(k=>{ if(k!=='id'&&k!=='icon'&&k[0]!=='_'&&n[k]!==undefined) c[k]=n[k]; });
    if(RUNG_TO_SERVER[c.kind]) c.kind=RUNG_TO_SERVER[c.kind];
    if(isRung(n.kind)&&n.meta){
      if(n.meta.catalog_ref) c.catalog_ref=n.meta.catalog_ref;
      if(n.meta.params) c.params=n.meta.params;
    }
    outN[n.id]=c;
  });
  edges.forEach(ed=>{
    const c={from:ed.source,to:ed.target};
    Object.keys(ed).forEach(k=>{ if(!['id','source','target'].includes(k)&&k[0]!=='_'&&ed[k]!==undefined) c[k]=ed[k]; });
    outL[ed.id]=c;
  });
  return {nodes:outN,links:outL};
}
function fromServer(b){
  b=cleanBoard(b);   // untrusted board data: ids, numbers, colours and images made safe
  const ns=Object.entries((b&&b.nodes)||{}).map(([id,d])=>{
    const n={id,...d};
    if(RUNG_FROM_SERVER[n.kind]) n.kind=RUNG_FROM_SERVER[n.kind];
    if(isRung(n.kind)){
      n.meta=n.meta||{};
      if(n.catalog_ref){ n.meta.catalog_ref=n.catalog_ref; delete n.catalog_ref; }
      if(n.params){ n.meta.params=n.params; delete n.params; }
    }
    if(n.text==null&&n.title) n.text=n.title;
    if(typeof n.x!=='number') n.x=0;
    if(typeof n.y!=='number') n.y=0;
    return n;
  });
  const es=Object.entries((b&&b.links)||{}).map(([id,d])=>{
    const {from,to,...rest}=d; return {id,source:from,target:to,...rest};
  });
  return {nodes:ns,edges:es};
}
function boardIdFrom(name){
  const s=String(name||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60);
  return s||('board-'+Date.now().toString(36));
}
async function jsonOf(r){ try{ return await r.json(); }catch(_){ return {}; } }
/** Save the board as shown. Resolves to true when the server holds it. */
async function saveBoard(quiet){
  // The name field picks the board. A new name saves a new board, based on nothing, so the
  // server refuses it (409) if a board of that name already exists instead of overwriting it.
  const wanted=boardIdFrom($('mapName').value);
  const before={mapId,boardHead};
  if(wanted && wanted!==mapId){ mapId=wanted; boardHead=null; }
  const body={board:mapId,...toServer(),base:boardHead||null};
  let r, j;
  try{
    r=await fetch(API+'/api/mindmap/save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    j=await jsonOf(r);
  }catch(_){ ({mapId,boardHead}=before); toast('Save failed: the server did not answer'); return false; }
  if(!r.ok){ ({mapId,boardHead}=before); toast('Save failed: '+(j.error||r.status)); return false; }
  boardHead=j.head||boardHead;
  $('mapName').value=mapId;
  if(!quiet) toast('Saved '+mapId+' · '+(j.written||0)+' change'+(j.written===1?'':'s'));
  loadAttribution();
  return true;
}
async function loadModelStatus(){
  try{
    const r=await fetch(API+'/api/mindmap/assist/status');
    const j=await jsonOf(r);
    modelStatus={configured:!!j.configured,reason:String(j.reason||'')};
  }catch(_){ modelStatus={configured:false,reason:''}; }
  applyModelStatus();
}
function applyModelStatus(){
  const off=!modelStatus.configured;
  const line=$('modelLine');
  if(line){
    line.style.display=off?'block':'none';
    line.innerHTML=off?(escapeHtml(MODEL_OFF_LINE)
      +(modelStatus.reason?'<br><code>'+escapeHtml(modelStatus.reason)+'</code>':'')):'';
  }
  ['btnRead','btnRun'].forEach(id=>{ const b=$(id); if(b){ b.disabled=off;
    b.title=off?'No model configured':''; } });
}
const REL_LABELS={association:'N:1 association',context:'context',dependency:'dependency',
  elaboration:'elaboration',contradiction:'contradiction'};
const REL_COLOR={association:'#8aa0bd',context:'#46b1ff',dependency:'#46d39a',
  elaboration:'#c792ea',contradiction:'#ff6b6b'};
const FLOW='flow';
const FLOW_COLOR='#46b1ff';
// RAG rung kinds: label, accent color, and which binding/params the inspector shows.
const RUNG={
  'source':        {label:'Source',        color:'#46b1ff', bind:'source'},
  'context-group': {label:'Context group', color:'#7c9cff', bind:null},
  'retriever':     {label:'Retriever',     color:'#46d39a', bind:'retriever'},
  'reranker':      {label:'Reranker',      color:'#37c7c7', bind:null},
  'agent':         {label:'Agent',         color:'#c792ea', bind:'agent'},
  'prompt':        {label:'Prompt',        color:'#ffb454', bind:'prompt'},
  'output':        {label:'Output',        color:'#ff8fb1', bind:null},
};
const RUNG_ORDER=['source','context-group','retriever','reranker','agent','prompt','output'];
const isRung=k=>k in RUNG;
const SHAPES=['rect','ellipse','diamond','rounded'];
let shapeIx=0;                                // cycles when the Shape tool is re-clicked
// Board-level canvas config. `tint` is the base canvas shade (adjustable). The
// image/swimlanes graphic is revealed only through shapes whose fill mode is
// 'seethrough' (see bgDefFor + mm-shapes.js). Fill patterns are per-shape.
// Kept per board in this browser's localStorage only.
const DEFAULT_TINT='#0a0e14';
const BG_DEFAULT={on:false,type:'swimlanes',color:'#46b1ff',opacity:0.5,
             lanes:4,dir:'v',image:'',tint:DEFAULT_TINT};
let boardBg=Object.assign({},BG_DEFAULT);
let catalog=null;                             // {sources,agents}, lazily fetched + cached
let lastRun=null;                             // last /rag/run result, keyed for per-rung chips

let mapId='';
let nodes=[], edges=[], strokes=[], covers=[], selId=null;
let ink=null, brush=null, inkLive=null;
let selEdgeId=null;                           // currently selected relation/flow line
let dragSnapPts=null, snapDotEls=null;        // magnetic snap targets while dragging a node
let multiSel=new Set();                       // Ctrl+click multi-selection (insertion order; first = connect anchor)
let me=null;                                 // the signed-in key {key}
let attribution={nodes:{},editors:{}};       // server-authoritative edit attribution
let relations={};                            // relation name → one-line gloss (board payload)
let _edgeEmphId=null;                        // currently emphasized edge (hover L0 affordance)
let _cardZooming=false;                      // brief suppress during wheel/button zoom
// Suppress cards while contenteditable node text is focused. Derived LIVE from
// document.activeElement (not a sticky flag): a re-render can replace a focused .tx
// without ever firing blur (save → loadAttribution → render), and a
// sticky flag would stay stuck true, suppressing cards forever.
function editingNodeText(){ const ae=document.activeElement;
  return !!(ae&&ae.classList&&(ae.classList.contains('tx')||ae.classList.contains('shape-tx')||ae.classList.contains('frame-tx'))&&ae.isContentEditable); }

// --- edit attribution + identity --------------------------------------------
const _hueCache={};
function editorHue(name){
  if(name in _hueCache) return _hueCache[name];
  let h=0; for(const ch of (name||'?')) h=(h*31+ch.charCodeAt(0))%360;
  return (_hueCache[name]=h);
}
function relTime(ts){
  if(!ts) return '';
  const d=new Date(ts); if(isNaN(d)) return ts;
  const s=Math.max(0,(Date.now()-d.getTime())/1000);
  if(s<60) return Math.floor(s)+'s ago';
  if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function fmtBytes(n){
  if(n==null||n==='') return null;
  const b=+n; if(!isFinite(b)||b<0) return String(n);
  if(b<1024) return b+' B';
  if(b<1024*1024){ const k=b/1024; return (k<10?k.toFixed(1):Math.round(k))+' KB'; }
  const m=b/(1024*1024); return (m<10?m.toFixed(1):Math.round(m))+' MB';
}
function truncTxt(s,n=40){
  s=String(s==null?'':s); return s.length>n?s.slice(0,n-1)+'…':s;
}
/** Capture additive board-payload keys without clobbering loadAttribution() shape. */
function captureBoardPayload(j){
  if(j&&j.attribution&&typeof j.attribution==='object'&&!Array.isArray(j.attribution)){
    // payload key is nodes-only map { [nodeId]: {last_editor,last_edited_at,edit_count} }
    if(j.attribution.nodes&&typeof j.attribution.nodes==='object'){
      attribution.nodes=Object.assign({},attribution.nodes,j.attribution.nodes);
      if(j.attribution.editors) attribution.editors=j.attribution.editors;
    }else{
      attribution.nodes=Object.assign({},attribution.nodes,j.attribution);
    }
  }
  relations=(j&&j.relations&&typeof j.relations==='object')?j.relations:{};
}
function uiBlocksCards(){
  if($('nodebar')&&$('nodebar').classList.contains('open')) return true;
  if($('stampEd')&&$('stampEd').classList.contains('on')) return true;
  const shown=id=>{ const el=$(id); if(!el) return false;
    const d=el.style.display; return !!(d&&d!=='none'); };
  return shown('pop')||shown('colorpop')||shown('framepop')||shown('bgpanel')||shown('rel')||shown('ragmenu')||shown('inspector');
}
let _popupAdjusting=false;   // a popup surface is being dragged or resized
function syncCardSuppress(){
  const on=!!(nodeDrag||shapeResize||nodeScale||marquee||drawing||panning||inkLive||_cardZooming||_popupAdjusting||editingNodeText()||uiBlocksCards());
  cards.suppress(on);
  if(on) clearEdgeEmphasis();
}
function clearEdgeEmphasis(){
  if(!_edgeEmphId) return;
  const esc=window.CSS&&CSS.escape?CSS.escape(_edgeEmphId):_edgeEmphId;
  const vis=$('edges')&&$('edges').querySelector('[data-edge-vis="'+esc+'"]');
  if(vis){
    vis.classList.remove('edge-emph');
    if(vis.localName==='path'||vis.tagName==='path'){
      if(vis.dataset._sw!=null){ vis.setAttribute('stroke-width',vis.dataset._sw); delete vis.dataset._sw; }
      if(vis.dataset._op!=null){ vis.setAttribute('opacity',vis.dataset._op); delete vis.dataset._op; }
    }else{
      vis.querySelectorAll('path').forEach(p=>{
        if(p.dataset._sw!=null){ p.setAttribute('stroke-width',p.dataset._sw); delete p.dataset._sw; }
      });
    }
  }
  _edgeEmphId=null;
}
function setEdgeEmphasis(edgeId){
  if(_edgeEmphId===edgeId) return;
  clearEdgeEmphasis();
  if(!edgeId) return;
  const esc=window.CSS&&CSS.escape?CSS.escape(edgeId):edgeId;
  const vis=$('edges')&&$('edges').querySelector('[data-edge-vis="'+esc+'"]');
  if(!vis) return;
  _edgeEmphId=edgeId;
  vis.classList.add('edge-emph');
  if(vis.localName==='path'||vis.tagName==='path'){
    vis.dataset._sw=vis.getAttribute('stroke-width')||'2';
    vis.dataset._op=vis.getAttribute('opacity')||'0.9';
    vis.setAttribute('stroke-width',String(Math.max(4.5, (+vis.dataset._sw||2)*2)));
    vis.setAttribute('opacity','1');
  }else{
    vis.querySelectorAll('path').forEach(p=>{
      p.dataset._sw=p.getAttribute('stroke-width')||'2';
      p.setAttribute('stroke-width','4.5');
    });
  }
}
function attribFactRow(nodeId){
  const a=attribution.nodes&&attribution.nodes[nodeId];
  if(!a||!a.last_editor) return null;
  return {label:'edited',value:'by '+a.last_editor+' · '+relTime(a.last_edited_at)
    +' · '+(a.edit_count!=null?a.edit_count:0)+' edits'};
}
function attribExpandRows(nodeId){
  const a=attribution.nodes&&attribution.nodes[nodeId];
  const rows=[];
  if(a){
    if(a.last_editor) rows.push({label:'editor',value:a.last_editor});
    if(a.edit_count!=null) rows.push({label:'edits',value:String(a.edit_count)});
    if(a.last_edited_at) rows.push({label:'when',value:relTime(a.last_edited_at)||String(a.last_edited_at)});
  }
  const eds=attribution.editors;
  if(eds&&typeof eds==='object'){
    const names=Object.keys(eds);
    if(names.length) rows.push({label:'editors',value:names.slice(0,8).join(', ')+(names.length>8?'…':'')});
  }
  return rows;
}
function nodeCardPayload(n){
  if(!n) return null;
  const facts=[], expand=[];
  let title=n.text||'(untitled)';
  let subtitle=n.kind||'';
  if(n.kind==='component'){
    const m=n.meta||{};
    subtitle=m.kind||'component';
    if(m.project) facts.push({label:'project',value:String(m.project)});
    if(m.path||m.rel) facts.push({label:'path',value:String(m.path||m.rel)});
    if(m.description) facts.push({label:'desc',value:String(m.description)});
  }else if(n.kind==='file'){
    const m=n.meta||{};
    subtitle=n.entity_type||'file';
    if(m.filename||n.text) facts.push({label:'file',value:String(m.filename||n.text)});
    if(m.method) facts.push({label:'read as',value:String(m.method)});
    if(canPreview(n)&&n.summary) facts.push({label:'summary',value:truncTxt(n.summary,140)});
  }else if(n.meta&&n.meta.role){
    facts.push({label:'role',value:String(n.meta.role)});
  }else{
    const kindLabel=isRung(n.kind)?(RUNG[n.kind]&&RUNG[n.kind].label)||n.kind:n.kind;
    if(kindLabel) facts.push({label:'kind',value:String(kindLabel)});
  }
  if(n.meta&&n.meta.blurb) expand.push({label:'means',value:String(n.meta.blurb)});
  const af=attribFactRow(n.id);
  if(af) facts.push(af);
  expand.push(...attribExpandRows(n.id));
  return {id:n.id,title,subtitle,facts,expand};
}
function edgeCardPayload(e){
  if(!e) return null;
  const a=nodeById(e.source), b=nodeById(e.target); if(!a||!b) return null;
  const title=truncTxt(a.text||a.id)+' → '+truncTxt(b.text||b.id);
  const facts=[];
  if(e.relation) facts.push({label:'relation',value:String(e.relation)});
  const gloss=relations[e.relation]||REL_LABELS[e.relation]||null;
  if(gloss) facts.push({label:'gloss',value:String(gloss)});
  if(e.label) facts.push({label:'label',value:String(e.label)});
  return {id:'edge:'+e.id,title,subtitle:'edge',facts,expand:[]};
}
function resolveHoverTarget(ev){
  // Prefer a .node under the event target
  let nodeEl=(ev.target&&ev.target.closest)?ev.target.closest('.node'):null;
  let edgeId=null;
  if(ev.target&&ev.target.getAttribute){
    edgeId=ev.target.getAttribute('data-edge-id');
  }
  // Fallback: hit-test (nodes sit above #edges; empty space may need elementFromPoint)
  if(!nodeEl&&!edgeId&&typeof document.elementFromPoint==='function'){
    const top=document.elementFromPoint(ev.clientX,ev.clientY);
    if(top){
      if(top.closest) nodeEl=top.closest('.node');
      if(!nodeEl&&top.getAttribute) edgeId=top.getAttribute('data-edge-id');
      // SVG path may be nested; walk up for data-edge-id
      if(!nodeEl&&!edgeId&&top.closest){
        const hit=top.closest('[data-edge-id]');
        if(hit) edgeId=hit.getAttribute('data-edge-id');
      }
    }
  }
  if(nodeEl){
    clearEdgeEmphasis();
    return nodeCardPayload(nodeById(nodeEl.dataset.id));
  }
  if(edgeId){
    const edge=edges.find(x=>x.id===edgeId);
    if(edge){ setEdgeEmphasis(edge.id); return edgeCardPayload(edge); }
  }
  clearEdgeEmphasis();
  return null;
}
/* ---------- connector geometry: default S-curve, or through a dragged waypoint ----------
   An edge may carry `wp` — a world-space point the
   user dragged it to — and the curve is routed THROUGH that point.

   Quadratic rather than cubic, because a quadratic has a closed form for "pass through
   this point at the midpoint": B(0.5) = (P0 + 2C + P2)/4, so C = 2W - (P0+P2)/2. The
   line therefore lands exactly under the cursor instead of near it. `wp` is stored in
   world coords (no OX/OY offset) so it survives pan and zoom, and it is saved by
   toServer() with the rest of the edge. */
function edgeCtrl(wp,x1,y1,x2,y2,OX,OY){
  return {cx:2*(wp.x+OX)-(x1+x2)/2, cy:2*(wp.y+OY)-(y1+y2)/2};
}
function edgePath(e,x1,y1,x2,y2,dx,OX,OY){
  const wp=e&&e.wp;
  if(wp&&isFinite(wp.x)&&isFinite(wp.y)){
    const ox=OX==null?20000:OX, oy=OY==null?20000:OY;
    const c=edgeCtrl(wp,x1,y1,x2,y2,ox,oy);
    return `M ${x1} ${y1} Q ${c.cx} ${c.cy}, ${x2} ${y2}`;
  }
  return `M ${x1} ${y1} C ${x1+dx} ${y1}, ${x2-dx} ${y2}, ${x2} ${y2}`;
}
function appendEdgeHit(svg,d,edgeId){
  const hit=document.createElementNS(SVGNS,'path');
  hit.setAttribute('d',d);
  hit.setAttribute('fill','none');
  hit.setAttribute('stroke','transparent');
  hit.setAttribute('stroke-width','14');
  hit.style.pointerEvents='stroke';
  hit.setAttribute('data-edge-id',edgeId);
  svg.appendChild(hit);
}
async function loadAttribution(){
  if(!mapId) return;
  try{
    const r=await fetch(API+'/api/mindmap/boards/'+encodeURIComponent(mapId)+'/attribution');
    if(!r.ok) return;
    const j=await jsonOf(r), out={};
    Object.entries(j.nodes||{}).forEach(([id,s])=>{
      out[id]={last_editor:s.last_by?('key '+String(s.last_by).slice(0,8)):'starter',
               last_edited_at:s.last_at, edit_count:s.edits};
    });
    attribution={nodes:out,editors:null};
  }catch(_){}
  render();
}
async function loadMe(){
  try{
    const r=await fetch(API+'/api/auth/me'); if(!r.ok) return;
    const j=await jsonOf(r);
    me={key:String(j.pubkey_id||'')};
    const chip=$('userchip');
    chip.innerHTML='<span class="dot"></span> signed in · key <b>'+escapeHtml(me.key.slice(0,8))+'</b>'
      +' · <a href="#" id="signout">sign out</a>';
    const so=$('signout'); if(so) so.onclick=async(ev)=>{ev.preventDefault();
      await fetch(API+'/api/auth/logout',{method:'POST'}); location.href='/login';};
  }catch(_){}
}
let cam={x:0,y:0,scale:1};
let minimap=null;                             // created at init (mm-minimap.js)
let tool='select', linkFrom=null, flowFrom=null;  // tool: select|note|text|shape|link|flow|shapes
let undoStack=[], redoStack=[], dragSnap=null;
let currentShape=null, drawing=null;        // architecture/flowchart shape draw-mode state

const $=id=>document.getElementById(id);
const uid=(p='n')=>p+Math.random().toString(36).slice(2,9);
const toast=m=>{const t=$('toast');t.textContent=m;t.classList.add('show');
  clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),2200)};

/* PRNG + generative icons live in /mindmap/js/mm-icons.js */

/* seeded loading animation lives in /mindmap/js/mm-icons.js */

/* ---------- camera ---------- */
function applyCam(){
  $('world').style.transform=`translate(${cam.x}px,${cam.y}px) scale(${cam.scale})`;
  updateZoom();
  if(minimap)minimap.update();
}
function screenToWorld(sx,sy){
  const st=$('stage').getBoundingClientRect();
  return {x:(sx-st.left-cam.x)/cam.scale, y:(sy-st.top-cam.y)/cam.scale};
}
function viewCenterWorld(){
  const st=$('stage').getBoundingClientRect();
  return screenToWorld(st.left+st.width/2, st.top+st.height/2);
}

/* ---------- model ops ---------- */
function addNode(text,x,y,kind='idea',glyph='',icon=null,meta=null){
  const n={id:uid(),text,x,y,kind,glyph,icon:icon||null,meta:meta||{}};
  // rung/shape/text nodes carry their own chrome — don't fetch a procedural icon
  if(kind==='shape'){n.shape=SHAPES[shapeIx%SHAPES.length];n.w=n.w||150;n.h=n.h||90;}
  nodes.push(n);
  if(!n.icon && !isRung(kind) && kind!=='shape' && kind!=='text') fetchIcon(n);
  render(); return n;
}
// Drop a RAG rung at the view centre with sensible default meta.
function addRung(kind){
  const c=viewCenterWorld();
  pushUndo();
  const meta={};
  if(kind==='retriever') meta.params={k:5,query:''};
  if(kind==='prompt') meta.params={template:''};
  const n=addNode(RUNG[kind].label, c.x+(Math.random()*50-25), c.y+(Math.random()*40-20),
    kind,'',{},meta);
  selId=n.id; render(); openInspector(n);
  return n;
}
/** The server's icon (omli.boards.icon) in the page's form: a named icon is one of the
    icon files shipped with the page; anything else is a procedural seed. */
function pageIcon(ic){
  if(ic&&ic.kind==='named'&&/^[a-z0-9-]+$/.test(ic.name||''))
    return {kind:'library',src:'/mindmap/icons/'+ic.name+'.svg',hue:200};
  if(ic&&ic.kind==='seed')
    return {seed:parseInt(String(ic.seed||'1').slice(0,8),16)||1,hue:ic.hue,form:ic.form,arms:ic.arms};
  return {};
}
async function fetchIcon(n){
  try{const r=await fetch(API+'/api/mindmap/icon',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({text:n.text||'',glyph:typeof n.glyph==='string'?n.glyph:''})});
    if(!r.ok) return;
    const j=await jsonOf(r); if(j.icon){n.icon=pageIcon(j.icon); render();}
  }catch(e){}
}
function nodeById(id){return nodes.find(n=>n.id===id)}

/* ---------- render ---------- */
function render(){
  // nodes
  const world=$('world');
  [...world.querySelectorAll('.node')].forEach(e=>e.remove());
  // frames render first so they sit BEHIND every other node (stable within class)
  const order=[...nodes].sort((a,b)=>(a.kind==='frame'?0:1)-(b.kind==='frame'?0:1));
  order.forEach(n=>{
    if(n._hidden) return;
    const el=document.createElement('div');
    const sel=(n.id===selId?' sel':''), lk=(linkFrom===n.id?' link1':'');
    el.dataset.id=n.id; el.style.left=n.x+'px'; el.style.top=n.y+'px';
    if(n.kind==='frame'){                     // titled region (drawn behind)
      const w=n.w||600, h=n.h||400;
      el.className='node frame'+sel+(multiSel.has(n.id)?' msel':'');
      el.style.width=w+'px'; el.style.height=h+'px'; el.tabIndex=0;
      // per-frame styling (set from the frame-properties popover), like shape/idea nodes
      if(n.meta&&n.meta.fill) el.style.background=n.meta.fill;
      if(n.meta&&n.meta.stroke) el.style.borderColor=n.meta.stroke;
      const plate=localImage(n.meta&&n.meta.photo);
      const covered=covers.some(c=>c.target===n.id);
      const outside=!plate&&imagePlaceholder(n.meta&&n.meta.photo,'frame-ph');
      const under=plate
        ? `<img class="frame-photo" alt="" src="${plate}">`
        : outside ? outside : (covered?`<div class="frame-plate"><b>${escapeHtml((n.meta&&n.meta.detail)||n.text||'Plate')}</b><i>scratch to reveal</i></div>`:'');
      el.innerHTML=under
        +`<div class="frame-label"><b>${n.order!=null?n.order:''}</b>`
        +`<span class="frame-tx" contenteditable>${escapeHtml(n.text||'Frame')}</span></div>`
        +`<div class="shape-resize" title="Drag to resize"></div>`;
      if(n.meta&&n.meta.textColor){ const ftx=el.querySelector('.frame-tx'); if(ftx) ftx.style.color=n.meta.textColor; }
      world.appendChild(el); return;
    }
    // shape + image nodes carry their own chrome and use the unified SVG
    // renderer — early-return before the rung/idea class machinery.
    if(n.kind==='shape'){                          // architecture/flowchart/basic shape node
      const w=n.w||150, h=n.h||90;
      el.className='node shape'+sel+lk+(multiSel.has(n.id)?' msel':'');
      el.style.width=w+'px'; el.style.height=h+'px';
      el.tabIndex=0;
      el.innerHTML=`<div class="shape-svg">${shapeSVG(n.shape,w,h,n.meta&&n.meta.shapeStyle,bgDefFor(n),shapeFill(n))}</div>`
        +`<div class="shape-tx" contenteditable>${escapeHtml(n.text)}</div>`
        +`<div class="shape-resize" title="Drag to resize"></div>`;
      if(n.meta&&n.meta.labelSize){
        const stx=el.querySelector('.shape-tx');
        if(stx) stx.style.fontSize=n.meta.labelSize+'px';
      }
      applyTextColor(el,n);
      world.appendChild(el); return;
    }
    if(n.kind==='image'){                          // pasted/dropped picture node
      el.className='node image'+sel+lk+(multiSel.has(n.id)?' msel':'');
      el.tabIndex=0;
      const raw=(n.meta&&n.meta.src)||'', src=localImage(raw);
      el.innerHTML=src?`<img src="${src}" alt="${escapeHtml(n.text)}" draggable="false">`
        :(imagePlaceholder(raw)||'<div class="img-ph">Image not available</div>');
      world.appendChild(el); return;
    }
    const mi=multiSel.has(n.id);
    const rung=isRung(n.kind);
    let cls='node'+(n.kind==='note'?' note':'')+(rung?' rung':'')
      +(n.kind==='text'?' text':'')
      +(n.id===selId?' sel':'')+(linkFrom===n.id?' link1':'')
      +(flowFrom===n.id?' link1':'')+(mi?' msel':'')
      +(mi&&[...multiSel][0]===n.id?' anchor':'');
    // pipeline run status (from the last /rag/run), if this rung took part
    let chip='';
    if(rung && lastRun && lastRun.byId && lastRun.byId[n.id]){
      const r=lastRun.byId[n.id], st=r.status||'ok';
      cls+=' r-'+st+(st==='ok'?'':'');
      chip=`<div class="rungchip ${st}">${st==='ok'?'✓':st==='skipped'?'skip':'✕'}`
        +(r.output&&r.output.snippets?(' '+r.output.snippets.length):'')+`</div>`;
    }
    if(rung && n._running) cls+=' running';
    if(n._invalid) cls+=' invalid';
    el.className=cls;
    if(n.meta&&n.meta.fill) el.style.background=n.meta.fill;
    if(n.meta&&n.meta.stroke) el.style.borderColor=n.meta.stroke;
    // attribution badge (shared by all kinds)
    let badge='';
    const a=attribution.nodes&&attribution.nodes[n.id];
    if(a&&a.last_editor){
      const h=editorHue(a.last_editor);
      el.classList.add('edited');
      el.style.setProperty('--attrib','hsl('+h+',70%,58%)');
      badge='<div class="attrib" style="background:hsl('+h+',70%,52%)">'
        +escapeHtml((a.last_editor[0]||'?').toUpperCase())+'<i>'+escapeHtml(String(a.edit_count))+'</i></div>';
    }
    el.tabIndex=0;
    if(rung){
      el.style.setProperty('--kc',RUNG[n.kind].color);
      el.innerHTML=badge+chip+rungInner(n);
    }else if(n.kind==='shape'||n.kind==='text'){
      const editable=' contenteditable';
      el.innerHTML=badge+`<div class="tx"${editable}>${escapeHtml(n.text)}</div>`;
      if(n.kind==='text' && n.w){ el.style.width=n.w+'px'; el.classList.add('wrap'); }
    }else if(n.kind==='component'){
      el.classList.add('component');
      const m=n.meta||{};
      const cat=m.kind?`<div class="cat">${escapeHtml(m.kind)}</div>`:'';
      const origin=m.project?`<div class="origin">from ${escapeHtml(m.project)}</div>`:'';
      el.innerHTML=`${cat}${badge}${svgIcon(n.icon)}
        <div class="tx">${escapeHtml(n.text)}</div>${origin}`;
    }else if(n.kind==='file'){                       // dropped file: title, sensitivity, tags
      el.classList.add('component','file');
      const sensList=Array.isArray(n.sensitivity)?n.sensitivity:(n.sensitivity?[n.sensitivity]:[]);
      const et=n.entity_type||'File';
      const tags=(Array.isArray(n.tags)?n.tags:[]).slice(0,2)
        .map(t=>`<span class="tag">${escapeHtml(t)}</span>`).join('');
      const sens=sensList.map(s=>`<div class="sens s-${['public','internal','local-only'].includes(s)?s:'other'}">${escapeHtml(s)}</div>`).join('');
      el.innerHTML=`<div class="cat">${escapeHtml(et)}</div>${badge}${svgIcon(n.icon)}
        <div class="tx">${escapeHtml(n.text)}</div>${sens}
        <div class="tags">${tags}</div>`;
    }else{
      const cat=n.glyph?`<div class="cat">${n.glyph}</div>`:'';
      const editable=n.kind==='note'?' contenteditable':'';
      el.innerHTML=`${cat}${badge}<div class="spin"></div>${svgIcon(n.icon)}
        <div class="tx"${editable}>${escapeHtml(n.text)}</div>`;
    }
    // icon-bearing nodes (idea/generated/note/component/file) get a scale grip; the
    // persisted meta.scale re-applies the transform on every render (el is rebuilt).
    if(!rung && n.kind!=='text'){
      el.insertAdjacentHTML('beforeend','<div class="node-resize" title="Drag to scale"></div>');
      const sc=(n.meta&&+n.meta.scale)||1;
      if(sc && sc!==1) el.style.transform=`translate(-50%,-50%) scale(${sc})`;
    }
    applyTextColor(el,n);
    world.appendChild(el);
  });
  fitAllNodeText();         // labels scale to fit their box rather than being clipped
  drawEdges();
  renderInk();
  updateToolbar();
  if(minimap)minimap.update();      // publish whatever this render changed (debounced diff)
}
function ensureInk(){
  if(ink) return ink;
  ink=inkFns.createInk($('world'));
  if(!brush) brush=inkFns.loadBrush();
  return ink;
}
function renderInk(){
  ensureInk().render(strokes, covers, nodes, inkLive);
}
function loadInk(m){
  strokes=Array.isArray(m&&m.strokes)?m.strokes.slice():[];
  covers=Array.isArray(m&&m.covers)?m.covers.slice():[];
}
function pressureOf(e){
  if(e.pressure>0) return e.pressure;
  return 1;
}
function startInkStroke(e){
  const w=screenToWorld(e.clientX,e.clientY);
  const mode=tool==='scratch'?'scratch':'paint';
  const clip=inkFns.resolveClip(w.x,w.y,mode,covers,nodes);
  const layer=inkFns.resolveLayer(w.x,w.y,covers,nodes);
  if(mode==='scratch' && !layer){
    toast('No layer here — add a layer, then scratch to reveal what is under it');
  }
  inkLive=inkFns.makeStroke('sk-'+Date.now().toString(36), mode,
    [{x:w.x,y:w.y,p:pressureOf(e)}], brush||inkFns.loadBrush(), clip, layer);
  inkLive._t0=performance.now();
  renderInk();
  syncCardSuppress();
}
function extendInkStroke(e){
  if(!inkLive) return;
  const t0=performance.now();
  const w=screenToWorld(e.clientX,e.clientY);
  const pts=inkLive.points;
  const last=pts[pts.length-1];
  if(last && Math.hypot(w.x-last.x,w.y-last.y)<0.6) return;
  pts.push({x:w.x,y:w.y,p:pressureOf(e)});
  renderInk();
  if(ink) ink.noteLatency(performance.now()-t0);
}
function commitInkStroke(){
  if(!inkLive) return;
  const live=inkLive; inkLive=null;
  if(!live.points || live.points.length<2){ renderInk(); syncCardSuppress(); return; }
  pushUndo();
  strokes=strokes.concat([live]);
  render();
  const st=ink&&ink.stats();
  if(st&&st.n>=8){
    const el=$('inkStat');
    if(el) el.textContent='p95 '+st.p95.toFixed(1)+'ms';
  }
  syncCardSuppress();
}
// Inner HTML for a RAG rung node: kind chip + catalog binding + params.
function rungInner(n){
  const meta=n.meta||{}, ref=meta.catalog_ref||null, params=meta.params||{};
  let bind='', unbound=false;
  if(n.kind==='source') { bind=ref&&ref.name?ref.name:'(pick project)'; unbound=!(ref&&ref.path); }
  else if(n.kind==='agent') { bind=ref&&(ref.serving_tag||ref.model_id)?(ref.serving_tag||ref.model_id):'(pick model)'; unbound=!(ref&&ref.model_id); }
  else bind=escapeHtml(n.text||RUNG[n.kind].label);
  let prm='';
  if(n.kind==='retriever') prm=`k=${params.k??5}`+(params.query?` · “${escapeHtml(String(params.query).slice(0,16))}”`:'');
  if(n.kind==='prompt'&&params.template) prm='“'+escapeHtml(String(params.template).slice(0,22))+'”';
  return `<div class="khead"><span class="kdot"></span><span class="kname">${RUNG[n.kind].label}</span></div>`
    +`<div class="bind${unbound?' unbound':''}">${escapeHtml(bind)}</div>`
    +(prm?`<div class="prm">${prm}</div>`:'');
}
function escapeHtml(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

const SVGNS='http://www.w3.org/2000/svg';
function drawEdges(){
  const svg=$('edges'); svg.innerHTML='';
  const prevEmph=_edgeEmphId; _edgeEmphId=null; // DOM wiped; re-apply after rebuild if still set
  const OX=20000, OY=20000; // edges svg is offset; world coords + OX/OY
  // arrowhead marker for flow edges (defs are wiped by innerHTML='' so re-add each pass)
  const defs=document.createElementNS(SVGNS,'defs');
  defs.innerHTML=`<marker id="flowArrow" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0L10 5L0 10z" fill="${FLOW_COLOR}"/></marker>`;
  svg.appendChild(defs);
  drawBgOutline(svg,OX,OY);        // appended first so it sits behind every edge
  edges.forEach(e=>{
    const a=nodeById(e.source), b=nodeById(e.target); if(!a||!b)return;
    if(a._hidden||b._hidden)return;
    const isFlow=e.relation===FLOW;
    const isSel=e.id===selEdgeId;
    const col=isFlow?FLOW_COLOR:(REL_COLOR[e.relation]||'#8aa0bd');
    const x1=a.x+OX,y1=a.y+OY,x2=b.x+OX,y2=b.y+OY;
    // Label anchor. For a reshaped line this must be the point ON the curve, not the
    // straight-line midpoint, or the label detaches and floats beside its own connector.
    // Quadratic at t=0.5 is (P0 + 2C + P2)/4.
    let mx=(x1+x2)/2, my=(y1+y2)/2;
    if(e.wp&&isFinite(e.wp.x)&&isFinite(e.wp.y)){
      const c=edgeCtrl(e.wp,x1,y1,x2,y2,OX,OY);
      mx=(x1+2*c.cx+x2)/4; my=(y1+2*c.cy+y2)/4;
    }
    const path=document.createElementNS(SVGNS,'path');
    const dx=(x2-x1)*0.3;
    const d=edgePath(e,x1,y1,x2,y2,dx,OX,OY);
    path.setAttribute('d',d);
    path.setAttribute('fill','none');path.setAttribute('stroke',col);
    path.setAttribute('opacity','0.9');
    if(isFlow){
      const pid='fp-'+e.id;
      path.setAttribute('id',pid);
      path.setAttribute('stroke-width','2.6');
      path.setAttribute('class','flowpath');
      path.setAttribute('data-edge-vis',e.id);
      path.setAttribute('marker-end','url(#flowArrow)');
      if(isSel){ path.setAttribute('stroke-width','4.4'); path.setAttribute('opacity','1');
        path.style.filter='drop-shadow(0 0 6px '+col+')'; }
      svg.appendChild(path);
      // flowing packets along the connector (native SVG, no JS loop)
      for(let i=0;i<2;i++){
        const c=document.createElementNS(SVGNS,'circle');
        c.setAttribute('r','3');c.setAttribute('class','flowpart');c.setAttribute('fill',col);
        const am=document.createElementNS(SVGNS,'animateMotion');
        am.setAttribute('dur','1.4s');am.setAttribute('repeatCount','indefinite');
        am.setAttribute('begin',(i*0.7)+'s');am.setAttribute('path',d);
        c.appendChild(am); svg.appendChild(c);
      }
      appendEdgeHit(svg,d,e.id);
      return;
    }
    path.setAttribute('stroke-width','2');
    if(e.relation==='contradiction')path.setAttribute('stroke-dasharray','5 4');
    // sketch skin: relation edges redraw hand-drawn (flow edges stay crisp —
    // their packets animate along the exact path geometry)
    const rp=isSketch()?roughPath(svg,d,{stroke:col,
      dash:e.relation==='contradiction'?'5 4':null}):null;
    const vis=rp||path;
    vis.setAttribute('data-edge-vis',e.id);
    if(isSel){ vis.setAttribute('stroke-width','3.6'); vis.setAttribute('opacity','1');
      if(vis.style) vis.style.filter='drop-shadow(0 0 6px '+col+')'; }
    svg.appendChild(vis);
    const t=document.createElementNS(SVGNS,'text');
    t.setAttribute('x',mx);t.setAttribute('y',my-4);t.setAttribute('fill',col);
    t.setAttribute('font-size',isSel?'11':'9');t.setAttribute('text-anchor','middle');
    if(isSel)t.setAttribute('font-weight','700');
    t.setAttribute('opacity',isSel?'1':'0.85');t.textContent=e.relation||'';
    svg.appendChild(t);
    appendEdgeHit(svg,d,e.id);
  });
  if(prevEmph) setEdgeEmphasis(prevEmph);
}

function commitChild(parent,idea){
  pushUndo();
  // A suggestion may name the existing node it hangs off (`attach_to`);
  // otherwise it hangs off the clicked node.
  const anchor=(idea.attach_to&&nodeById(idea.attach_to))||parent;
  // place around the anchor, spreading by existing child count
  const sibs=edges.filter(e=>e.source===anchor.id).length;
  const ang=(-0.6+sibs*0.5);
  const dist=190;
  const x=anchor.x+Math.cos(ang)*dist+Math.sin(sibs)*30;
  const y=anchor.y+Math.sin(ang)*dist+120;
  const child=addNode(idea.text,x,y,'generated',idea.glyph||'',idea.icon||null);
  edges.push({id:uid('e'),source:anchor.id,target:child.id,relation:idea.relation||'association',label:''});
  render(); toast('Added: '+idea.text);
}

/* ---------- node action sidebar (single click) ----------
   A plain click opens this sidebar of
   analysis actions. "Generate next ideas" and "Ask: critique this idea" both run
   their model work in the lower-right thinking bubble (thinkOpen → thinkBody). */
let nodebarId=null;
const NB_ACTIONS=[
  {key:'suggest',icon:'✨',label:'Generate next ideas',desc:'One gated call to your model'},
  {key:'analyze',icon:'🔬',label:'Ask: critique this idea',desc:'Strongest point, weakest assumption, open question'},
];
function openNodebar(n){
  nodebarId=n.id;
  $('nbTitle').textContent=n.text||'(untitled)';
  const off=!modelStatus.configured;
  $('nbActs').innerHTML=NB_ACTIONS.map(a=>
    `<button class="act" data-k="${a.key}"${off?' disabled title="No model configured"':''}><span class="ai">${a.icon}</span>`
    +`<span><span class="al">${escapeHtml(a.label)}</span>`
    +`<span class="ad">${escapeHtml(off?'No model configured':a.desc)}</span></span></button>`).join('')
    +(off?`<div class="nbnote">${escapeHtml(MODEL_OFF_LINE)}</div>`:'');
  $('nbActs').querySelectorAll('.act').forEach(b=>b.onclick=()=>{ if(!b.disabled) nodebarAction(b.dataset.k,n); });
  $('nodebar').classList.add('open');
  syncCardSuppress();
}
function closeNodebar(){ nodebarId=null; $('nodebar').classList.remove('open'); syncCardSuppress(); }
function nodebarAction(key,n){
  if(key==='suggest'){ closeNodebar(); suggestNextIdeas(n); }
  else if(key==='analyze'){ closeNodebar(); analyzeNode(n); }
}

/* ---------- lower-right procedural "thinking" bubble ----------
   Reuses the seeded procedural loader (startLoading/stopLoading) but bubbles up
   in the corner instead of anchoring to the node, so it never fights the click. */
function thinkOpen(n,title){
  const t=$('think');
  $('thinkTitle').textContent=title||'Thinking';
  $('thinkBody').innerHTML='<canvas class="loader"></canvas><div class="loadcap"></div>';
  t.classList.remove('show'); void t.offsetWidth; t.classList.add('show');
  const el=n&&document.querySelector(`.node[data-id="${n.id}"]`); if(el)el.classList.add('busy');
  startLoading($('thinkBody').querySelector('canvas.loader'),
    (n&&n.icon)||{}, $('thinkBody').querySelector('.loadcap'));
}
function thinkDone(n){ stopLoading();
  const el=n&&document.querySelector(`.node[data-id="${n.id}"]`); if(el)el.classList.remove('busy'); }
function thinkBody(html){ $('thinkBody').innerHTML=html; }
function thinkClose(){ stopLoading(); $('think').classList.remove('show');
  document.querySelectorAll('.node.busy').forEach(el=>el.classList.remove('busy')); }

/* ---------- sidebar actions ----------
   Each action saves the board, then asks the server for one gated model call on the
   saved board (pre-gate, your registry's model, post-gate). A refusal names the gate.
   Every fetch carries a client timeout so the thinking bubble can never spin forever. */
const THINK_TIMEOUT_MS=120000;
async function modelCall(path,body){
  const ctl=new AbortController();
  const t=setTimeout(()=>ctl.abort(), THINK_TIMEOUT_MS);
  try{
    const r=await fetch(API+path,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body), signal:ctl.signal});
    const j=await jsonOf(r);
    if(r.ok) return {ok:true,...j};
    if(j.refused) return {ok:false,error:'Refused by the '+j.refused+(j.reason?': '+j.reason:'')};
    return {ok:false,error:j.error||('the server answered '+r.status)};
  }catch(e){
    return {ok:false,error:e.name==='AbortError'?'Timed out waiting for the model.':'The request failed.'};
  }finally{ clearTimeout(t); }
}
async function focusedCall(n,path,title){
  thinkOpen(n,title);
  if(!(await saveBoard(true))){ thinkDone(n); thinkBody('<div class="tsummary">The board could not be saved, so nothing was asked.</div>'); return null; }
  const j=await modelCall(path,{board:mapId,node:n.id});
  thinkDone(n);
  if(!j.ok){ thinkBody(`<div class="tsummary">${escapeHtml(j.error)}</div>`); return null; }
  return j;
}
async function suggestNextIdeas(n){
  const j=await focusedCall(n,'/api/mindmap/expand','Next ideas'); if(!j) return;
  const ideas=String(j.text||'').split(/\r?\n/).map(s=>s.replace(/^\s*(?:[-*\u2022]|\d+[.)])\s*/,'').trim())
    .filter(Boolean).slice(0,8).map(text=>({text:text.slice(0,160),relation:'association'}));
  if(!ideas.length){ thinkBody('<div class="tsummary">The model suggested nothing new.</div>'); return; }
  let html='';
  ideas.forEach((id,i)=>{
    html+=`<div class="choice" data-i="${i}">${svgIcon(null,30).replace('class="ic"','class="ci"')}`
      +`<div><div class="cl">${escapeHtml(id.text)}</div><div class="cr">click to add</div></div></div>`;
  });
  thinkBody(html);
  $('thinkBody').querySelectorAll('.choice').forEach(c=>{
    c.onclick=()=>{ commitChild(n,ideas[+c.dataset.i]); thinkClose(); };
  });
}
async function analyzeNode(n){
  const j=await focusedCall(n,'/api/mindmap/analyze','Critique'); if(!j) return;
  thinkBody(`<div class="tsummary">${escapeHtml(j.text||'(no answer)')}</div>`);
}

/* ---------- relationship picker (link mode) ---------- */
function openRelPicker(a,b,scrX,scrY){
  const rel=$('rel'); rel.style.display='block'; rel.style.left=scrX+'px'; rel.style.top=scrY+'px';
  let html='<h3>Relationship</h3>';
  Object.keys(REL_LABELS).forEach(k=>{
    html+=`<button class="rel-opt" data-k="${k}"><b>${k}</b> — ${REL_LABELS[k]}</button>`;
  });
  rel.innerHTML=html;
  rel.querySelectorAll('.rel-opt').forEach(o=>{
    o.onclick=()=>{
      pushUndo();
      edges.push({id:uid('e'),source:a,target:b,relation:o.dataset.k,label:''});
      rel.style.display='none'; setTool('select'); render(); toast('Linked: '+o.dataset.k);
      syncCardSuppress();
    };
  });
  syncCardSuppress();
}
function clearLink(){linkFrom=null; render();}

/* ---------- selectable lines (edges) ---------- */
// Resolve a data-edge-id under a pointer event (the wide invisible hit paths in
// #edges carry it; fall back to elementFromPoint when the target is the label).
function edgeIdAt(e){
  let id=(e.target&&e.target.getAttribute)?e.target.getAttribute('data-edge-id'):null;
  if(!id&&typeof document.elementFromPoint==='function'){
    const top=document.elementFromPoint(e.clientX,e.clientY);
    if(top&&top.closest){ const h=top.closest('[data-edge-id]'); if(h) id=h.getAttribute('data-edge-id'); }
  }
  return id;
}
function clearEdgeSel(){ if(selEdgeId){ selEdgeId=null; drawEdges(); updateToolbar(); } }
function selectEdge(id,scrX,scrY){
  selId=null; if(multiSel.size)multiSel.clear();
  selEdgeId=id;
  $('pop').style.display='none'; closeNodebar(); clearEdgeEmphasis();
  render(); updateToolbar();
  const ed=edges.find(x=>x.id===id);
  if(ed&&scrX!=null) openEdgeEditor(ed,scrX,scrY);
  syncCardSuppress();
}
// Small context popover for the selected line: change its relation, or delete it.
function openEdgeEditor(ed,scrX,scrY){
  const rel=$('rel'); rel.style.display='block';
  rel.style.left=Math.min(window.innerWidth-220,scrX)+'px';
  rel.style.top=Math.max(60,scrY)+'px';
  const a=nodeById(ed.source), b=nodeById(ed.target);
  let html='<h3>Line</h3>'
    +`<div style="font-size:10px;color:var(--dim);margin:-2px 0 6px">`
    +`${escapeHtml(truncTxt((a&&a.text)||a&&a.id||'?',22))} → ${escapeHtml(truncTxt((b&&b.text)||b&&b.id||'?',22))}</div>`;
  if(ed.relation!==FLOW){
    Object.keys(REL_LABELS).forEach(k=>{
      const on=ed.relation===k?' style="border-color:var(--accent);color:var(--ink)"':'';
      html+=`<button class="rel-opt" data-k="${k}"${on}><b>${k}</b> — ${REL_LABELS[k]}</button>`;
    });
  }
  html+=`<button class="rel-opt" data-del="1" style="border-color:#ff6b6b;color:#ff9a9a"><b>Delete line</b></button>`;
  rel.innerHTML=html;
  rel.querySelectorAll('.rel-opt').forEach(o=>{ o.onclick=()=>{
    if(o.dataset.del){ rel.style.display='none'; deleteSelected(); return; }
    pushUndo();
    const i=edges.findIndex(x=>x.id===ed.id);
    if(i>=0) edges[i]={...edges[i],relation:o.dataset.k};
    rel.style.display='none'; render(); syncCardSuppress();
  };});
  syncCardSuppress();
}

/* ---------- magnetic snap: node → connection points on shapes ---------- */
// The four side-midpoints + center of every shape box (for a diamond these ARE
// its vertices). Excludes the node being dragged.
function computeSnapPoints(excludeId){
  const pts=[];
  nodes.forEach(n=>{ if(n.kind!=='shape'||n.id===excludeId||n._hidden) return;
    const w=n.w||150, h=n.h||90;
    pts.push({x:n.x,y:n.y},{x:n.x,y:n.y-h/2},{x:n.x,y:n.y+h/2},
             {x:n.x-w/2,y:n.y},{x:n.x+w/2,y:n.y});
  });
  return pts;
}
function clearSnapDots(){
  document.querySelectorAll('#world .snapdot').forEach(d=>d.remove());
  snapDotEls=null; dragSnapPts=null;
}
function renderSnapDots(){
  clearSnapDots();
  dragSnapPts=nodeDrag?computeSnapPoints(nodeDrag.id):null;
  if(!dragSnapPts||!dragSnapPts.length){ dragSnapPts=null; return; }
  const world=$('world');
  snapDotEls=dragSnapPts.map(p=>{ const d=document.createElement('div');
    d.className='snapdot'; d.style.left=p.x+'px'; d.style.top=p.y+'px';
    world.appendChild(d); return d; });
}
// Snap the dragged node's center to the nearest shape point within a screen-
// constant threshold; returns the snapped {x,y} or the input unchanged.
function applySnap(x,y){
  if(!dragSnapPts) return {x,y};
  const th=13/cam.scale, th2=th*th; let best=null, bd=th2;
  for(const p of dragSnapPts){ const dx=p.x-x, dy=p.y-y, d2=dx*dx+dy*dy;
    if(d2<bd){ bd=d2; best=p; } }
  if(snapDotEls) snapDotEls.forEach((d,i)=>d.classList.toggle('active',dragSnapPts[i]===best));
  return best?{x:best.x,y:best.y}:{x,y};
}

/* ---------- Ctrl+click multi-select → connect many at once ---------- */
function clearMulti(){ if(multiSel.size){multiSel.clear(); $('rel').style.display='none'; render(); syncCardSuppress();} }
function toggleMulti(n,scrX,scrY){
  if(multiSel.has(n.id)) multiSel.delete(n.id); else multiSel.add(n.id);
  selId=null; render();
  if(multiSel.size>=2) openMultiRelPicker(scrX,scrY);
  else{ $('rel').style.display='none'; syncCardSuppress(); }
}
function openMultiRelPicker(scrX,scrY){
  const rel=$('rel'); rel.style.display='block';
  rel.style.left=Math.min(scrX,window.innerWidth-220)+'px'; rel.style.top=scrY+'px';
  const ids=[...multiSel]; const anchor=nodeById(ids[0]);
  let html='<h3>Connect '+ids.length+' nodes</h3>';
  if(anchor) html+='<div style="font-size:9px;color:var(--dim);margin:-2px 0 6px">from <b style="color:var(--ink)">'
    +escapeHtml(anchor.text.slice(0,28))+'</b> → '+(ids.length-1)+' other'+(ids.length>2?'s':'')+'</div>';
  Object.keys(REL_LABELS).forEach(k=>{
    html+=`<button class="rel-opt" data-k="${k}"><b>${k}</b> — ${REL_LABELS[k]}</button>`;
  });
  rel.innerHTML=html;
  rel.querySelectorAll('.rel-opt').forEach(o=>{ o.onclick=()=>connectMulti(o.dataset.k); });
  syncCardSuppress();
}
function connectMulti(relation){
  const ids=[...multiSel]; if(ids.length<2) return;
  pushUndo();
  const src=ids[0]; let added=0;
  for(let i=1;i<ids.length;i++){
    const tgt=ids[i];
    if(edges.some(e=>e.source===src&&e.target===tgt)) continue;
    edges.push({id:uid('e'),source:src,target:tgt,relation,label:''}); added++;
  }
  $('rel').style.display='none'; multiSel.clear(); render();
  toast('Linked '+added+' connection'+(added===1?'':'s')+' · '+relation);
  syncCardSuppress();
}

/* ---------- toolbar: tools, history, zoom ---------- */
// shape tool → tShape (basic click-place / cycle). tShapes is the palette
// opener, not a tool state, so it's intentionally absent here.
const TOOL_BTN={select:'tSelect',text:'tText',note:'tNote',shape:'tShape',
  link:'tLink',flow:'tFlow',frame:'tBg'};
function setTool(t){
  tool=t; linkFrom=null; flowFrom=null;
  Object.values(TOOL_BTN).forEach(id=>{ const el=$(id); if(el) el.classList.remove('active'); });
  if(TOOL_BTN[t] && $(TOOL_BTN[t])) $(TOOL_BTN[t]).classList.add('active');
  stage.classList.toggle('tool-note', t==='note'||t==='text');
  stage.classList.toggle('tool-link', t==='link'||t==='flow');
  stage.classList.toggle('tool-shape', t==='shape'||t==='frame');   // crosshair cursor for drawing
  stage.classList.toggle('tool-ink', t==='paint'||t==='scratch');
  if(t!=='shape') hideShapePalette();
  if(t==='link') toast('Connect ideas: click two nodes → pick a relation');
  if(t==='flow') toast('Flow: click source rung → target rung');
  if(t==='frame') toast('Frame: drag on the canvas to draw a region');
  // basic-shape mode shows the cycle hint; the palette shows its own toast
  if(t==='shape'&&!currentShape) toast('Shape: '+SHAPES[shapeIx%SHAPES.length]+' — click canvas to place');
  render();
}
function snapshot(){return JSON.stringify({nodes,edges,strokes,covers});}
function pushUndo(){undoStack.push(snapshot()); if(undoStack.length>60)undoStack.shift();
  redoStack.length=0; updateToolbar();}
function restoreState(s){const o=JSON.parse(s); nodes=o.nodes; edges=o.edges;
  strokes=o.strokes||[]; covers=o.covers||[]; selId=null; selEdgeId=null; render();}
function undo(){if(!undoStack.length)return; redoStack.push(snapshot()); restoreState(undoStack.pop());}
function redo(){if(!redoStack.length)return; undoStack.push(snapshot()); restoreState(redoStack.pop());}
function deleteSelected(){
  if(selEdgeId&&!selId&&!multiSel.size){    // a selected line → remove just that edge
    pushUndo(); edges=edges.filter(x=>x.id!==selEdgeId);
    selEdgeId=null; $('rel').style.display='none'; render(); return;
  }
  if(multiSel.size){                       // delete every Ctrl+selected node (and its edges)
    pushUndo(); const del=new Set(multiSel);
    nodes=nodes.filter(n=>!del.has(n.id));
    edges=edges.filter(x=>!del.has(x.source)&&!del.has(x.target));
    covers=covers.filter(c=>!del.has(c.target));
    strokes=strokes.map(s=>del.has(s.clip)?{...s,clip:inkFns.CLIP_BOARD}:s);
    multiSel.clear(); selId=null; $('rel').style.display='none'; render(); return;
  }
  if(!selId)return; pushUndo();
  nodes=nodes.filter(n=>n.id!==selId);
  edges=edges.filter(x=>x.source!==selId&&x.target!==selId);
  covers=covers.filter(c=>c.target!==selId);
  strokes=strokes.map(s=>s.clip===selId?{...s,clip:inkFns.CLIP_BOARD}:s);
  selId=null; render();}
function updateToolbar(){
  const u=$('tUndo'),r=$('tRedo'),d=$('tDel');
  if(u)u.disabled=!undoStack.length; if(r)r.disabled=!redoStack.length;
  if(d)d.disabled=!selId&&!multiSel.size&&!selEdgeId;}
function updateZoom(){const z=$('zLabel'); if(z)z.textContent=Math.round(cam.scale*100)+'%';}
function resetZoom(){zoomBy(1/cam.scale);}

/* ---------- interaction ---------- */
const stage=$('stage');
let panning=false, panStart=null, nodeDrag=null, moved=false;
// bottom-right corner resize of a selected shape/frame (top-left stays anchored)
let shapeResize=null, resizeSnap=null, resizeMoved=false;
let nodeScale=null;             // scaling a whole idea/icon node from its corner grip
let edgeDrag=null;              // reshaping a connector: press a line, move, drop a waypoint
const EDGE_DRAG_SLOP=3;         // px before a press on a line counts as a reshape, not a click
let marquee=null;               // rubber-band select (Select tool, drag on empty canvas)
let spaceDown=false;            // hold Space to pan instead of marquee-selecting
// press-and-hold to move: a node press arms a hold timer instead of grabbing
// immediately, so a plain click (→ action sidebar) and a drag stop fighting.
let pendingNode=null, holdTimer=null;
const HOLD_MS=160;

stage.addEventListener('mousedown',e=>{
  if(e.button===2) return;                 // right-click is the context menu (see contextmenu handler)
  if((tool==='paint'||tool==='scratch') && e.button===0){
    e.preventDefault();
    startInkStroke(e);
    return;
  }
  const nodeEl=e.target.closest('.node');
  if(nodeEl){
    const n=nodeById(nodeEl.dataset.id);
    // grab the corner handle → resize the shape instead of moving/selecting it
    if(e.target.classList.contains('shape-resize')){
      e.preventDefault();
      shapeResize={id:n.id, sx:e.clientX, sy:e.clientY,
                   ow:n.w||150, oh:n.h||90, ox:n.x, oy:n.y};
      resizeSnap=snapshot(); resizeMoved=false;   // committed on mouseup only if it changed
      syncCardSuppress();
      return;
    }
    // grab the scale grip → scale the whole node (icon + label) instead of moving it
    if(e.target.classList.contains('node-resize')){
      e.preventDefault();
      nodeScale={id:n.id, sx:e.clientX, sy:e.clientY, os:(n.meta&&+n.meta.scale)||1};
      resizeSnap=snapshot(); resizeMoved=false;
      syncCardSuppress();
      return;
    }
    if(e.target.isContentEditable) return; // editing a note
    clearEdgeSel();
    if(e.ctrlKey||e.metaKey){              // Ctrl/Cmd+click: build a multi-selection
      e.preventDefault(); toggleMulti(n,e.clientX,e.clientY); return;
    }
    // A plain click drops the multi-selection — UNLESS you pressed a member of it,
    // in which case keep it so the hold-drag moves the whole selection together.
    if(multiSel.size && !multiSel.has(n.id)) clearMulti();
    // Move is press-and-hold: arm a hold timer; drag engages only after it fires.
    // A quick press+release (no hold) is a click → the action sidebar (handleNodeClick).
    pendingNode={id:n.id, sx:e.clientX, sy:e.clientY, ox:n.x, oy:n.y}; moved=false;
    clearTimeout(holdTimer);
    holdTimer=setTimeout(()=>{
      if(!pendingNode) return;             // released or cancelled before the hold elapsed
      dragSnap=snapshot();                 // committed on mouseup only if actually moved
      const pn=nodeById(pendingNode.id);
      nodeDrag={id:pendingNode.id, sx:pendingNode.sx, sy:pendingNode.sy,
                ox:pendingNode.ox, oy:pendingNode.oy,
                peers:dragPeers(pn).map(m=>({id:m.id,ox:m.x,oy:m.y}))};   // group/multi move together
      pendingNode=null; holdTimer=null;
      const el=document.querySelector(`.node[data-id="${nodeDrag.id}"]`);
      if(el) el.classList.add('holding');
      renderSnapDots();                    // show shape connection points to snap to
      syncCardSuppress();
    }, HOLD_MS);
    return;
  }
  // select tool: a line can be either CLICKED (select + context actions) or DRAGGED
  // (reshape via a waypoint). Which one it was is only knowable on mouseup, so arm the
  // drag here and let mouseup decide — pressing must not pre-empt the click.
  if(tool==='select'){
    const eid=edgeIdAt(e);
    if(eid){ e.preventDefault(); stopLoading(); $('pop').style.display='none'; closeNodebar();
      edgeDrag={id:eid, sx:e.clientX, sy:e.clientY, snap:snapshot(), moved:false};
      syncCardSuppress(); return; }
  }
  stopLoading(); $('pop').style.display='none'; $('rel').style.display='none'; closeNodebar();
  clearEdgeSel();                          // clicking empty canvas / other tools drops a line selection
  if(multiSel.size) clearMulti();          // clicking empty canvas drops the multi-selection
  // note / text / basic-shape: drop at the click. The palette (currentShape set)
  // uses the drag-to-size path below instead, so exclude it here.
  if(tool==='note'||tool==='text'||(tool==='shape'&&!currentShape)){
    const w=screenToWorld(e.clientX,e.clientY);
    pushUndo();
    const seed=tool==='note'?'New note…':tool==='text'?'Text':'';
    const n=addNode(seed, w.x, w.y, tool);
    selId=n.id; setTool('select');
    // shapes carry their text in .shape-tx; notes/text use .tx
    const txSel=tool==='shape'?'.shape-tx':'.tx';
    setTimeout(()=>{const el=document.querySelector(`.node[data-id="${n.id}"] ${txSel}`);
      if(el){el.focus();document.getSelection().selectAllChildren(el);}},30);
    syncCardSuppress();
    return;
  }
  if(tool==='shape'&&currentShape){        // Shape palette: drag to size a architecture/flowchart shape
    drawing={sx:e.clientX, sy:e.clientY};
    const rb=$('rubber'); rb.style.display='block';
    rb.style.left=e.clientX+'px'; rb.style.top=e.clientY+'px'; rb.style.width='0px'; rb.style.height='0px';
    syncCardSuppress();
    return;
  }
  if(tool==='frame'){                      // Frames tool: drag to draw a titled region
    drawing={sx:e.clientX, sy:e.clientY, frame:true};
    const rb=$('rubber'); rb.style.display='block';
    rb.style.left=e.clientX+'px'; rb.style.top=e.clientY+'px'; rb.style.width='0px'; rb.style.height='0px';
    syncCardSuppress();
    return;
  }
  // Select tool + drag on empty canvas = marquee select (hold Space / middle-drag to pan).
  if(tool==='select' && e.button===0 && !spaceDown){
    marquee={sx:e.clientX, sy:e.clientY, add:e.shiftKey, moved:false};
    const mq=$('marquee'); mq.style.display='block';
    mq.style.left=e.clientX+'px'; mq.style.top=e.clientY+'px'; mq.style.width='0px'; mq.style.height='0px';
    syncCardSuppress();
    return;
  }
  // pan background
  panning=true; moved=false; panStart={x:e.clientX-cam.x,y:e.clientY-cam.y};
  stage.classList.add('panning');
  syncCardSuppress();
});
window.addEventListener('mousemove',e=>{
  if(inkLive){ extendInkStroke(e); return; }
  if(edgeDrag){
    if(Math.abs(e.clientX-edgeDrag.sx)+Math.abs(e.clientY-edgeDrag.sy)>EDGE_DRAG_SLOP){
      edgeDrag.moved=true;
      const ed=edges.find(x=>x.id===edgeDrag.id);
      if(ed){ const w=screenToWorld(e.clientX,e.clientY);
        ed.wp={x:w.x,y:w.y};             // world coords: survives pan/zoom and saves with the edge
        drawEdges(); }
    }
    return;
  }
  if(shapeResize){
    const dx=(e.clientX-shapeResize.sx)/cam.scale, dy=(e.clientY-shapeResize.sy)/cam.scale;
    if(Math.abs(e.clientX-shapeResize.sx)+Math.abs(e.clientY-shapeResize.sy)>3) resizeMoved=true;
    const w=Math.max(40,Math.round(shapeResize.ow+dx)), h=Math.max(30,Math.round(shapeResize.oh+dy));
    const n=nodeById(shapeResize.id);
    n.w=w; n.h=h;
    // node is a center-anchored box (translate(-50%,-50%)); shift the center by half the
    // growth so the top-left corner stays put and the shape grows toward the cursor.
    n.x=shapeResize.ox+(w-shapeResize.ow)/2; n.y=shapeResize.oy+(h-shapeResize.oh)/2;
    const el=document.querySelector(`.node[data-id="${n.id}"]`);
    if(el){
      el.style.width=w+'px'; el.style.height=h+'px';
      el.style.left=n.x+'px'; el.style.top=n.y+'px';
      const sv=el.querySelector('.shape-svg');
      if(sv) sv.innerHTML=shapeSVG(n.shape,w,h,n.meta&&n.meta.shapeStyle,bgDefFor(n),shapeFill(n));
      drawEdges();
    }
    return;
  }
  if(nodeScale){
    if(Math.abs(e.clientX-nodeScale.sx)+Math.abs(e.clientY-nodeScale.sy)>3) resizeMoved=true;
    const d=((e.clientX-nodeScale.sx)+(e.clientY-nodeScale.sy))/2/cam.scale;  // avg corner drag
    const sc=Math.max(0.5,Math.min(3,Math.round((nodeScale.os+d/90)*20)/20));  // 90px/×, snap .05
    const n=nodeById(nodeScale.id); n.meta=n.meta||{}; n.meta.scale=sc;
    const el=document.querySelector(`.node[data-id="${n.id}"]`);
    if(el){ el.style.transform=`translate(-50%,-50%) scale(${sc})`; drawEdges(); }
    return;
  }
  if(drawing){
    const rb=$('rubber');
    const x=Math.min(e.clientX,drawing.sx), y=Math.min(e.clientY,drawing.sy);
    rb.style.left=x+'px'; rb.style.top=y+'px';
    rb.style.width=Math.abs(e.clientX-drawing.sx)+'px'; rb.style.height=Math.abs(e.clientY-drawing.sy)+'px';
    return;
  }
  if(marquee){
    if(Math.abs(e.clientX-marquee.sx)+Math.abs(e.clientY-marquee.sy)>3) marquee.moved=true;
    const x=Math.min(e.clientX,marquee.sx), y=Math.min(e.clientY,marquee.sy);
    const mq=$('marquee'); mq.style.left=x+'px'; mq.style.top=y+'px';
    mq.style.width=Math.abs(e.clientX-marquee.sx)+'px'; mq.style.height=Math.abs(e.clientY-marquee.sy)+'px';
    return;
  }
  if(pendingNode){
    // moved past the slop before the hold fired → not a click, not (yet) a drag: cancel
    if(Math.abs(e.clientX-pendingNode.sx)+Math.abs(e.clientY-pendingNode.sy)>6){
      clearTimeout(holdTimer); holdTimer=null; pendingNode=null;
      toast('Hold to move · click for options');
    }
    return;
  }
  if(nodeDrag){
    const dx=(e.clientX-nodeDrag.sx)/cam.scale, dy=(e.clientY-nodeDrag.sy)/cam.scale;
    if(Math.abs(e.clientX-nodeDrag.sx)+Math.abs(e.clientY-nodeDrag.sy)>3)moved=true;
    const n=nodeById(nodeDrag.id);
    const peers=nodeDrag.peers||[];
    // grouped/multi drags translate by the raw delta (no magnetic snap — snap is a
    // single-node affordance); a lone node still snaps to shape connection points.
    if(peers.length){ n.x=nodeDrag.ox+dx; n.y=nodeDrag.oy+dy; }
    else { const sn=applySnap(nodeDrag.ox+dx, nodeDrag.oy+dy); n.x=sn.x; n.y=sn.y; }
    const place=m=>{const el=document.querySelector(`.node[data-id="${m.id}"]`);
      if(el){el.style.left=m.x+'px'; el.style.top=m.y+'px';}};
    place(n);
    peers.forEach(p=>{ const m=nodeById(p.id); if(m){ m.x=p.ox+dx; m.y=p.oy+dy; place(m); } });
    drawEdges();
  }else if(panning){
    cam.x=e.clientX-panStart.x; cam.y=e.clientY-panStart.y; moved=true; applyCam();
  }
});
window.addEventListener('mouseup',e=>{
  if(inkLive){ commitInkStroke(); return; }
  if(edgeDrag){
    const ed=edgeDrag;
    edgeDrag=null;
    if(ed.moved){                         // it was a reshape — make it undoable
      if(ed.snap){ undoStack.push(ed.snap); if(undoStack.length>60)undoStack.shift();
        redoStack.length=0; updateToolbar(); }
      toast('line reshaped — double-click it to straighten');
    }else{                                // it was a plain click after all: select as before
      selectEdge(ed.id,e.clientX,e.clientY);
    }
    syncCardSuppress();
    return;
  }
  if(shapeResize){
    if(resizeMoved&&resizeSnap){
      undoStack.push(resizeSnap); if(undoStack.length>60)undoStack.shift();
      redoStack.length=0; updateToolbar();
    }
    shapeResize=null; resizeSnap=null;
    syncCardSuppress();
    return;
  }
  if(nodeScale){
    if(resizeMoved&&resizeSnap){
      undoStack.push(resizeSnap); if(undoStack.length>60)undoStack.shift();
      redoStack.length=0; updateToolbar();
    }
    nodeScale=null; resizeSnap=null;
    syncCardSuppress();
    return;
  }
  if(drawing){
    const rb=$('rubber'); rb.style.display='none';
    const sw=Math.abs(e.clientX-drawing.sx), sh=Math.abs(e.clientY-drawing.sy);
    const x0=Math.min(e.clientX,drawing.sx), y0=Math.min(e.clientY,drawing.sy);
    const c=screenToWorld(x0+sw/2, y0+sh/2);
    let w=sw/cam.scale, h=sh/cam.scale;
    const wasFrame=drawing.frame; drawing=null;
    if(wasFrame){                          // Frames tool: create a titled region
      if(sw<24&&sh<24){ w=600; h=400; }    // a click → sensible default frame
      w=Math.max(120,w); h=Math.max(90,h);
      pushUndo();
      const n=addFrame(c.x, c.y, Math.round(w), Math.round(h));
      selId=n.id; setTool('select'); render();
      syncCardSuppress();
      return;
    }
    if(sw<24&&sh<24){ w=150; h=90; }       // a click (no real drag) → sensible default size
    w=Math.max(40,w); h=Math.max(30,h);
    pushUndo();
    const n=addShape(currentShape.key, c.x, c.y, Math.round(w), Math.round(h));
    selId=n.id; setTool('select');
    setTimeout(()=>{const el=document.querySelector(`.node[data-id="${n.id}"] .shape-tx`);
      if(el){el.setAttribute('contenteditable','true'); el.classList.add('editing'); el.focus();}},30);
    syncCardSuppress();
    return;
  }
  if(marquee){                             // rubber-band select on empty canvas
    $('marquee').style.display='none';
    const mv=marquee.moved, add=marquee.add;
    const a=screenToWorld(Math.min(e.clientX,marquee.sx), Math.min(e.clientY,marquee.sy));
    const b=screenToWorld(Math.max(e.clientX,marquee.sx), Math.max(e.clientY,marquee.sy));
    marquee=null;
    if(mv){ selectInRect(a.x,a.y,b.x,b.y,add); }
    else if(!add){ clearMulti(); selId=null; render(); }   // a bare click → deselect
    syncCardSuppress();
    return;
  }
  if(holdTimer){clearTimeout(holdTimer); holdTimer=null;}
  if(pendingNode){                         // released before the hold fired → a click
    const n=nodeById(pendingNode.id); pendingNode=null;
    if(n) handleNodeClick(n,e.clientX,e.clientY);
  }else if(nodeDrag){
    const n=nodeById(nodeDrag.id);
    const el=document.querySelector(`.node[data-id="${nodeDrag.id}"]`);
    if(el) el.classList.remove('holding');
    if(!moved) handleNodeClick(n,e.clientX,e.clientY);   // held in place, never moved → options
    else if(dragSnap){undoStack.push(dragSnap); if(undoStack.length>60)undoStack.shift();
      redoStack.length=0; updateToolbar();}
    dragSnap=null; nodeDrag=null; clearSnapDots();                       // drag-end moves don't re-render
  }
  panning=false; stage.classList.remove('panning');
  syncCardSuppress();
});

// Hold Space to pan with the Select tool (whose empty-canvas drag is marquee-select).
window.addEventListener('keydown',e=>{
  if(e.code==='Space'&&!spaceDown&&!editingNodeText()
     &&!/^(INPUT|TEXTAREA|SELECT)$/.test((e.target&&e.target.tagName)||'')){
    spaceDown=true; stage.style.cursor='grab';
  }
});
window.addEventListener('keyup',e=>{ if(e.code==='Space'){ spaceDown=false; stage.style.cursor=''; } });

function handleNodeClick(n,scrX,scrY){
  if(tool==='link'){
    if(!linkFrom){linkFrom=n.id; render(); toast('Now click the target node');}
    else if(linkFrom!==n.id){openRelPicker(linkFrom,n.id,scrX,scrY);}
    return;
  }
  if(tool==='flow'){                         // draw a directional RAG dataflow arrow
    if(!flowFrom){flowFrom=n.id; render(); toast('Now click the downstream rung');}
    else if(flowFrom!==n.id){
      if(!edges.some(x=>x.source===flowFrom&&x.target===n.id&&x.relation===FLOW)){
        pushUndo();
        edges.push({id:uid('e'),source:flowFrom,target:n.id,relation:FLOW,label:''});
        toast('Flow: '+(nodeById(flowFrom).text||'')+' → '+(n.text||''));
      }
      flowFrom=null; setTool('select'); render();
    }
    return;
  }
  selId=n.id;
  if(isRung(n.kind)){render(); openInspector(n); return;}
  if(n.kind==='note'||n.kind==='shape'||n.kind==='text'||n.kind==='image'||n.kind==='frame'){render(); return;}
  render(); openNodebar(n);
}

// zoom
stage.addEventListener('wheel',e=>{
  e.preventDefault();
  _cardZooming=true; syncCardSuppress();
  const st=stage.getBoundingClientRect();
  const mx=e.clientX-st.left, my=e.clientY-st.top;
  const wx=(mx-cam.x)/cam.scale, wy=(my-cam.y)/cam.scale;
  const factor=e.deltaY<0?1.12:0.89;
  cam.scale=Math.max(0.15,Math.min(3,cam.scale*factor));
  cam.x=mx-wx*cam.scale; cam.y=my-wy*cam.scale; applyCam();
  _cardZooming=false; syncCardSuppress();
},{passive:false});

// progressive hover mini-cards (nodes + edge hit paths)
stage.addEventListener('pointermove',e=>{
  syncCardSuppress();   // self-heal: a re-render can replace a focused .tx without blur
  cards.schedule(e,()=>resolveHoverTarget(e));
});
stage.addEventListener('pointerleave',()=>{
  cards.hide(); clearEdgeEmphasis();
});
// keyboard: focusin on a tabbable node shows L1 immediately
cards.attachFocus($('world'),()=>{
  const ae=document.activeElement;
  const nodeEl=ae&&ae.closest?ae.closest('.node'):null;
  if(!nodeEl) return null;
  clearEdgeEmphasis();
  return nodeCardPayload(nodeById(nodeEl.dataset.id));
});

// note editing -> suppress cards while focused; commit text on blur
const isEditTx=t=>t&&t.classList&&(t.classList.contains('tx')||t.classList.contains('shape-tx')||t.classList.contains('frame-tx'))&&t.isContentEditable;
stage.addEventListener('focus',e=>{
  if(isEditTx(e.target)) syncCardSuppress();   // editingNodeText() reads activeElement — now this target
},true);
stage.addEventListener('blur',e=>{
  const t=e.target;
  if(isEditTx(t)){
    const nodeEl=t.closest('.node'); const n=nodeEl&&nodeById(nodeEl.dataset.id);
    if(n){ n.text=t.textContent.trim(); }  // blur commits text without a re-render
    if(t.classList.contains('shape-tx')){ t.removeAttribute('contenteditable'); t.classList.remove('editing'); }
    syncCardSuppress();  // activeElement has moved on — unsuppresses on every edit-end path
  }
},true);
// double-click a shape/frame to edit its label (normally click-through for dragging)
stage.addEventListener('dblclick',e=>{
  const tx=e.target.closest&&e.target.closest('.node.shape .shape-tx');
  if(tx){ tx.setAttribute('contenteditable','true'); tx.classList.add('editing'); tx.focus();
    document.getSelection().selectAllChildren(tx); return; }
  const ftx=e.target.closest&&e.target.closest('.node.frame .frame-tx');
  if(ftx){ ftx.focus(); document.getSelection().selectAllChildren(ftx); return; }
  // double-click a reshaped line to straighten it back to its default route
  const eid=edgeIdAt(e);
  if(eid){ const ed=edges.find(x=>x.id===eid);
    if(ed&&ed.wp){ pushUndo(); delete ed.wp; drawEdges(); toast('line straightened'); } }
});

/* ---------- toolbar ---------- */
$('ideaIn').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&e.target.value.trim()){
    const text=e.target.value.trim(); e.target.value='';
    const c=viewCenterWorld();
    pushUndo();
    addNode(text, c.x+(Math.random()*80-40), c.y+(Math.random()*60-30),'idea');
  }
});
$('tSelect').onclick=()=>setTool('select');
$('tText').onclick=()=>setTool(tool==='text'?'select':'text');
$('tNote').onclick=()=>setTool(tool==='note'?'select':'note');
$('tShape').onclick=()=>{ currentShape=null;   // basic shape tool: click-place, cycle rect/ellipse/diamond/rounded
  if(tool==='shape'){shapeIx++; toast('Shape: '+SHAPES[shapeIx%SHAPES.length]); setTool('shape');}
  else setTool('shape'); };
$('tLink').onclick=()=>setTool(tool==='link'?'select':'link');
$('tShapes').onclick=()=>toggleShapePalette();
$('tFlow').onclick=()=>setTool(tool==='flow'?'select':'flow');
$('tRag').onclick=(e)=>openRagMenu(e.currentTarget);
$('tColor').onclick=(e)=>openColorPop(e.currentTarget);
$('tBg').onclick=openBgPanel;
$('tDup').onclick=duplicateSelection;
$('tAlign').onclick=alignSelection;
$('tGroup').onclick=toggleGroup;
$('tUndo').onclick=undo;
$('tRedo').onclick=redo;
$('tDel').onclick=deleteSelected;
$('tZoomIn').onclick=()=>zoomBy(1.15);
$('tZoomOut').onclick=()=>zoomBy(0.87);
$('tFit').onclick=fit;
$('tCore').onclick=()=>openSavedMap(STARTER_BOARD);
$('zLabel').onclick=resetZoom;
function zoomBy(f){
  _cardZooming=true; syncCardSuppress();
  const st=stage.getBoundingClientRect();
  const mx=st.width/2,my=st.height/2;const wx=(mx-cam.x)/cam.scale,wy=(my-cam.y)/cam.scale;
  cam.scale=Math.max(0.15,Math.min(3,cam.scale*f));cam.x=mx-wx*cam.scale;cam.y=my-wy*cam.scale;applyCam();
  _cardZooming=false; syncCardSuppress();
}
function fit(){
  if(!nodes.length){cam={x:0,y:0,scale:1};applyCam();return;}
  const xs=nodes.map(n=>n.x),ys=nodes.map(n=>n.y);
  const minX=Math.min(...xs)-100,maxX=Math.max(...xs)+100;
  const minY=Math.min(...ys)-100,maxY=Math.max(...ys)+100;
  const st=stage.getBoundingClientRect();
  const sc=Math.max(0.2,Math.min(1.4,Math.min(st.width/(maxX-minX),st.height/(maxY-minY))));
  cam.scale=sc; cam.x=st.width/2-(minX+maxX)/2*sc; cam.y=st.height/2-(minY+maxY)/2*sc; applyCam();
}

/** Merge a loaded board's background config over the defaults (missing keys keep
    their default). Called from every board-load path. */
function loadBg(){
  boardBg=Object.assign({},BG_DEFAULT);
  try{ const raw=localStorage.getItem(bgKey()); const b=raw&&JSON.parse(raw);
    if(b&&typeof b==='object'&&!Array.isArray(b)) boardBg=Object.assign(boardBg,b); }catch(_){}
  applyCanvasTint();
}
// The canvas background is a per-viewer setting kept in this browser only; the board
// log holds nodes and links, nothing else.
function bgKey(){ return 'omli.mm.bg.'+(mapId||'new'); }
function saveBg(){ try{ localStorage.setItem(bgKey(),JSON.stringify(boardBg)); }catch(_){} }

$('btnSave').onclick=()=>saveBoard(false);
$('btnLoad').onclick=async()=>{
  const q=prompt('Search boards (id or text). Leave empty to list all.','');
  if(q===null) return;
  let j={};
  try{ const r=await fetch(API+'/api/mindmap/list?q='+encodeURIComponent(q.trim())); j=await jsonOf(r); }catch(_){}
  const maps=j.boards||[]; if(!maps.length){toast(q.trim()?'No boards match that search':'No saved boards');return;}
  const pick=prompt('Load which board?\n'+maps.map(m=>`${m.board}  (${m.nodes} nodes)`).join('\n'),maps[maps.length-1].board);
  if(!pick)return;
  await openSavedMap(pick.trim());
};
$('nbClose').onclick=closeNodebar;
$('thinkClose').onclick=thinkClose;
$('btnHome').onclick=()=>{location.href='/';};

// keyboard: declarative command registry (mm-keymap.js) — palette on Ctrl+K,
// cheat sheet on ?, all bindings live in one registered command list.
// Registration happens further down this script; escapeAll keeps the
// Escape semantics in one place.
function escapeAll(){
  flowFrom=null; linkFrom=null; hideShapePalette();
  $('inspector').style.display='none'; $('ragmenu').style.display='none';
  $('colorpop').style.display='none'; $('framepop').style.display='none'; $('runOut').style.display='none';
  $('rel').style.display='none'; selEdgeId=null; clearSnapDots();
  closeNodebar(); thinkClose();
  if(multiSel.size){clearMulti();}
  if(tool!=='select') setTool('select');
  render();
  clearEdgeEmphasis(); cards.hide(); syncCardSuppress();
}

/* ---------- selection helpers ---------- */
function selectedIds(){ return multiSel.size?[...multiSel]:(selId?[selId]:[]); }
function selectedNodes(){ return selectedIds().map(nodeById).filter(Boolean); }

/* ---------- RAG rung palette ---------- */
function openRagMenu(anchorEl){
  const m=$('ragmenu'); const r=anchorEl.getBoundingClientRect();
  if(m.style.display==='block'){m.style.display='none';return;}
  let html='<h4>Add a RAG rung</h4>';
  RUNG_ORDER.forEach(k=>{
    html+=`<div class="ritem" data-k="${k}"><span class="rd" style="background:${RUNG[k].color}"></span>${RUNG[k].label}</div>`;
  });
  m.innerHTML=html;
  m.style.display='block';
  m.style.left=Math.max(8,r.left-70)+'px';
  m.style.top=(r.top-8-m.offsetHeight)+'px';
  m.style.top=(r.top-8-Math.min(300,m.scrollHeight))+'px';
  m.querySelectorAll('.ritem').forEach(it=>it.onclick=()=>{
    m.style.display='none'; addRung(it.dataset.k); syncCardSuppress();
  });
  syncCardSuppress();
}

/* ---------- color / style popover ---------- */
const PALETTE=['#141f2e','#3a3414','#16283a','#233a2a','#3a1f2e','#2a2340','#0e1622','#46b1ff','#46d39a','#ffb454','#ff6b6b','#c792ea'];
// Apply a node's chosen text colour to its label element(s). Called from render() after the
// node's innerHTML is built; persisted per-node in meta.textColor (like meta.fill/stroke).
function applyTextColor(el,n){
  if(n.meta&&n.meta.textColor) el.querySelectorAll('.tx,.shape-tx').forEach(t=>t.style.color=n.meta.textColor);
}
/* ---------- checkpoint history: recover a board from the append-only chain ----------
   Every save appends operations to the board's log, and a checkpoint also records the
   board as an artifact chain. A restore appends a restore operation; nothing earlier is
   rewritten, so the points the user rolls away from stay listed. */
const histEl=$('hist');
let _histAt=null;                     // head of the checkpoint the board was last restored from
function hideHist(){ histEl.style.display='none'; }
function _histWhen(iso){
  const t=Date.parse(iso||''); if(!t) return String(iso||'—');
  const mins=Math.round((Date.now()-t)/60000);
  if(mins<1) return 'just now';
  if(mins<60) return mins+'m ago';
  if(mins<1440) return Math.round(mins/60)+'h ago';
  return new Date(t).toLocaleString();
}
async function openHist(){
  histEl.style.left=Math.max(8,window.innerWidth-320)+'px';
  histEl.style.top='64px';
  histEl.style.display='block';
  const head='<h4>Checkpoints</h4>';
  if(!mapId){ histEl.innerHTML=head+'<div class="hempty">Save the board first.</div>'; return; }
  histEl.innerHTML=head+'<div class="hempty">loading…</div>';
  let chain=[];
  try{
    const r=await fetch(API+'/api/mindmap/boards/'+encodeURIComponent(mapId)+'/checkpoints');
    const j=await jsonOf(r);
    if(!r.ok) throw new Error(j.error||r.status);
    chain=j.checkpoints||[];
  }catch(e){
    histEl.innerHTML=head+'<div class="hempty">could not read the checkpoints: '+escapeHtml(String(e.message||e))+'</div>';
    return;
  }
  const note='A checkpoint records the board as it stands. Restoring appends a new '
    +'operation; nothing earlier is rewritten, so every checkpoint stays listed.';
  const make='<div class="hmake"><input id="hLabel" placeholder="Checkpoint label" maxlength="200">'
    +'<button class="hrest" id="hMake">Checkpoint</button></div>';
  const rows=chain.slice().reverse().map(c=>{
    const cur=c.head&&c.head===_histAt;
    return `<div class="hitem${cur?' now':''}">`
      +`<span class="hwhen">${escapeHtml(_histWhen(c.created_at))}`
      +`<div class="hmeta">${escapeHtml(c.label||'')}</div></span>`
      +`<button class="hrest" data-to="${escapeHtml(c.head||'')}">${cur?'restored':'Restore'}</button></div>`;
  }).join('');
  histEl.innerHTML=head+'<p class="hnote">'+note+'</p>'+make
    +(rows||'<div class="hempty">No checkpoints yet.</div>');
  histEl.querySelectorAll('.hrest[data-to]').forEach(b=>b.onclick=()=>restoreCheckpoint(b.dataset.to));
  $('hMake').onclick=makeCheckpoint;
}
async function makeCheckpoint(){
  const label=($('hLabel').value||'').trim()||('checkpoint '+new Date().toLocaleString());
  if(!(await saveBoard(true))) return;
  try{
    const r=await fetch(API+'/api/mindmap/boards/'+encodeURIComponent(mapId)+'/checkpoints',
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label})});
    const j=await jsonOf(r);
    if(!r.ok){ toast('Checkpoint failed: '+(j.error||r.status)); return; }
    toast(j.created?'Checkpoint recorded':'No change since the last checkpoint');
  }catch(_){ toast('Checkpoint failed'); }
  openHist();
}
async function restoreCheckpoint(to){
  if(!to||!mapId) return;
  try{
    const r=await fetch(API+'/api/mindmap/boards/'+encodeURIComponent(mapId)+'/restore',
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({to,base:boardHead||null})});
    const j=await jsonOf(r);
    if(!r.ok){ toast('Restore failed: '+(j.error||r.status)); return; }
    const b=fromServer(j);
    nodes=b.nodes; edges=b.edges; selId=null; boardHead=j.head||boardHead;
    _histAt=to;
    render(); fit();
    toast('Restored: a restore operation was appended; earlier operations are unchanged');
    loadAttribution();
    openHist();
  }catch(e){ toast('Restore request failed'); }
}
$('tHist').onclick=(e)=>{ e.stopPropagation();
  histEl.style.display==='block' ? hideHist() : openHist(); };

/* ---------- label fit: scale text down, never clip it ----------
   A node's label is how the user identifies it, so a long one must shrink to fit
   rather than get cut off by .tx's max-height. Split in two so the policy is
   testable without real layout (jsdom reports no heights): fitTextSize decides
   from injected measurements, fitNodeText is the thin DOM adapter. */
const TX_MIN_FS=7, TX_STEP=.5;
// Largest size in [min, base] whose measured height fits boxH. Falls back to the
// floor with `unclip` set — at that point the box grows instead of truncating.
function fitTextSize(measureAt,boxH,{base=11,min=TX_MIN_FS,step=TX_STEP}={}){
  if(!(boxH>0)) return {fontSize:null,unclip:false};      // unmeasurable: leave as authored
  if(measureAt(base)<=boxH) return {fontSize:null,unclip:false};
  for(let fs=base-step; fs>=min; fs-=step){
    if(measureAt(fs)<=boxH) return {fontSize:fs,unclip:false};
  }
  return {fontSize:min,unclip:true};
}
function fitNodeText(tx){
  if(!tx) return null;
  tx.style.fontSize=''; tx.style.maxHeight='';            // back to the authored CSS
  const base=parseFloat(getComputedStyle(tx).fontSize)||11;
  const measureAt=fs=>{ tx.style.fontSize=(fs===base?'':fs+'px'); return tx.scrollHeight; };
  const fit=fitTextSize(measureAt,tx.clientHeight,{base});
  tx.style.fontSize=fit.fontSize==null?'':fit.fontSize+'px';
  if(fit.unclip) tx.style.maxHeight='none';
  return fit;
}
// One pass per render — nodes are rebuilt each time, so the fit must be re-applied.
function fitAllNodeText(){ world.querySelectorAll('.node .tx').forEach(fitNodeText); }
function openColorPop(anchorEl){
  const sel=selectedNodes();
  if(!sel.length){toast('Select node(s) first');return;}
  const p=$('colorpop'); const r=anchorEl.getBoundingClientRect();
  // Text-color swatches: the readable inks (bright end of the palette) plus black/white,
  // so node label text can be recoloured alongside fill/border.
  const TEXT_SWATCHES=['#e8f0fb','#0a0e14'].concat(PALETTE.slice(7));
  // Shape-tint palette (color + line-pattern combos) — only when shape nodes are
  // selected. Each swatch is a live mini-preview drawn by the shape renderer.
  const hasShape=sel.some(n=>n.kind==='shape');
  // Fill controls read their active state from the first selected shape.
  const sh0=sel.find(n=>n.kind==='shape'), sm=(sh0&&sh0.meta)||{};
  const fm=sm.fillMode||'tint', curPat=sm.pattern||'dots';
  const fillOp=(sm.fillOpacity!=null)?sm.fillOpacity:(fm==='pattern'?0.9:0.15);
  const FILL_MODES=[
    ['tint','Tint','Solid customizable colour wash'],
    ['pattern','Pattern','Fill with a repeating pattern (dots, grid, …)'],
    ['seethrough','See-thru','Reveal the board background — or the canvas tint when none'],
    ['neon','Neon','Glowing illuminated outline'],
  ];
  const shapeSection=hasShape
    ? '<h4>Shape style</h4><div class="row shapes">'
      +SHAPE_STYLES.map(st=>`<div class="sw ssw" data-style="${st.key}" title="Outline: ${st.label}">`
        +shapeSVG('diamond',26,20,st.key)+`</div>`).join('')
      +'</div>'
      +'<h4>Fill</h4><div class="seg">'
      +FILL_MODES.map(([k,l,t])=>`<button class="fmode ${fm===k?'on':''}" data-fmode="${k}" title="${t}">${l}</button>`).join('')
      +'</div>'
      +(fm==='pattern'?'<div class="pats">'
        +Object.keys(SHAPE_PATTERNS).map(k=>`<div class="pat ${curPat===k?'on':''}" data-pat="${k}" title="Pattern: ${SHAPE_PATTERNS[k].label}">${cpPatternPreview(k)}</div>`).join('')
        +'</div>':'')
      +((fm==='tint'||fm==='pattern'||fm==='neon')
        ? '<div class="row">'+PALETTE.slice(7).map(c=>`<div class="sw ${sm.fillColor===c?'on':''}" data-fill="${c}" style="background:${c}" title="Fill colour ${c}"></div>`).join('')+'</div>'
        : '')
      +((fm==='tint'||fm==='pattern')?`<input type="range" id="cpFillOp" min="0" max="1" step="0.05" value="${fillOp}" title="Fill opacity">`:'')
    : '';
  let html='<h4>Picker</h4><div class="row">'
    +`<input type="color" id="cpHexPick" value="#46b1ff" title="Colour picker">`
    +`<input type="text" id="cpHex" value="#46b1ff" spellcheck="false" style="width:78px;background:#0e1622;color:var(--ink);border:1px solid var(--line);border-radius:5px;padding:3px 5px;font-size:11px" title="Hex">`
    +'</div><h4>Fill</h4><div class="row">'
    +PALETTE.map(c=>`<div class="sw" data-t="fill" style="background:${c}" title="Fill ${c}"></div>`).join('')
    +'</div><h4>Border</h4><div class="row">'
    +PALETTE.slice(7).concat(['#2a3647','#8aa0bd']).map(c=>`<div class="sw" data-t="stroke" style="background:${c}" title="Border ${c}"></div>`).join('')
    +'</div><h4>Text</h4><div class="row">'
    +TEXT_SWATCHES.map(c=>`<div class="sw" data-t="textColor" style="background:${c}" title="Text ${c}"></div>`).join('')
    +'</div>'+shapeSection;
  p.innerHTML=html; p.style.display='block';
  p.style.left=Math.max(8,r.left-60)+'px';
  p.style.top=(r.top-8-Math.min(160,p.scrollHeight))+'px';
  p.querySelectorAll('.sw[data-t]').forEach(sw=>sw.onclick=()=>{
    pushUndo();
    sel.forEach(n=>{ n.meta=n.meta||{}; n.meta[sw.dataset.t]=sw.style.background; });
    render();
  });
  const applyHex=v=>{
    if(!/^#[0-9a-fA-F]{6}$/.test(v)) return;
    pushUndo();
    sel.forEach(n=>{ n.meta=n.meta||{}; n.meta.fill=v; });
    if(tool==='paint'||tool==='scratch'){ brush=inkFns.clampBrush({...(brush||{}),color:v}); inkFns.saveBrush(brush); }
    render();
  };
  const hp=$('cpHexPick'), hx=$('cpHex');
  if(hp) hp.oninput=()=>{ if(hx) hx.value=hp.value; applyHex(hp.value); };
  if(hx) hx.onchange=()=>applyHex(hx.value.trim());
  p.querySelectorAll('.ssw').forEach(sw=>sw.onclick=()=>{
    pushUndo();
    sel.filter(n=>n.kind==='shape').forEach(n=>{ n.meta=n.meta||{}; n.meta.shapeStyle=sw.dataset.style; });
    render();
  });
  // fill mode → re-open the popover so its mode-specific controls (pattern gallery,
  // color row, opacity) rebuild for the new mode.
  p.querySelectorAll('.fmode').forEach(b=>b.onclick=()=>{
    pushUndo();
    sel.filter(n=>n.kind==='shape').forEach(n=>{ n.meta=n.meta||{}; n.meta.fillMode=b.dataset.fmode; });
    render(); openColorPop(anchorEl);
  });
  p.querySelectorAll('.pat[data-pat]').forEach(x=>x.onclick=()=>{
    pushUndo();
    sel.filter(n=>n.kind==='shape').forEach(n=>{ n.meta=n.meta||{}; n.meta.pattern=x.dataset.pat; });
    render(); openColorPop(anchorEl);
  });
  // fill color stores the hex verbatim (not the resolved rgb()), so opacity works.
  p.querySelectorAll('.sw[data-fill]').forEach(sw=>sw.onclick=()=>{
    pushUndo();
    sel.filter(n=>n.kind==='shape').forEach(n=>{ n.meta=n.meta||{}; n.meta.fillColor=sw.dataset.fill; });
    render(); openColorPop(anchorEl);
  });
  const fo=$('cpFillOp'); if(fo) fo.oninput=()=>{
    sel.filter(n=>n.kind==='shape').forEach(n=>{ n.meta=n.meta||{}; n.meta.fillOpacity=+fo.value; });
    render();
  };
  syncCardSuppress();
}
// Tiled preview swatch for the fill-pattern gallery (bright ink, ignores opacity).
function cpPatternPreview(key){
  const p=SHAPE_PATTERNS[key], t=p.tile;
  return `<svg viewBox="0 0 ${t*2} ${t*2}" preserveAspectRatio="xMidYMid slice">`
    +`<defs><pattern id="cpp-${key}" patternUnits="userSpaceOnUse" width="${t}" height="${t}">`
    +`${p.mk('#8fb8ff',0.95)}</pattern></defs>`
    +`<rect width="${t*2}" height="${t*2}" fill="url(#cpp-${key})"/></svg>`;
}

/* ---------- frame properties popover (right-click a frame) ----------
   A frame carries properties that don't fit on its tiny inline label: a title,
   its order in the walkthrough sequence, and a custom fill/border. This
   popover edits them in place — opened by right-clicking a frame, or via the
   'Frames › Edit frame properties…' command. Colour is stored in meta.fill /
   meta.stroke, honoured by the frame branch of render(). */
function openFramePop(n,x,y){
  if(!n||n.kind!=='frame'){ toast('Select a frame first'); return; }
  const p=$('framepop');
  const fs=frames(), ix=fs.findIndex(f=>f.id===n.id), total=fs.length;
  const m=n.meta||{};
  const FILLS=PALETTE.slice(0,7);                                  // the dark fill washes
  const BORDERS=['#33507a'].concat(PALETTE.slice(7)).concat(['#8aa0bd']);   // default + inks
  const fillSw='<div class="sw def'+(m.fill?'':' on')+'" data-fill="" title="Default (no fill)">╱</div>'
    +FILLS.map(c=>`<div class="sw ${m.fill===c?'on':''}" data-fill="${c}" style="background:${c}" title="Fill ${c}"></div>`).join('');
  const borderSw=BORDERS.map(c=>`<div class="sw ${(m.stroke||'#33507a')===c?'on':''}" data-stroke="${c}" style="background:${c}" title="Border ${c}"></div>`).join('');
  p.innerHTML=`<span class="close" id="fpClose">✕</span>`
    +`<h4>Frame ${ix>=0?ix+1:''} of ${total} — properties</h4>`
    +`<input type="text" id="fpTitle" placeholder="Frame title" spellcheck="false">`
    +`<h4>Sequence order</h4>`
    +`<div class="seg"><button id="fpEarlier" ${ix<=0?'disabled':''} title="Move earlier in the walkthrough ([)">◀ Earlier</button>`
    +`<button id="fpLater" ${(ix<0||ix>=total-1)?'disabled':''} title="Move later in the walkthrough (])">Later ▶</button>`
    +`<button id="fpFit" title="Fit the view to this frame">Fit view</button></div>`
    +`<h4>Fill</h4><div class="row">${fillSw}</div>`
    +`<h4>Border</h4><div class="row">${borderSw}</div>`
    +`<div class="seg" style="margin-top:9px"><button id="fpReset" title="Clear custom colours">Reset style</button>`
    +`<button id="fpDelete" class="danger" title="Delete this frame">Delete</button></div>`;
  p.style.display='block';
  const ti=$('fpTitle'); ti.value=n.text||'';                     // set via property to avoid attr escaping
  // position at the cursor, clamped into view
  const pr=p.getBoundingClientRect();
  p.style.left=Math.max(8,Math.min(window.innerWidth-pr.width-8,x))+'px';
  p.style.top=Math.max(8,Math.min(window.innerHeight-pr.height-8,y))+'px';
  // title: one undo frame on the first edit, then live-rename
  let titleDirty=false;
  ti.oninput=()=>{ if(!titleDirty){ pushUndo(); titleDirty=true; } n.text=ti.value; render(); };
  $('fpClose').onclick=closeFramePop;
  $('fpFit').onclick=()=>{ selId=n.id; fitToFrame(n); render(); };
  $('fpEarlier').onclick=()=>reorderFrame(n,-1,x,y);
  $('fpLater').onclick=()=>reorderFrame(n,1,x,y);
  $('fpReset').onclick=()=>{ if(n.meta&&(n.meta.fill||n.meta.stroke||n.meta.textColor)){
    pushUndo(); delete n.meta.fill; delete n.meta.stroke; delete n.meta.textColor;
    render(); openFramePop(n,x,y); } };
  $('fpDelete').onclick=()=>{ closeFramePop(); deleteFrame(n.id); };
  p.querySelectorAll('.sw[data-fill]').forEach(sw=>sw.onclick=()=>{
    pushUndo(); n.meta=n.meta||{};
    if(sw.dataset.fill) n.meta.fill=sw.dataset.fill; else delete n.meta.fill;
    render(); openFramePop(n,x,y);
  });
  p.querySelectorAll('.sw[data-stroke]').forEach(sw=>sw.onclick=()=>{
    pushUndo(); n.meta=n.meta||{}; n.meta.stroke=sw.dataset.stroke;
    render(); openFramePop(n,x,y);
  });
  syncCardSuppress();
}
function closeFramePop(){ const p=$('framepop'); if(p) p.style.display='none'; syncCardSuppress(); }
// Swap a frame's order with its neighbour so the walkthrough sequence (the [ ] keys
// and the Next/fit cycle) can be reordered. Orders stay 1..n; frames() re-sorts.
function reorderFrame(n,dir,x,y){
  const fs=frames(), ix=fs.findIndex(f=>f.id===n.id), j=ix+dir;
  if(ix<0||j<0||j>=fs.length) return;
  pushUndo();
  const a=fs[ix], b=fs[j], ao=a.order; a.order=b.order; b.order=ao;
  frameCycleIx=-1;
  render(); buildBgPanel(); openFramePop(n,x,y);
}

/* ---------- duplicate / align ---------- */
function duplicateSelection(){
  const sel=selectedNodes(); if(!sel.length){toast('Nothing to duplicate');return;}
  pushUndo(); const idmap={}, clones=[];
  sel.forEach(n=>{ const c={...JSON.parse(JSON.stringify(n)),id:uid()}; c.x+=34;c.y+=34;
    idmap[n.id]=c.id; nodes.push(c); clones.push(c); });
  // carry internal edges between duplicated nodes
  edges.filter(e=>idmap[e.source]&&idmap[e.target]).forEach(e=>
    edges.push({id:uid('e'),source:idmap[e.source],target:idmap[e.target],relation:e.relation,label:e.label||''}));
  multiSel=new Set(clones.map(c=>c.id)); selId=clones.length===1?clones[0].id:null;
  render(); toast('Duplicated '+clones.length);
}
function alignSelection(){
  const sel=selectedNodes(); if(sel.length<2){toast('Select 2+ nodes to align into a ladder');return;}
  pushUndo();
  const avgX=sel.reduce((s,n)=>s+n.x,0)/sel.length;
  const ordered=[...sel].sort((a,b)=>a.y-b.y);
  const top=ordered[0].y;
  ordered.forEach((n,i)=>{ n.x=avgX; n.y=top+i*150; });
  render(); toast('Aligned into a column');
}
/* ---------- grouping: bind a selection so it drags as one (ungroup releases) ---------- */
function groupSelection(){
  const sel=selectedNodes().filter(n=>n.kind!=='frame');
  if(sel.length<2){toast('Select 2+ nodes to group (marquee-drag or Ctrl+click)');return;}
  pushUndo();
  const gid=uid('g'); sel.forEach(n=>{ n.group=gid; });
  multiSel=new Set(sel.map(n=>n.id)); selId=null;
  render(); toast('Grouped '+sel.length+' — they now move together');
}
function ungroupSelection(){
  const sel=selectedNodes();
  const gids=new Set(sel.map(n=>n.group).filter(Boolean));
  if(!gids.size){toast('Select a grouped node to ungroup');return;}
  pushUndo();
  nodes.forEach(n=>{ if(gids.has(n.group)) delete n.group; });
  render(); toast('Ungrouped');
}
// tGroup toolbar button: ungroup if the selection is already grouped, else group.
function toggleGroup(){
  if(selectedNodes().some(n=>n.group)) ungroupSelection(); else groupSelection();
}
// Nodes that should move together with n while dragging: its group peers, plus
// the rest of an active multi-selection when n is part of it.
function dragPeers(n){
  const ids=new Set();
  if(n.group) nodes.forEach(m=>{ if(m.group===n.group&&m.id!==n.id) ids.add(m.id); });
  if(multiSel.has(n.id)) multiSel.forEach(id=>{ if(id!==n.id) ids.add(id); });
  return [...ids].map(nodeById).filter(Boolean);
}

/* ---------- frames: ordered titled regions, cycled/fit for a walkthrough ---------- */
function frames(){ return nodes.filter(n=>n.kind==='frame').sort((a,b)=>(a.order||0)-(b.order||0)); }
function addFrame(x,y,w,h){
  const order=frames().reduce((m,n)=>Math.max(m,n.order||0),0)+1;
  const n={id:uid(),text:'Frame '+order,kind:'frame',x,y,w,h,order,icon:{}};
  nodes.push(n); render(); return n;
}
function fitToFrame(f){
  const w=f.w||600, h=f.h||400, pad=48;
  const minX=f.x-w/2-pad,maxX=f.x+w/2+pad,minY=f.y-h/2-pad,maxY=f.y+h/2+pad;
  const st=stage.getBoundingClientRect();
  const sc=Math.max(0.15,Math.min(3,Math.min(st.width/(maxX-minX),st.height/(maxY-minY))));
  cam.scale=sc; cam.x=st.width/2-(minX+maxX)/2*sc; cam.y=st.height/2-(minY+maxY)/2*sc; applyCam();
}
let frameCycleIx=-1;
function cycleFrame(dir){
  const fs=frames(); if(!fs.length){toast('No frames yet — draw one with the Frames tool');return;}
  frameCycleIx=((frameCycleIx+(dir||1))%fs.length+fs.length)%fs.length;
  const f=fs[frameCycleIx]; selId=f.id; fitToFrame(f); render();
  toast('Frame '+(frameCycleIx+1)+'/'+fs.length+(f.text?' — '+f.text:''));
}

/* ---------- marquee: select every node whose centre falls in the dragged rect ---------- */
function selectInRect(minX,minY,maxX,maxY,add){
  if(!add) multiSel.clear();
  nodes.forEach(n=>{ if(n._hidden) return;
    if(n.x>=minX&&n.x<=maxX&&n.y>=minY&&n.y<=maxY) multiSel.add(n.id); });
  selId=null; render(); syncCardSuppress();
  if(multiSel.size) toast(multiSel.size+' selected'); else toast('Nothing in selection');
}

/* ---------- rung inspector ---------- */
async function fetchCatalog(){
  if(catalog) return catalog;
  try{const r=await fetch(API+'/api/mindmap/rag/catalog'); const j=await jsonOf(r);
    catalog=r.ok?{sources:j.sources||[],agents:j.agents||[]}:{sources:[],agents:[]};
  }catch(_){ catalog={sources:[],agents:[]}; }
  return catalog;
}
async function openInspector(n){
  const box=$('inspector');
  const cat=await fetchCatalog();
  if(nodeById(n.id)!==n) return;              // selection changed while awaiting
  const k=n.kind, meta=n.meta=n.meta||{};
  let body=`<span class="close" id="inspClose">×</span>`
    +`<h3><span class="kdot" style="background:${RUNG[k].color}"></span>${RUNG[k].label}</h3>`;
  if(k==='source'){
    const cur=(meta.catalog_ref&&meta.catalog_ref.name)||'';
    body+=`<label>Folder</label><select id="iSrc"><option value="">— pick a project folder —</option>`
      +cat.sources.map(s=>`<option value="${escapeHtml(s.id)}" ${s.name===cur?'selected':''}>${escapeHtml(s.name)}</option>`).join('')
      +`</select><div class="hintline">${cat.sources.length?'Retrieval reads only folders on your project list.':'Add a folder on the home page first.'}</div>`;
  }else if(k==='agent'){
    const cur=(meta.catalog_ref&&meta.catalog_ref.model_id)||'';
    body+=`<label>Model</label><select id="iAgent"><option value="">— pick a model —</option>`
      +cat.agents.map(a=>`<option value="${escapeHtml(a.id)}" ${a.id===cur?'selected':''}>${escapeHtml(a.name||a.id)}</option>`).join('')
      +`</select><div class="hintline">${cat.agents.length?'Models come from your model registry.':'No model registry yet.'}</div>`;
  }else if(k==='retriever'){
    const p=meta.params=meta.params||{k:5,query:''};
    body+=`<label>Top-k</label><input id="iK" type="number" min="1" max="20" value="${p.k??5}"/>`
      +`<label>Query override (optional)</label><input id="iQ" value="${escapeHtml(p.query||'')}" placeholder="defaults to the Run query"/>`;
  }else if(k==='prompt'){
    const p=meta.params=meta.params||{template:''};
    body+=`<label>Prompt template</label><textarea id="iT" placeholder="Instruction for the agent…">${escapeHtml(p.template||'')}</textarea>`;
  }else{
    body+=`<label>Label</label><input id="iLabel" value="${escapeHtml(n.text||'')}"/>`
      +`<div class="hintline">${k==='context-group'?'Merges upstream sources.':k==='reranker'?'Re-scores upstream snippets (deterministic).':'Terminal artifact of the ladder.'}</div>`;
  }
  box.innerHTML=body; box.style.display='block';
  $('inspClose').onclick=()=>{box.style.display='none'; syncCardSuppress();};
  syncCardSuppress();
  const commit=()=>{ pushUndo(); render(); };
  const iSrc=$('iSrc'); if(iSrc) iSrc.onchange=()=>{ const s=cat.sources.find(x=>x.id===iSrc.value);
    n.meta.catalog_ref=s?{name:s.name,path:s.path}:null; if(s)n.text=s.name; commit(); };
  const iAgent=$('iAgent'); if(iAgent) iAgent.onchange=()=>{ const a=cat.agents.find(x=>x.id===iAgent.value);
    n.meta.catalog_ref=a?{model_id:a.id}:null; commit(); };
  const iK=$('iK'); if(iK) iK.onchange=()=>{ n.meta.params=n.meta.params||{}; n.meta.params.k=Math.max(1,+iK.value||5); commit(); };
  const iQ=$('iQ'); if(iQ) iQ.onchange=()=>{ n.meta.params=n.meta.params||{}; n.meta.params.query=iQ.value; commit(); };
  const iT=$('iT'); if(iT) iT.onchange=()=>{ n.meta.params=n.meta.params||{}; n.meta.params.template=iT.value; commit(); };
  const iLabel=$('iLabel'); if(iLabel) iLabel.onchange=()=>{ n.text=iLabel.value; commit(); };
}

/* ---------- validate + run the RAG ladder ---------- */
$('btnValidate').onclick=async()=>{
  nodes.forEach(n=>{delete n._invalid;});
  let r, j;
  try{
    r=await fetch(API+'/api/mindmap/rag/validate',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(toServer())});
    j=await jsonOf(r);
  }catch(e){ toast('Validate error'); return; }
  if(!r.ok){ toast('Validate failed: '+(j.error||r.status)); return; }
  nodes.forEach(n=>{ if(n.kind==='source'&&!(n.meta&&n.meta.catalog_ref&&n.meta.catalog_ref.path)) n._invalid=true;
    if(n.kind==='agent'&&!(n.meta&&n.meta.catalog_ref&&n.meta.catalog_ref.model_id)) n._invalid=true; });
  render();
  if(j.ok) toast('Ladder is valid ✓ — '+(j.order?j.order.length:0)+' rungs'+(j.warnings&&j.warnings.length?(' · '+j.warnings.length+' warning(s)'):''));
  else toast('✕ '+(j.errors||['invalid']).join(' · '));
};
$('btnRun').onclick=async()=>{
  if(!modelStatus.configured){ toast('No model configured'); return; }
  const q=$('runQ').value.trim();
  if(!q){ $('runQ').focus(); toast('Enter a query to run'); return; }
  const rungs=nodes.filter(n=>isRung(n.kind));
  if(!rungs.length){ toast('Add ladder rungs first (▤ toolbar button)'); return; }
  rungs.forEach(n=>{n._running=true;}); render();
  const j=await modelCall('/api/mindmap/rag/run',{...toServer(),query:q});
  rungs.forEach(n=>delete n._running);
  render();
  if(!j.ok){ toast('✕ '+j.error); return; }
  showRunDrawer(j,q);
};
function showRunDrawer(j,q){
  const box=$('runOut'); box.style.display='flex';
  $('runSrc').textContent=(j.hits||[]).length+' snippet(s) from your folders';
  const hits=(j.hits||[]).map(h=>`<div><span style="color:#46d39a">●</span> <b>${escapeHtml(h.file||'')}</b>`
    +`<div class="src">${escapeHtml(String(h.snippet||'').slice(0,200))}</div></div>`).join('');
  $('runBody').innerHTML=`<div style="margin-bottom:8px"><b>Query:</b> ${escapeHtml(q)}</div>`
    +(hits||'<div class="src">No snippet matched.</div>')
    +`<hr style="border:none;border-top:1px solid var(--line);margin:9px 0"/>`
    +`<div><b>Answer</b></div><div style="margin-top:4px;white-space:pre-wrap">${escapeHtml(j.text||'(no answer)')}</div>`;
}
$('runClose').onclick=()=>$('runOut').style.display='none';

// close popovers/menus on canvas click
stage.addEventListener('mousedown',()=>{ $('ragmenu').style.display='none';
  $('colorpop').style.display='none'; $('framepop').style.display='none'; syncCardSuppress(); },true);

let dsState={answers:{},q:null,done:false};
function openDesigner(){
  dsState={answers:{},q:null,done:false};
  $('designer').style.display='flex';
  $('ivBody').innerHTML='<div class="ivq">A few quick questions, then a starter board built from your '
    +'answers by a template. This runs offline: no model is called.</div>';
  const inp=$('ivIn'); inp.value=''; inp.placeholder='Type your answer…';
  ivNext();
}
function ivBubble(cls,html){ const d=document.createElement('div'); d.className=cls; d.innerHTML=html;
  $('ivBody').appendChild(d); $('ivBody').scrollTop=$('ivBody').scrollHeight; return d; }
async function ivSend(){
  const v=$('ivIn').value.trim(); if(!v||dsState.done||!dsState.q) return; $('ivIn').value='';
  ivBubble('iva',escapeHtml(v));
  dsState.answers[dsState.q.id]=v;
  return ivNext();
}
async function ivNext(){
  const note=ivBubble('ivnote','…');
  try{
    const r=await fetch(API+'/api/mindmap/autodesign/question',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:dsState.answers})});
    const j=await jsonOf(r); note.remove();
    if(!r.ok){ ivBubble('ivnote','The question set could not be read'); return; }
    if(j.done) return ivBuild();
    dsState.q=j.question;
    ivBubble('ivq',escapeHtml(j.question.text));
    $('ivIn').focus();
  }catch(e){ note.textContent='the question set could not be read'; }
}
async function ivBuild(){
  const note=ivBubble('ivnote','Building your board…');
  try{
    const r=await fetch(API+'/api/mindmap/autodesign/build',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:dsState.answers})});
    const j=await jsonOf(r); note.remove();
    if(!r.ok){ ivBubble('ivnote','Build failed: '+escapeHtml(j.error||r.status)); return; }
    mergeBoard(fromServer(j.board));
    dsState.done=true;
    ivBubble('ivnote','Built a starter board from a template. Save it to keep it.');
    toast('Starter board added');
    setTimeout(()=>{$('designer').style.display='none';},3000);
  }catch(e){ note.textContent='build failed'; }
}
function mergeBoard(board){
  if(!board||!board.nodes||!board.nodes.length) return;
  pushUndo();
  const c=viewCenterWorld();
  const xs=board.nodes.map(n=>n.x||0), ys=board.nodes.map(n=>n.y||0);
  const cx=(Math.min(...xs)+Math.max(...xs))/2, cy=(Math.min(...ys)+Math.max(...ys))/2;
  const idmap={};
  board.nodes.forEach(n=>{ const nid=uid(); idmap[n.id]=nid;
    const nn={id:nid,text:n.text||'',x:c.x+((n.x||0)-cx),y:c.y+((n.y||0)-cy),
      kind:n.kind||'idea',glyph:'',icon:null,meta:n.meta||{}};
    nodes.push(nn);
    if(!isRung(nn.kind) && nn.kind!=='shape' && nn.kind!=='text') fetchIcon(nn);
  });
  (board.edges||[]).forEach(e=>{ if(idmap[e.source]&&idmap[e.target])
    edges.push({id:uid('e'),source:idmap[e.source],target:idmap[e.target],
      relation:e.relation||'association',label:e.label||''}); });
  render(); fit();
}
$('btnDesign').onclick=openDesigner;
$('ivClose').onclick=()=>$('designer').style.display='none';
$('ivSend').onclick=ivSend;
$('ivIn').addEventListener('keydown',e=>{ if(e.key==='Enter'){e.preventDefault(); ivSend();} });

const HINT_MAP='Type an idea → Enter · drop or paste a file to sort it · click a node for directions · <b>Ctrl+K</b> commands · <b>?</b> shortcuts';
function setHint(){ $('hint').innerHTML=HINT_MAP; }

/* ---------- command registry: every shortcut + the Ctrl+K palette ---------- */
const commandCenter=initCommandCenter();
const selNode=()=>selId&&nodeById(selId);
commandCenter.register([
  // tools
  {id:'tool-select',group:'Tools',label:'Select / move tool',keys:['v'],run:()=>setTool('select')},
  {id:'tool-text',group:'Tools',label:'Text tool',keys:['t'],run:()=>setTool('text')},
  {id:'tool-note',group:'Tools',label:'Sticky note tool',keys:['n'],run:()=>setTool('note')},
  {id:'tool-shape',group:'Tools',label:'Shape tool / shape palette',keys:['s'],
    run:()=>{tool==='shape'?toggleShapePalette():setTool('shape');}},
  {id:'tool-link',group:'Tools',label:'Connect ideas (typed relation)',keys:['l'],run:()=>setTool('link')},
  {id:'tool-flow',group:'Tools',label:'Flow arrow (RAG dataflow)',keys:['f'],run:()=>setTool('flow')},
  // edit
  {id:'undo',group:'Edit',label:'Undo',keys:['ctrl+z'],run:undo},
  {id:'redo',group:'Edit',label:'Redo',keys:['ctrl+shift+z','ctrl+y'],run:redo},
  {id:'duplicate',group:'Edit',label:'Duplicate selection',keys:['ctrl+d'],run:duplicateSelection},
  {id:'delete',group:'Edit',label:'Delete selection',keys:['delete','backspace'],
    when:()=>!!(selId||multiSel.size||selEdgeId),run:deleteSelected},
  {id:'escape',group:'Edit',label:'Deselect / close panels',keys:['escape'],run:escapeAll},
  {id:'align',group:'Edit',label:'Align selection into a column',run:alignSelection},
  {id:'group',group:'Edit',label:'Group selection (move together)',keys:['ctrl+g'],run:groupSelection},
  {id:'ungroup',group:'Edit',label:'Ungroup selection',keys:['ctrl+shift+g'],run:ungroupSelection},
  // frames
  {id:'frame-draw',group:'Frames',label:'Draw a frame region',run:()=>setTool('frame')},
  {id:'frame-next',group:'Frames',label:'Next frame (fit view)',keys:[']'],run:()=>cycleFrame(1)},
  {id:'frame-prev',group:'Frames',label:'Previous frame (fit view)',keys:['['],run:()=>cycleFrame(-1)},
  {id:'frame-props',group:'Frames',label:'Edit frame properties…',
    when:()=>{const n=selNode(); return !!(n&&n.kind==='frame');},
    run:()=>{const n=selNode(); if(n) openFramePop(n, Math.max(8,window.innerWidth/2-119), 130);}},
  // view
  {id:'zoom-in',group:'View',label:'Zoom in',keys:['='],run:()=>zoomBy(1.15)},
  {id:'zoom-out',group:'View',label:'Zoom out',keys:['-'],run:()=>zoomBy(0.87)},
  {id:'zoom-reset',group:'View',label:'Reset zoom to 100%',keys:['0'],run:resetZoom},
  {id:'fit',group:'View',label:'Fit board to view',keys:['.'],run:fit},
  {id:'sketch',group:'View',label:'Toggle sketch (hand-drawn) style',
    run:()=>{setSketch(!isSketch()); render();
      toast(isSketch()?'Sketch style on':'Sketch style off');}},
  {id:'minimap',group:'View',label:'Toggle minimap',keys:['m'],
    run:()=>{if(minimap)toast(minimap.toggle()?'Minimap on':'Minimap off');}},
  // board
  {id:'save',group:'Board',label:'Save board',keys:['ctrl+s'],global:true,run:()=>$('btnSave').click()},
  {id:'load',group:'Board',label:'Load a saved board…',run:()=>$('btnLoad').click()},
  {id:'starter',group:'Board',label:'Open the starter board',run:()=>openSavedMap(STARTER_BOARD)},
  {id:'design',group:'Board',label:'Design a board from an idea…',run:()=>$('btnDesign').click()},
  // meta
  {id:'palette',group:'Help',label:'Command palette',keys:['ctrl+k'],global:true,
    run:()=>commandCenter.openPalette()},
  {id:'cheat',group:'Help',label:'Keyboard shortcuts',keys:['?'],run:()=>commandCenter.openCheat()},
]);
// Right-click anywhere on the canvas → the full tools & settings menu (registry-
// driven, hover-expanding). Right-clicking a node selects it first so the Edit
// commands (delete/duplicate/colour) act on it.
stage.addEventListener('contextmenu',e=>{
  e.preventDefault();
  const nodeEl=e.target.closest('.node');
  if(nodeEl){ const id=nodeEl.dataset.id; if(id && !multiSel.has(id)){ selId=id; render(); } }
  // Right-clicking a single frame opens its properties editor (title/order/colour/
  // delete) directly; everything else opens the full tools & settings menu. The
  // 'Frames › Edit frame properties…' command reaches the same popover. When a
  // multi-selection is active the frame keeps the generic menu (so group/duplicate/
  // delete still apply to the selection) but a pinned "Edit frame properties…"
  // entry bound to the frame under the cursor is always offered.
  const n=nodeEl&&nodeById(nodeEl.dataset.id);
  if(n&&n.kind==='frame'&&!multiSel.size){ openFramePop(n,e.clientX,e.clientY); return; }
  const extras=(n&&n.kind==='frame')
    ?[{icon:'▭',label:'Edit frame properties…',run:()=>openFramePop(n,e.clientX,e.clientY)}]
    :null;
  commandCenter.openMenu(e.clientX,e.clientY,extras);
});
// NOTE: applyCam()/setTool()/setHint() run near the bottom of this
// script, once the minimap and the ink layer exist.

// identity chip for the signed-in key
loadMe();

/* ====================== architecture / flowchart shapes ====================== */
// Palette of architecture/flowchart shapes (objects). Distinct from the basic
// `SHAPES` string list (rect/ellipse/diamond/rounded) that drives the tShape
// cycle tool — both feed the single shapeSVG() renderer.
/* SHAPE_PALETTE + shapeSVG live in /mindmap/js/mm-shapes.js */
function addShape(shape,x,y,w,h){
  const n={id:uid(),text:'',x,y,kind:'shape',shape,w,h,glyph:'',icon:{}};
  nodes.push(n); render(); return n;
}
function buildPalette(){
  const flow=$('shGridFlow'), arch=$('shGridArch'); if(!flow||flow._built)return; flow._built=true;
  SHAPE_PALETTE.forEach(s=>{ const b=document.createElement('div'); b.className='sh'; b.title=s.label;
    b.innerHTML=shapeSVG(s.key,30,22)+`<span>${s.label}</span>`;
    b.onclick=()=>pickShape(s); (s.group==='flow'?flow:arch).appendChild(b); });
}
function pickShape(s){ currentShape=s; setTool('shape'); hideShapePalette();
  toast('Drag on the canvas to size: '+s.label); }
function toggleShapePalette(){ const p=$('shapePalette');
  if(p.style.display==='flex'){ hideShapePalette(); return; }
  buildPalette(); p.style.display='flex';
  const r=$('tShapes').getBoundingClientRect(), pr=p.getBoundingClientRect();
  p.style.left=Math.max(8,Math.min(window.innerWidth-pr.width-8, r.left+r.width/2-pr.width/2))+'px';
  p.style.top=Math.max(8,(r.top-pr.height-10))+'px';
}
function hideShapePalette(){ const p=$('shapePalette'); if(p) p.style.display='none'; }

/* ============= board background graphic (revealed by see-through shapes) =======
   The graphic lives in WORLD coordinates but is painted only through the
   silhouettes of shapes whose fill mode is 'seethrough': each such shape fills
   itself with an SVG <pattern> whose origin is offset by the shape's world
   top-left, so every shape samples the same world-anchored graphic (continuous
   across overlaps, zoom-correct via the world transform). Everywhere else reads
   the adjustable canvas tint. Local-first inline SVG — no external library.
   (Decorative fill patterns are per-shape: mm-shapes.js SHAPE_PATTERNS.) */
function bgActive(){
  return !!(boardBg&&boardBg.on&&(boardBg.type!=='image'||boardBg.image));
}
/* Per-shape fill descriptor read from meta, handed to shapeSVG. Default mode is
   'tint' (a solid customizable wash); 'seethrough' opts a shape into revealing
   the board graphic / canvas tint. `uid` keeps the shape's <defs> ids unique. */
function shapeFill(n){
  const m=n.meta||{};
  return {mode:m.fillMode||'tint', color:m.fillColor, opacity:m.fillOpacity,
          pattern:m.pattern||'dots', tint:(boardBg&&boardBg.tint)||DEFAULT_TINT, uid:n.id};
}
/* Apply the adjustable canvas tint to the stage. Everywhere not covered by a
   node reads this shade; see-through shapes over bare canvas reveal it too. */
function applyCanvasTint(){
  const st=$('stage'); if(st) st.style.background=(boardBg&&boardBg.tint)||DEFAULT_TINT;
}
/* Build the <pattern> def + id for one shape node, anchored to world coords.
   x/y offset = -(node world top-left) so the tile origin maps to world (0,0).
   Only built for see-through shapes over an active board graphic; a tint/pattern/
   neon shape needs no reveal def, and see-through with no board graphic falls back
   to the canvas tint inside shapeSVG. */
function bgDefFor(n){
  if(n.kind!=='shape') return null;
  if(((n.meta&&n.meta.fillMode)||'tint')!=='seethrough') return null;
  if(!bgActive()) return null;
  const w=n.w||150, h=n.h||90, wx=n.x-w/2, wy=n.y-h/2;
  const id='bg-'+n.id;
  const o=(boardBg.opacity==null?0.5:boardBg.opacity);
  if(boardBg.type==='image'){
    const iw=boardBg.imgW||512, ih=boardBg.imgH||512;
    return {id, def:`<pattern id="${id}" patternUnits="userSpaceOnUse" x="${-wx}" y="${-wy}" `
      +`width="${iw}" height="${ih}"><image href="${boardBg.image}" x="0" y="0" `
      +`width="${iw}" height="${ih}" preserveAspectRatio="xMidYMid slice" opacity="${o}"/></pattern>`};
  }
  if(boardBg.type==='swimlanes'){
    const bw=Math.max(40,boardBg.lanes||220), c=boardBg.color||'#46b1ff', vert=(boardBg.dir!=='h');
    // Two-band period (alternating tint) with a divider at each band edge.
    const per=bw*2, band=(x,fo)=>vert
      ? `<rect x="${x}" y="0" width="${bw}" height="${per}" fill="${c}" fill-opacity="${fo}"/>`
      : `<rect x="0" y="${x}" width="${per}" height="${bw}" fill="${c}" fill-opacity="${fo}"/>`;
    const line=vert
      ? `<path d="M0 0V${per}M${bw} 0V${per}" stroke="${c}" stroke-opacity="${Math.min(1,o*1.4)}" stroke-width="1.5"/>`
      : `<path d="M0 0H${per}M0 ${bw}H${per}" stroke="${c}" stroke-opacity="${Math.min(1,o*1.4)}" stroke-width="1.5"/>`;
    return {id, def:`<pattern id="${id}" patternUnits="userSpaceOnUse" x="${-wx}" y="${-wy}" `
      +`width="${per}" height="${per}">${band(0,o*0.5)}${band(bw,o*0.16)}${line}</pattern>`};
  }
  return null;   // any other board type reveals nothing
}
const BG_INKS=['#46b1ff','#46d39a','#ffb454','#ff6b6b','#c792ea','#ffd866','#33d6d6','#8aa0bd','#e8f0fb'];
// A small palette of canvas base shades for the adjustable canvas tint.
const TINT_SHADES=['#0a0e14','#0e1622','#12161f','#0c1418','#101a12','#1a0f14','#161020','#141414'];
function bgApply(patch){ Object.assign(boardBg,patch); saveBg(); render(); buildBgPanel(); }
/* Faint dotted outline of the board image's world bounds.
   The image is only ever *revealed* through see-through shapes, so with no such shape
   on the board an upload changed nothing visible anywhere and read as "the image didn't
   upload". This marks where the image actually is — the same world-origin anchor
   bgDefFor() samples from — so the upload has an immediate, honest effect. Decorative
   only: it is drawn into the edge layer, never added to `nodes`, so it cannot be
   selected, saved, or mistaken for board content. */
function drawBgOutline(svg,OX,OY){
  if(!bgActive()||boardBg.type!=='image'||!boardBg.image) return;
  const iw=boardBg.imgW||512, ih=boardBg.imgH||512;
  const g=document.createElementNS(SVGNS,'g');
  g.setAttribute('id','bgOutline');
  g.setAttribute('pointer-events','none');
  g.innerHTML=`<rect x="${OX}" y="${OY}" width="${iw}" height="${ih}" rx="6" fill="none" `
      +`stroke="#46b1ff" stroke-opacity=".45" stroke-width="2" stroke-dasharray="10 8"/>`
    +`<text x="${OX+12}" y="${OY+26}" fill="#46b1ff" fill-opacity=".55" font-size="14" `
      +`font-weight="700">background image · ${iw}×${ih}</text>`;
  svg.appendChild(g);
}
function deleteFrame(id){
  pushUndo();
  nodes=nodes.filter(n=>n.id!==id);
  covers=covers.filter(c=>c.target!==id);
  strokes=strokes.map(s=>s.clip===id?{...s,clip:inkFns.CLIP_BOARD}:s);
  frames().forEach((f,i)=>{ f.order=i+1; });   // renumber 1..n so the list stays tidy
  frameCycleIx=-1; if(selId===id) selId=null;
  render(); buildBgPanel();
}
function inkSurface(){
  const n=selId&&nodeById(selId);
  return (n&&n.kind==='frame')?n.id:inkFns.CLIP_BOARD;
}
function addLayer(target){
  const t=target||inkSurface();
  const stack=inkFns.coversForTarget(covers,t);
  if(stack.length>=inkFns.MAX_LAYERS){ toast('Layer cap ('+inkFns.MAX_LAYERS+') on this surface'); return; }
  pushUndo();
  const fill=inkFns.nextLayerFill(covers,t);
  const order=inkFns.nextLayerOrder(covers,t);
  covers=covers.concat([{id:'cv-'+Date.now().toString(36),target:t,fill,photo:'',order}]);
  toast('Layer '+order+' · '+fill+' (scratch to reveal underneath)');
  render();
}
function popLayer(target){
  const t=target||inkSurface();
  const top=inkFns.topCover(covers,t);
  if(!top){ toast('No layer to remove'); return; }
  pushUndo();
  covers=covers.filter(c=>c.id!==top.id);
  strokes=strokes.filter(s=>s.layer!==top.id);
  toast('Top layer removed');
  render();
}
function toggleCover(target){
  /* Empty surface → add; otherwise add another coating. */
  addLayer(target);
}
function buildInkDock(){
  const dock=$('inkdock'); if(!dock) return;
  const on=tool==='paint'||tool==='scratch';
  dock.classList.toggle('on', on);
  if(!on) return;
  if(!brush) brush=inkFns.loadBrush();
  const b=inkFns.clampBrush(brush);
  const st=ink&&ink.stats();
  const surf=inkSurface();
  const stack=inkFns.coversForTarget(covers,surf);
  const chips=stack.map((c,i)=>`<span class="lay${i===stack.length-1?' top':''}" style="background:${c.fill||'#1b2738'}" title="Layer ${c.order||i+1} ${c.fill||''}"></span>`).join('');
  dock.innerHTML=`<label>Colour<input type="color" id="inkColor" value="${b.color&&b.color[0]==='#'?b.color:'#46b1ff'}"></label>`
    +`<label>Hex<input type="text" id="inkHex" value="${b.color||'#46b1ff'}" spellcheck="false"></label>`
    +`<label>Size<input type="range" id="inkW" min="2" max="64" step="1" value="${b.width}"></label>`
    +`<label>Opacity<input type="range" id="inkOp" min="0.05" max="1" step="0.05" value="${b.opacity}"></label>`
    +`<label>Hard<input type="range" id="inkHd" min="0" max="1" step="0.05" value="${b.hardness}"></label>`
    +`<span class="layers" title="Bottom → top">${chips||'<span class="lcount">no layers</span>'}</span>`
    +`<span class="lcount">${stack.length} layer${stack.length===1?'':'s'}</span>`
    +`<button class="btn" id="inkAddCover" title="Add a coating that covers everything underneath">＋ Layer</button>`
    +`<button class="btn" id="inkPopLayer" ${stack.length?'':'disabled'} title="Remove the top coating">− Top</button>`
    +`<span class="istat" id="inkStat">${st&&st.n?('p95 '+st.p95.toFixed(1)+'ms'):'latency —'}</span>`;
  const apply=()=>{ brush=inkFns.clampBrush({color:$('inkHex').value,width:+$('inkW').value,
    opacity:+$('inkOp').value,hardness:+$('inkHd').value}); inkFns.saveBrush(brush); };
  $('inkColor').oninput=()=>{ $('inkHex').value=$('inkColor').value; apply(); };
  $('inkHex').onchange=()=>{ const v=$('inkHex').value.trim(); if(/^#[0-9a-fA-F]{6}$/.test(v)) $('inkColor').value=v; apply(); };
  $('inkW').oninput=$('inkOp').oninput=$('inkHd').oninput=apply;
  $('inkAddCover').onclick=()=>{ addLayer(surf); buildInkDock(); };
  $('inkPopLayer').onclick=()=>{ popLayer(surf); buildInkDock(); };
}
// The tBg tool hosts a "Frames & canvas" panel: draw/list/cycle frames, set the
// adjustable canvas tint, and configure the graphic that see-through shapes reveal.
function buildBgPanel(){
  const p=$('bgpanel'); if(p.style.display!=='block') return;
  const b=boardBg, seg=(t,l)=>`<button data-type="${t}" class="${b.type===t?'on':''}">${l}</button>`;
  const inks=BG_INKS.map(c=>`<div class="sw ${b.color===c?'on':''}" data-color="${c}" style="background:${c}" title="Background colour ${c}"></div>`).join('');
  const tint=(b.tint||DEFAULT_TINT);
  const tints=TINT_SHADES.map(c=>`<div class="sw ${tint===c?'on':''}" data-tint="${c}" style="background:${c}" title="Canvas tint ${c}"></div>`).join('');
  const fs=frames();
  const frameRows=fs.length
    ? fs.map(f=>`<div class="frow" data-fit="${f.id}" title="Click to fit the view to this frame"><b>${f.order}</b><span>${escapeHtml(f.text||'Frame')}</span>`
        +`<button class="fdel" data-del="${f.id}" title="Delete frame">✕</button></div>`).join('')
    : '<div class="sub">No frames yet — click “Draw frame”, then drag on the canvas.</div>';
  let bgBody='';
  if(b.type==='swimlanes'){
    bgBody='<h4>Direction</h4><div class="seg">'
      +`<button data-dir="v" class="${b.dir!=='h'?'on':''}">Vertical</button>`
      +`<button data-dir="h" class="${b.dir==='h'?'on':''}">Horizontal</button></div>`
      +`<h4>Band width</h4><input type="number" id="bgLaneW" min="40" max="800" step="10" value="${b.lanes||220}">`
      +'<h4>Color</h4><div class="row">'+inks+'</div>';
  }else{
    bgBody='<h4>Image</h4><input type="file" id="bgImgFile" accept="image/*">'
      +(b.image?`<div class="sub">✓ Loaded ${b.imgW||'?'}×${b.imgH||'?'} · anchored at world origin`
          +` · dotted outline on the canvas shows where it is`
          +`<br>Draw a shape over it and set its fill to “see-through” to reveal the picture.</div>`
        :'<div class="sub">Anchored in world space; shown through see-through shapes over it.</div>');
  }
  p.innerHTML=`<span class="close" id="bgClose">✕</span>`
    +`<div class="toggle"><b>Frames &amp; canvas</b></div>`
    +`<h4>Frames</h4>`
    +`<div class="seg"><button id="fDraw" title="Draw a frame region — then drag on the canvas">＋ Draw frame</button>`
    +`<button id="fNext" ${fs.length?'':'disabled'} title="Fit the view to the next frame (] key)">▶ Next</button></div>`
    +`<div class="frames">${frameRows}</div>`
    +`<h4>Canvas tint</h4><div class="row">${tints}</div>`
    +`<h4>Background <span style="text-transform:none;color:var(--dim)">(revealed by see-through shapes)</span></h4>`
    +`<label style="font-size:11px;color:var(--dim);display:flex;gap:5px;align-items:center;cursor:pointer;margin-bottom:6px">`
    +`<input type="checkbox" id="bgOn" ${b.on?'checked':''}> On</label>`
    +`<div class="seg">${seg('swimlanes','Swimlanes')}${seg('image','Image')}</div>`
    +bgBody
    +`<h4>Opacity</h4><input type="range" id="bgOpacity" min="0" max="1" step="0.05" value="${b.opacity==null?0.5:b.opacity}">`;
  // wire
  $('bgClose').onclick=closeBgPanel;
  $('fDraw').onclick=()=>{ closeBgPanel(); setTool('frame'); };
  const fn=$('fNext'); if(fn) fn.onclick=()=>cycleFrame(1);
  p.querySelectorAll('.frow').forEach(x=>x.onclick=e=>{
    if(e.target.classList.contains('fdel')) return;              // handled below
    const f=nodeById(x.dataset.fit); if(f){ selId=f.id; fitToFrame(f); render(); }
  });
  p.querySelectorAll('.fdel').forEach(x=>x.onclick=e=>{ e.stopPropagation(); deleteFrame(x.dataset.del); });
  p.querySelectorAll('.sw[data-tint]').forEach(x=>x.onclick=()=>{ boardBg.tint=x.dataset.tint; applyCanvasTint(); render(); buildBgPanel(); });
  $('bgOn').onchange=e=>bgApply({on:e.target.checked});
  p.querySelectorAll('.seg button[data-type]').forEach(x=>x.onclick=()=>bgApply({type:x.dataset.type,on:true}));
  p.querySelectorAll('.seg button[data-dir]').forEach(x=>x.onclick=()=>bgApply({dir:x.dataset.dir,on:true}));
  p.querySelectorAll('.sw[data-color]').forEach(x=>x.onclick=()=>bgApply({color:x.dataset.color,on:true}));
  const lw=$('bgLaneW'); if(lw) lw.onchange=e=>bgApply({lanes:Math.max(40,+e.target.value||220)});
  const op=$('bgOpacity'); if(op) op.oninput=e=>{boardBg.opacity=+e.target.value; render();};
  const f=$('bgImgFile'); if(f) f.onchange=async e=>{
    const file=e.target.files&&e.target.files[0]; if(!file) return;
    try{
      const src=await fileToDownscaledDataURL(file, 1600);
      const im=new Image();
      im.onload=()=>{
        bgApply({image:src,imgW:im.naturalWidth,imgH:im.naturalHeight,on:true,type:'image'});
        // Confirm the upload explicitly: rebuilding the panel resets the file input to
        // blank, which on its own reads as "nothing happened".
        toast(`Image loaded ${im.naturalWidth}×${im.naturalHeight} — outlined on the canvas`);
      };
      im.onerror=()=>toast('Image load failed — the file could not be decoded');
      im.src=src;
    }catch(_){ toast('Image load failed'); }
  };
}
function openBgPanel(){
  const p=$('bgpanel'); if(p.style.display==='block'){ closeBgPanel(); return; }
  p.style.display='block'; buildBgPanel();
  const r=$('tBg').getBoundingClientRect(), pr=p.getBoundingClientRect();
  p.style.left=Math.max(8,Math.min(window.innerWidth-pr.width-8, r.left+r.width/2-pr.width/2))+'px';
  p.style.top=Math.max(8,r.top-pr.height-10)+'px';
  syncCardSuppress();
}
function closeBgPanel(){ const p=$('bgpanel'); if(p) p.style.display='none'; syncCardSuppress(); }

/* ====================== draggable toolbar ====================== */
(function dragToolbar(){
  const bar=$('toolbar'), grip=$('tbGrip'); if(!bar||!grip)return;
  let d=null; const clamp=(v,lo,hi)=>Math.max(lo,Math.min(hi,v));
  function place(left,top){ const r=bar.getBoundingClientRect();
    left=clamp(left,4,window.innerWidth-r.width-4); top=clamp(top,4,window.innerHeight-r.height-4);
    bar.style.left=left+'px'; bar.style.top=top+'px'; bar.style.bottom='auto'; bar.style.transform='none'; }
  grip.addEventListener('pointerdown',e=>{ const r=bar.getBoundingClientRect();
    d={dx:e.clientX-r.left,dy:e.clientY-r.top}; bar.classList.add('dragging');
    grip.setPointerCapture(e.pointerId); e.preventDefault(); });
  grip.addEventListener('pointermove',e=>{ if(d) place(e.clientX-d.dx,e.clientY-d.dy); });
  const end=e=>{ if(!d)return; d=null; bar.classList.remove('dragging');
    try{grip.releasePointerCapture(e.pointerId);}catch(_){} };
  grip.addEventListener('pointerup',end); grip.addEventListener('pointercancel',end);
  window.addEventListener('resize',()=>{ if(bar.style.left) place(parseFloat(bar.style.left)||0,parseFloat(bar.style.top)||0); });
})();

/* ====================== image nodes (paste / drop) ====================== */
// Downscale to keep saved boards small: longest side <= maxDim, JPEG-encoded.
function fileToDownscaledDataURL(file, maxDim=768){
  return new Promise((res,rej)=>{
    const fr=new FileReader();
    fr.onload=()=>{ const img=new Image();
      img.onload=()=>{ let w=img.width,h=img.height;
        const scale=Math.min(1, maxDim/Math.max(w,h)); w=Math.round(w*scale); h=Math.round(h*scale);
        const c=document.createElement('canvas'); c.width=w; c.height=h;
        c.getContext('2d').drawImage(img,0,0,w,h);
        try{ res(c.toDataURL('image/jpeg',0.82)); }catch(e){ rej(e); } };
      img.onerror=rej; img.src=fr.result; };
    fr.onerror=rej; fr.readAsDataURL(file);
  });
}
function addImageNode(src,x,y,caption){
  const n={id:uid(),text:(caption||'image').slice(0,80),x,y,kind:'image',glyph:'',icon:{},meta:{src}};
  nodes.push(n); render(); return n;
}
// Non-image drop: the server turns the file into text by code, then (with a model
// configured) one gated call titles and summarizes it; it lands as an editable 'file' node.
function fileToB64(file){
  return new Promise((res,rej)=>{ const fr=new FileReader();
    fr.onload=()=>res(String(fr.result).split(',',2)[1]||''); fr.onerror=rej; fr.readAsDataURL(file); });
}
async function dumpFile(file,x,y){
  let b64=''; try{ b64=await fileToB64(file); }catch(_){}
  const meta={filename:file.name};
  let title=file.name, summary='';
  if(modelStatus.configured){
    toast('Sorting '+file.name+'…');
    const j=await modelCall('/api/mindmap/dump',{filename:file.name,content:b64});
    if(j.ok){
      const lines=String(j.text||'').split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
      title=(lines[0]||file.name).slice(0,120); summary=(lines[1]||'').slice(0,400);
      meta.method=j.method; meta.truncated=!!j.truncated;
    }else{ toast(file.name+': '+j.error); }
  }else{ toast(file.name+' added unsorted: no model is configured'); }
  const n=addNode(title,x,y,'file','doc',null,meta);
  if(summary) n.summary=summary;
  n.title=title;
  return n;
}
// drop files anywhere on the canvas: images render inline, everything else gets classified
stage.addEventListener('dragover',e=>{ e.preventDefault(); });
stage.addEventListener('drop',async e=>{
  const all=[...((e.dataTransfer&&e.dataTransfer.files)||[])];
  if(!all.length) return;
  e.preventDefault();
  const w=screenToWorld(e.clientX,e.clientY);
  pushUndo();
  const imgs=all.filter(f=>f.type.startsWith('image/'));
  const others=all.filter(f=>!f.type.startsWith('image/'));
  for(let i=0;i<imgs.length;i++){
    try{ const src=await fileToDownscaledDataURL(imgs[i]); addImageNode(src, w.x+i*28, w.y+i*28, imgs[i].name); }catch(_){}
  }
  if(imgs.length) toast(imgs.length+' image'+(imgs.length>1?'s':'')+' added');
  for(let j=0;j<others.length;j++){ await dumpFile(others[j], w.x+(imgs.length+j)*28, w.y+(imgs.length+j)*28); }
});
// paste an image (or a block of text) onto the board
window.addEventListener('paste',async e=>{
  const typing=document.activeElement.tagName==='INPUT'||document.activeElement.isContentEditable;
  if(typing) return;                       // let normal paste happen inside inputs / editable nodes
  const items=[...((e.clipboardData&&e.clipboardData.items)||[])];
  const imgItem=items.find(it=>it.type&&it.type.startsWith('image/'));
  if(imgItem){
    const file=imgItem.getAsFile(); if(!file) return;
    e.preventDefault();
    const c=viewCenterWorld();
    try{ const src=await fileToDownscaledDataURL(file); pushUndo(); addImageNode(src,c.x,c.y,'pasted image'); toast('Image added'); }catch(_){}
    return;
  }
  const fileItem=items.find(it=>it.kind==='file');   // a non-image file on the clipboard
  if(fileItem){
    const file=fileItem.getAsFile();
    if(file){ e.preventDefault(); const c=viewCenterWorld(); pushUndo(); dumpFile(file,c.x,c.y); return; }
  }
  const text=((e.clipboardData&&e.clipboardData.getData('text/plain'))||'').trim();
  if(text){                                 // pasted text becomes a note the interpreter reads
    e.preventDefault();
    const c=viewCenterWorld(); pushUndo();
    addNode(text.slice(0,240), c.x, c.y, 'note'); toast('Text note added');
  }
});

/* ====================== stamp editor (dropped files) ====================== */
// The model's title and summary are a suggestion; the person edits them here. The closed
// vocabularies come from the server's pack (/api/mindmap/dump/config), never from this page.
// Sensitivity is a node field: a model call that reads the board carries every tag on it.
let _dumpCfg=null, _stampNode=null;
async function dumpCfg(){
  if(_dumpCfg) return _dumpCfg;
  try{ const r=await fetch('/api/mindmap/dump/config'); _dumpCfg=r.ok?await jsonOf(r):null; }catch(_){ _dumpCfg=null; }
  _dumpCfg=_dumpCfg||{entity_types:[],sensitivity:[],status:[],tags:[]};
  return _dumpCfg;
}
function _opt(list,cur,blank){ return (blank?'<option value="">—</option>':'')+(list||[]).map(v=>
  `<option value="${escapeHtml(v)}"${v===cur?' selected':''}>${escapeHtml(v)}</option>`).join(''); }
async function openStamp(n){
  const cfg=await dumpCfg(); _stampNode=n;
  $('seTitle').textContent=n.title||n.text||'File';
  $('seSub').textContent=(n.meta&&n.meta.filename?n.meta.filename+' · ':'')
    +(n.meta&&n.meta.method?'read as '+n.meta.method:'not read by a model');
  const curTags=new Set(Array.isArray(n.tags)?n.tags:[]);
  const sens=Array.isArray(n.sensitivity)?n.sensitivity[0]:n.sensitivity;
  const tagRow=(cfg.tags||[]).map(t=>`<label><input type="checkbox" data-tag="${escapeHtml(t)}"`
        +`${curTags.has(t)?' checked':''}> ${escapeHtml(t)}</label>`).join('');
  $('seFields').innerHTML=`
    <label>Title</label><input id="seName" value="${escapeHtml(n.title||n.text||'')}">
    <label>Summary</label><textarea id="seSummary" rows="2">${escapeHtml(n.summary||'')}</textarea>
    <label>Entity type</label><select id="seEType">${_opt(cfg.entity_types,n.entity_type,true)}</select>
    <label>Status</label><select id="seStatus">${_opt(cfg.status,n.status,true)}</select>
    <label>Sensitivity</label><select id="seSens">${_opt(cfg.sensitivity,sens,true)}</select>
    <label>Tags</label><div class="tagrow">${tagRow}</div>`;
  $('stampEd').classList.add('on');
  syncCardSuppress();
}
function closeStamp(){ $('stampEd').classList.remove('on'); _stampNode=null; syncCardSuppress(); }
function saveStamp(){
  if(!_stampNode) return closeStamp();
  pushUndo();
  const n=_stampNode;
  n.title=$('seName').value.trim()||n.title; n.text=n.title;
  n.summary=$('seSummary').value.trim();
  n.entity_type=$('seEType').value||undefined;
  n.status=$('seStatus').value||undefined;
  n.sensitivity=$('seSens').value||undefined;
  n.tags=[...document.querySelectorAll('#seFields input[data-tag]:checked')].map(c=>c.getAttribute('data-tag'));
  n.glyph={Dataset:'data',Image:'image'}[n.entity_type]||'doc';
  render(); toast('Stamp updated'); closeStamp();
}
$('seCancel').addEventListener('click',closeStamp);
$('seSave').addEventListener('click',saveStamp);
$('stampEd').addEventListener('click',e=>{ if(e.target.id==='stampEd') closeStamp(); });
// double-click a file node to edit its stamp
$('world').addEventListener('dblclick',e=>{
  const el=e.target.closest('.node.file'); if(!el) return;
  const n=nodeById(el.dataset.id); if(n){ e.preventDefault(); openStamp(n); }
});

/* ====================== board reading ====================== */
// "Read the board" saves it and asks the model once for a reading. The reading never
// changes the board (Primitive 2): it is text shown here.
async function runInterpret(){
  if(!modelStatus.configured){ toast('No model configured'); return; }
  if(!nodes.length){ toast('The board is empty'); return; }
  $('reading').classList.add('busy');
  let j={ok:false,error:'The board could not be saved, so nothing was asked.'};
  if(await saveBoard(true)) j=await modelCall('/api/mindmap/interpret',{board:mapId});
  $('reading').classList.remove('busy');
  $('rBody').innerHTML=j.ok?`<div class="rsummary">${escapeHtml(j.text||'(no reading)')}</div>`
    :`<div class="rempty">${escapeHtml(j.error)}</div>`;
}
$('btnRead').onclick=runInterpret;
$('rToggle').onclick=()=>{ const p=$('reading'); p.classList.toggle('collapsed');
  $('rToggle').textContent=p.classList.contains('collapsed')?'▸':'▾'; };

// Open a saved board by id: used by /mindmap?map=<id>, the Load button and the
// starter-board button.
async function openSavedMap(id){
  let r, j;
  try{ r=await fetch(API+'/api/mindmap/boards/'+encodeURIComponent(id)); j=await jsonOf(r); }
  catch(_){ toast('Load failed: the server did not answer'); return false; }
  if(!r.ok){ toast('Load failed: '+(j.error||id)); return false; }
  const b=fromServer(j);
  mapId=j.board; $('mapName').value=j.board; boardHead=j.head||null;
  nodes=b.nodes; edges=b.edges; selId=null; strokes=[]; covers=[]; undoStack=[]; redoStack=[];
  loadBg(); captureBoardPayload(j);
  render(); fit(); toast('Loaded '+j.board);
  nodes.forEach(n=>{ if((!n.icon||!Object.keys(n.icon).length)&&!isRung(n.kind)&&!['shape','text','frame','image'].includes(n.kind)) fetchIcon(n); });
  loadAttribution();
  return true;
}
minimap=createMinimap({
  nodes:()=>nodes, cam:()=>cam, stage,
  setCam:(x,y)=>{cam.x=x; cam.y=y; applyCam();},
  colorOf:n=>isRung(n.kind)?RUNG[n.kind].color
    :n.kind==='note'?'#ffd866':n.kind==='shape'?'#8aa0bd':'#46b1ff',
});
initSketch(); brush=inkFns.loadBrush(); ensureInk(); applyCam(); setTool('select'); setHint();

/* ---------- popup surfaces: draggable + resizable ---------- */
// Every floating surface moves by its header (or any blank area, slop-guarded so
// clicks stay clicks) and resizes from the bottom-right grip (js/resize.js).
// Free panels persist position+size under omli.mm.<id>.pos/.size — the drag.js
// localStorage convention. Anchored popovers re-open at their anchor (cursor/
// toolbar button) by design, so only their size persists. Transient pick-one
// menus (#rel/#colorpop/#ragmenu) move but don't resize. Mini-cards are
// suppressed while adjusting (a hover card never shows while something moves); both helpers clamp
// to the viewport, and popups live outside #stage so pan/zoom never sees these
// gestures (the grip stops propagation as a second guard).
function wirePopup(el,{handle,posKey,sizeKey,minW=160,minH=100,resize=true,freezeH=false,onAdjustStart}={}){
  if(!el||typeof makeDraggable!=='function'||typeof makeResizable!=='function') return;
  const begin=()=>{
    // a top+bottom-anchored panel would collapse when drag releases its bottom
    // anchor — freeze its current height first
    if(freezeH&&!el.style.height){ const r=el.getBoundingClientRect(); if(r.height)el.style.height=r.height+'px'; }
    if(onAdjustStart)onAdjustStart();
    _popupAdjusting=true; syncCardSuppress();
  };
  const end=()=>{ _popupAdjusting=false; syncCardSuppress(); };
  makeDraggable(el,{handle:(handle&&el.querySelector(handle))||el,
    ignore:'input,textarea,select,button,a,[contenteditable],.rz-grip',
    slop:4,storageKey:posKey||null,onDragStart:begin,onDragEnd:end});
  if(resize) makeResizable(el,{minWidth:minW,minHeight:minH,storageKey:sizeKey||null,
    onResizeStart:begin,onResizeEnd:end});
}
{
  const K=id=>({posKey:'omli.mm.'+id+'.pos',sizeKey:'omli.mm.'+id+'.size'});
  // free-floating panels: position + size persist
  wirePopup($('nodebar'),{handle:'header',...K('nodebar'),minW:200,minH:120});
  wirePopup($('think'),{handle:'.thead',...K('think'),minW:200,minH:120});
  wirePopup($('reading'),{handle:'.rhead',...K('reading'),minW:200,minH:100});
  wirePopup($('inspector'),{...K('inspector'),minW:200,minH:140});
  wirePopup($('runOut'),{handle:'header',...K('runOut'),minW:220,minH:140});
  wirePopup($('designer'),{handle:'header',...K('designer'),minW:220,minH:180});
  // anchored popovers: draggable; size persists, position re-anchors on open
  wirePopup($('pop'),{sizeKey:'omli.mm.pop.size',minW:190,minH:120});
  wirePopup($('framepop'),{sizeKey:'omli.mm.framepop.size',minW:210,minH:170});
  wirePopup($('bgpanel'),{sizeKey:'omli.mm.bgpanel.size',minW:210,minH:170});
  wirePopup($('shapePalette'),{sizeKey:'omli.mm.shapePalette.size',minW:180,minH:150});
  // transient pick-one menus: movable only
  wirePopup($('rel'),{resize:false});
  wirePopup($('colorpop'),{resize:false});
  wirePopup($('ragmenu'),{resize:false});
  // modal cards: flex-centred until dragged, then pinned fixed where they sit
  const modal=(sel,o)=>{ const card=document.querySelector(sel); if(!card)return;
    wirePopup(card,{...o,onAdjustStart:()=>{ if(card.style.position==='fixed')return;
      const r=card.getBoundingClientRect(); card.style.position='fixed'; card.style.margin='0';
      card.style.left=r.left+'px'; card.style.top=r.top+'px'; }}); };
  modal('#stampEd .card',{handle:'h3',minW:300,minH:220});
}

(async function seedFromUrl(){
  // /mindmap?map=<id> opens that board; plain /mindmap opens the starter board.
  loadModelStatus();
  try{
    const q=new URLSearchParams(location.search);
    const map=(q.get('map')||'').trim();
    if(map && await openSavedMap(map)) return;
    await openSavedMap(STARTER_BOARD);
  }catch(e){}
})();
