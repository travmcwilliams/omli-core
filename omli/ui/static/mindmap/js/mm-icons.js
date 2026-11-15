/* Generative node icons + the seeded procedural loading animation.

   svgIcon renders a deterministic SVG identicon from the icon seed the
   server derives; startLoading animates the same visual language on a
   canvas while the model thinks. Pure rendering — no fetch, no canvas state. */

/* deterministic PRNG, seeded from the server's icon seed */
export function mulberry32(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);
  t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}

// Module-private same-origin API base: icon files are served by this page's own server.
const API_BASE='';

/* generative SVG icon from an icon seed */
export function svgIcon(icon,size=54){
  icon=icon||{};
  // Library tier: a keyword-matched icon from the bundled icon set, recolored (monochrome SVG via CSS mask).
  // Keeps the exact class="ic" token so call-site .replace('class="ic"',…) still restyles it.
  if(icon.kind==='library' && icon.src){
    const hue=icon.hue??200, sat=icon.sat??68, light=icon.light??60;
    const col=`hsl(${hue} ${sat}% ${light}%)`, c2=`hsl(${(hue+90)%360} ${sat}% ${Math.min(74,light+12)}%)`;
    const src=API_BASE+icon.src, br=Math.round(size*0.22), pad=Math.round(size*0.2);
    const mask=`url('${src}') center/contain no-repeat`;
    return `<span class="ic" style="position:relative;width:${size}px;height:${size}px;`
      +`border-radius:${br}px;background:#0a0e14;border:1px solid ${c2};box-sizing:border-box">`
      +`<span style="position:absolute;inset:${pad}px;background:${col};`
      +`-webkit-mask:${mask};mask:${mask}"></span></span>`;
  }
  const seed=icon.seed||1, rnd=mulberry32(seed);
  const hue=icon.hue??200, hue2=icon.hue2??((hue+90)%360);
  const sat=icon.sat??68, light=icon.light??58;
  const arms=Math.max(3,Math.min(8,icon.arms||5));
  const rings=Math.max(1,Math.min(3,icon.rings||2));
  const rot=icon.rotation||0, jit=icon.jitter??0.4;
  const c1=`hsl(${hue} ${sat}% ${light}%)`, c2=`hsl(${hue2} ${sat}% ${Math.min(72,light+10)}%)`;
  const cx=size/2, cy=size/2, R=size*0.42;
  let body='';
  const form=icon.form||'orbit';
  if(form==='orbit'){
    for(let r=1;r<=rings;r++){const rr=R*r/rings;
      body+=`<circle cx="${cx}" cy="${cy}" r="${rr.toFixed(1)}" fill="none" stroke="${c2}" stroke-width="1.4" opacity="${0.5+0.15*r}"/>`;}
    for(let i=0;i<arms;i++){const a=rot*Math.PI/180+i/arms*Math.PI*2;
      const rr=R*(0.5+0.5*rnd());const x=cx+Math.cos(a)*rr,y=cy+Math.sin(a)*rr;
      body+=`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${(2+2*rnd()).toFixed(1)}" fill="${c1}"/>`;}
    body+=`<circle cx="${cx}" cy="${cy}" r="${(R*0.22).toFixed(1)}" fill="${c1}"/>`;
  }else if(form==='petal'){
    for(let i=0;i<arms;i++){const a=rot+i*360/arms;
      body+=`<ellipse cx="${cx}" cy="${cy-R*0.55}" rx="${(R*0.22).toFixed(1)}" ry="${(R*0.55).toFixed(1)}"
        fill="${i%2?c2:c1}" opacity="0.82" transform="rotate(${a} ${cx} ${cy})"/>`;}
    body+=`<circle cx="${cx}" cy="${cy}" r="${(R*0.2).toFixed(1)}" fill="${c2}"/>`;
  }else if(form==='sigil'){
    const pts=[];for(let i=0;i<arms;i++){const a=rot*Math.PI/180+i/arms*Math.PI*2;
      const rr=R*(0.7+0.3*(i%2));pts.push([cx+Math.cos(a)*rr,cy+Math.sin(a)*rr]);}
    body+=`<polygon points="${pts.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ')}"
      fill="none" stroke="${c1}" stroke-width="2" stroke-linejoin="round"/>`;
    const step=arms>4?2:1;
    for(let i=0;i<pts.length;i++){const j=(i+step)%pts.length;
      body+=`<line x1="${pts[i][0].toFixed(1)}" y1="${pts[i][1].toFixed(1)}" x2="${pts[j][0].toFixed(1)}" y2="${pts[j][1].toFixed(1)}" stroke="${c2}" stroke-width="1" opacity="0.7"/>`;}
    body+=`<circle cx="${cx}" cy="${cy}" r="3" fill="${c2}"/>`;
  }else if(form==='lattice'){
    const n=Math.max(3,Math.min(5,arms-1)), cell=size*0.82/n, o=size*0.09;
    for(let i=0;i<n;i++)for(let j=0;j<n;j++){if(rnd()<0.42+jit*0.2)continue;
      body+=`<rect x="${(o+i*cell+1).toFixed(1)}" y="${(o+j*cell+1).toFixed(1)}"
        width="${(cell-2).toFixed(1)}" height="${(cell-2).toFixed(1)}" rx="2"
        fill="${rnd()<0.5?c1:c2}" opacity="${(0.55+0.45*rnd()).toFixed(2)}"/>`;}
  }else{ /* burst */
    for(let i=0;i<arms*2;i++){const a=rot*Math.PI/180+i/(arms*2)*Math.PI*2;
      const rr=R*(0.45+0.55*rnd());
      body+=`<line x1="${cx}" y1="${cy}" x2="${(cx+Math.cos(a)*rr).toFixed(1)}" y2="${(cy+Math.sin(a)*rr).toFixed(1)}"
        stroke="${i%2?c1:c2}" stroke-width="${(1.5+1.5*rnd()).toFixed(1)}" stroke-linecap="round"/>`;}
    body+=`<circle cx="${cx}" cy="${cy}" r="${(R*0.18).toFixed(1)}" fill="${c1}"/>`;
  }
  const g=icon.glyph?`<text x="${cx}" y="${cy+4}" text-anchor="middle" font-size="13"
    fill="#fff" opacity="0.9" font-weight="700">${icon.glyph}</text>`:'';
  return `<svg class="ic" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">
    <rect x="1" y="1" width="${size-2}" height="${size-2}" rx="12" fill="#0a0e14" stroke="${c2}" stroke-width="1" opacity="0.55"/>
    ${body}${g}</svg>`;
}

/* procedural loading animation (seeded by the clicked node's icon) — one RAF
   loop at a time; stopLoading() tears down both the loop and the caption. */
let _loaderRAF=null, _loadCapTimer=null;
const LOAD_MSGS=['Reading the board','Retrieving OMLi context','Consulting the model','Shaping directions'];
export function stopLoading(){
  if(_loaderRAF){cancelAnimationFrame(_loaderRAF); _loaderRAF=null;}
  if(_loadCapTimer){clearInterval(_loadCapTimer); _loadCapTimer=null;}
}
export function startLoading(canvas, icon, capEl){
  stopLoading();
  if(capEl){let i=0; capEl.textContent=LOAD_MSGS[0];
    _loadCapTimer=setInterval(()=>{i=(i+1)%LOAD_MSGS.length; capEl.textContent=LOAD_MSGS[i];},1600);}
  if(!canvas||!canvas.getContext) return;
  const ctx=canvas.getContext('2d');
  const dpr=window.devicePixelRatio||1, w=canvas.clientWidth||120, h=canvas.clientHeight||120;
  canvas.width=w*dpr; canvas.height=h*dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
  icon=icon||{};
  const seed=icon.seed||1, rnd=mulberry32(seed);
  const hue=icon.hue??200, hue2=icon.hue2??((hue+120)%360);
  const sat=icon.sat??68, light=icon.light??58, form=icon.form||'orbit';
  const arms=Math.max(3,Math.min(8,icon.arms||5)), dir=(seed&1)?1:-1;
  const c1=`hsl(${hue} ${sat}% ${light}%)`, c2=`hsl(${hue2} ${sat}% ${Math.min(76,light+14)}%)`;
  const parts=Array.from({length:arms},()=>({a:rnd()*6.283,r:0.32+0.52*rnd(),
    sp:0.5+rnd()*1.6,sz:1.6+rnd()*2.6,ph:rnd()*6.283}));
  const start=performance.now();
  function frame(now){
    const t=(now-start)/1000, cx=w/2, cy=h/2, R=Math.min(w,h)*0.40;
    ctx.clearRect(0,0,w,h); ctx.save(); ctx.translate(cx,cy);
    if(form==='lattice'){
      const n=Math.max(3,Math.min(5,arms-1)), cell=Math.min(w,h)*0.72/n, o=-(n*cell)/2;
      for(let i=0;i<n;i++)for(let j=0;j<n;j++){
        ctx.globalAlpha=0.22+0.6*(0.5+0.5*Math.sin(t*3-(i+j)*0.5));
        ctx.fillStyle=((i+j)&1)?c1:c2; ctx.fillRect(o+i*cell+1.5,o+j*cell+1.5,cell-3,cell-3);}
      ctx.globalAlpha=1;
    }else if(form==='burst'){
      const m=arms*2; ctx.lineCap='round'; ctx.lineWidth=2;
      for(let i=0;i<m;i++){const ang=dir*t*1.2+i/m*6.283, len=R*(0.45+0.5*(0.5+0.5*Math.sin(t*3+i)));
        ctx.strokeStyle=(i&1)?c1:c2; ctx.beginPath(); ctx.moveTo(0,0);
        ctx.lineTo(Math.cos(ang)*len,Math.sin(ang)*len); ctx.stroke();}
    }else if(form==='sigil'){
      ctx.rotate(dir*t*0.9); ctx.strokeStyle=c1; ctx.lineWidth=2; ctx.lineJoin='round'; ctx.beginPath();
      for(let i=0;i<=arms;i++){const a=i/arms*6.283, rr=R*(0.7+0.25*Math.sin(t*2+i));
        const x=Math.cos(a)*rr,y=Math.sin(a)*rr; i?ctx.lineTo(x,y):ctx.moveTo(x,y);}
      ctx.closePath(); ctx.stroke();
    }else{ /* orbit / petal: rings + orbiting particles */
      for(let r=1;r<=2;r++){ctx.globalAlpha=0.16*r; ctx.strokeStyle=c2; ctx.lineWidth=1.2;
        ctx.beginPath(); ctx.arc(0,0,R*r/2.2,0,6.283); ctx.stroke();}
      ctx.globalAlpha=1;
      parts.forEach(p=>{const ang=p.a+dir*t*p.sp, rr=R*p.r;
        ctx.fillStyle=c1; ctx.beginPath();
        ctx.arc(Math.cos(ang)*rr,Math.sin(ang)*rr,
          Math.max(0.8,p.sz*(0.7+0.5*Math.sin(t*3+p.ph))),0,6.283); ctx.fill();});
    }
    const pr=R*0.16*(0.8+0.3*Math.sin(t*2.4));
    ctx.globalAlpha=0.92; ctx.fillStyle=c2; ctx.beginPath(); ctx.arc(0,0,pr,0,6.283); ctx.fill();
    ctx.globalAlpha=1; ctx.restore();
    _loaderRAF=requestAnimationFrame(frame);
  }
  _loaderRAF=requestAnimationFrame(frame);
}
