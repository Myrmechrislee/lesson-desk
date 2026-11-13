(() => {
  'use strict';
  const root = document.querySelector('[data-whiteboard]');
  if (!root) return;
  const $ = s => root.querySelector(s), $$ = s => [...root.querySelectorAll(s)];
  const canvas = $('[data-board-canvas]'), stage = $('[data-board-stage]'), ctx = canvas.getContext('2d');
  const status = $('[data-board-status]'), dialog = $('[data-board-text-dialog]'), settings = $('[data-board-settings]');
  const objects = new Map(), pending = new Map(), images = new Map(), remote = new Map(), committing = new Map();
  const selection = new Set(), undo = [], redo = [];
  const clientId = crypto.randomUUID(), clone = v => v ? structuredClone(v) : null, uid = () => crypto.randomUUID();
  const ordered = () => [...objects.values()].sort((a, b) => (a.z || 0) - (b.z || 0));
  const actionButton = name => $(`[data-board-action="${name}"]`);
  let revision = -1, loaded = false, sending = false, polling = false, timer;
  let tool = 'select', gesture = null, draft = null, space = false, editing = null, copied = [];
  let zoom = 1, pan = {x: 110, y: 190}, width = 800, height = 600, dragDepth = 0, importing = false;
  let liveSequence = 0, liveBusy = false, lastLive = '', lastHeartbeat = 0, lastLiveReceived = 0, leaving = false;
  const presetColors = ['#243d35', '#111827', '#64748b', '#dc2626', '#ea580c', '#ca8a04', '#16a34a', '#2563eb', '#7c3aed', '#db2777', '#ffffff'];
  let personalColors = [];
  const paletteKey = `lesson-desk-colours:${root.dataset.user}`;
  try { personalColors = JSON.parse(localStorage.getItem(paletteKey) || '[]').filter(c => /^#[0-9a-f]{6}$/i.test(c)).slice(0, 16); } catch { /* Storage can be disabled. */ }
  const setStatus = (message, error = false) => { status.textContent = message; status.classList.toggle('error', error); };
  const position = event => { const r = canvas.getBoundingClientRect(); return {x: (event.clientX - r.left - pan.x) / zoom, y: (event.clientY - r.top - pan.y) / zoom}; };
  const center = () => ({x: (width / 2 - pan.x) / zoom, y: (height / 2 - pan.y) / zoom});
  const base = (type, p, w = 1, h = 1) => ({id: uid(), type, x: p.x, y: p.y, w, h, color: $('[data-board-color]').value, width: +$('[data-board-width]').value, z: Date.now() + Math.random()});
  function buttons() {
    for (const id of selection) if (!objects.has(id)) selection.delete(id);
    for (const name of ['duplicate', 'delete', 'back']) actionButton(name).disabled = !selection.size;
    actionButton('undo').disabled = !undo.length; actionButton('redo').disabled = !redo.length;
    $('[data-board-empty]').hidden = objects.size > 0 || !!draft || !!editing || remote.size > 0;
  }
  function settingsOpen(open) {
    settings.classList.toggle('is-open', open); settings.setAttribute('aria-hidden', String(!open)); settings.inert = !open;
  }
  function choose(next, showSettings = true) {
    finish();
    const selected=[...selection].map(id=>objects.get(id)).filter(Boolean);
    const compatible=next==='shape'?['rect','ellipse','polygon']:next==='path'?['path']:next==='line'?['line']:[];
    const preserve=selected.length && selected.every(e=>compatible.includes(e.type));
    tool = next; if(!preserve)selection.clear();
    if (next === 'rect' || next === 'ellipse') { $('[data-board-shape]').value = next; tool = 'shape'; }
    $$('[data-board-tool]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.boardTool === tool)));
    canvas.style.cursor = tool === 'select' ? 'default' : ['text', 'note'].includes(tool) ? 'text' : 'crosshair';
    const shape = tool === 'shape';
    $('[data-board-settings-title]').textContent = shape ? 'Shape settings' : tool === 'line' ? 'Line settings' : 'Pen settings';
    $('[data-board-color-label]').textContent = shape ? 'Border colour' : 'Ink colour';
    $('[data-board-shape-field]').hidden = !shape; $('[data-board-fill-field]').hidden = !shape;
    if(preserve){const e=selected[0];$('[data-board-color]').value=e.color;$('[data-board-width]').value=e.width;if(shape){$('[data-board-shape]').value=e.type==='polygon'?String(e.sides):e.type;$('[data-board-no-fill]').checked=!e.fill||e.fill==='transparent';if(e.fill&&e.fill!=='transparent')$('[data-board-fill]').value=e.fill;}refreshPresets();}
    settingsOpen(showSettings && ['path', 'shape', 'line'].includes(tool)); draw();
  }
  function apply(changes, remember = true) {
    if (!changes.length) return;
    const history = [];
    for (const change of changes) {
      history.push({id: change.id, before: clone(objects.get(change.id)), after: clone(change.element)});
      if (change.element) objects.set(change.id, clone(change.element)); else objects.delete(change.id);
      if (committing.has(change.id)) { if(change.element)committing.set(change.id,clone(change.element));else committing.delete(change.id); }
      pending.set(change.id, {id: change.id, element: clone(change.element)});
    }
    if (remember) { undo.push(history); if (undo.length > 80) undo.shift(); redo.length = 0; }
    setStatus('Saving…'); clearTimeout(timer); timer = setTimeout(flush, 150); draw();
  }
  function receive(data) {
    if (data.revision < revision) return;
    const active = gesture?.before?.map(e => clone(objects.get(e.id))).filter(Boolean) || [];
    revision = data.revision;
    if (!data.unchanged) {
      objects.clear(); for (const e of data.elements) objects.set(e.id, e);
      for (const change of pending.values()) { if (change.element) objects.set(change.id, clone(change.element)); else objects.delete(change.id); }
      for (const e of active) objects.set(e.id, e);
    }
    draw();
  }
  async function responseError(response) {
    const text = await response.text();
    const doc = new DOMParser().parseFromString(text, 'text/html');
    return doc.querySelector('.error-message, .error p, .empty p, main p')?.textContent.trim() || `Request failed (${response.status}). Reload the page if your session expired.`;
  }
  async function flush() {
    if (!loaded || sending || polling || !pending.size) return;
    sending = true; const batch = [...pending.values()].slice(0, 100);
    try {
      const response = await fetch(root.dataset.endpoint, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.token}, body: JSON.stringify({revision, changes: batch})});
      if (response.status === 409) { receive(await response.json()); setStatus('Merging shared changes…'); }
      else {
        if (!response.ok) throw new Error(await responseError(response));
        revision = Math.max(revision, (await response.json()).revision);
        for (const change of batch) if (pending.get(change.id) === change) { pending.delete(change.id); committing.delete(change.id); }
        setStatus(pending.size ? 'Saving…' : 'All changes saved');
      }
    } catch (error) { setStatus(`${error.message} Changes remain in this tab.`, true); }
    finally { sending = false; if (pending.size) { clearTimeout(timer); timer = setTimeout(flush, 400); } }
  }
  async function poll() {
    if (sending || polling || importing || document.hidden) return;
    polling = true;
    try {
      const response = await fetch(`${root.dataset.endpoint}?since=${revision}`, {cache: 'no-store'});
      if (!response.ok) throw new Error(await responseError(response));
      const data = await response.json(); if (!data.unchanged || !loaded) receive(data);
      const first = !loaded; loaded = true; if (first && objects.size) fit();
      if (!pending.size) setStatus('All changes saved');
    } catch (error) { setStatus(error.message, true); }
    finally { polling = false; if (pending.size) flush(); }
  }
  // Drafts have a short lifetime on the server; saved elements use the revision API above.
  function previewElements() {
    const elements = new Map(committing);
    if (editing) elements.set(editing.id, editing);
    if (draft) elements.set(draft.id, draft);
    if (gesture?.before) for (const before of gesture.before) { const e=objects.get(before.id); if(e)elements.set(e.id,e); }
    return [...elements.values()].slice(-200);
  }
  async function live() {
    if (liveBusy || !loaded || leaving) return;
    if (document.hidden) { remote.clear(); return; }
    liveBusy = true;
    const elements = previewElements(), signature = JSON.stringify(elements), changed = signature !== lastLive;
    const heartbeat = Date.now() - lastHeartbeat > (elements.length ? 2000 : 10000);
    try {
      let response;
      if (changed || heartbeat) {
        const payload = {client_id: clientId, sequence: ++liveSequence};
        if (changed || elements.length) payload.elements = elements;
        response = await fetch(root.dataset.live, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.token}, body: JSON.stringify(payload)});
      } else response = await fetch(`${root.dataset.live}?client_id=${clientId}`, {cache: 'no-store'});
      if (!response.ok) throw new Error('Live connection interrupted');
      const data = await response.json();
      if (changed || heartbeat) { lastLive = signature; lastHeartbeat = Date.now(); }
      // A cleared draft may already be saved. Read that revision before removing
      // its preview so remote strokes stay visible throughout the handoff.
      if (data.revision > revision) { await poll(); if(data.revision>revision)return; }
      lastLiveReceived = Date.now(); remote.clear();
      for (const preview of data.previews) for (const element of preview.elements) remote.set(element.id, element);
      const viewers = data.viewers.map(v => `${v.name}${v.id === root.dataset.user ? ' (you)' : ''}`);
      $('[data-board-presence]').textContent = viewers.length ? `Viewing: ${viewers.join(', ')}` : 'Viewing: you';
      draw();
    } catch {
      $('[data-board-presence]').textContent = 'Live connection reconnecting…';
      if (Date.now() - lastLiveReceived > 8000) { remote.clear(); draw(); }
    } finally { liveBusy = false; }
  }
  function leave() {
    leaving = true;
    // fetch keepalive includes the CSRF header, unlike sendBeacon.
    fetch(root.dataset.live, {method: 'POST', keepalive: true, headers: {'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.token}, body: JSON.stringify({client_id: clientId, sequence: ++liveSequence, leave: true})}).catch(() => {});
  }
  function imageFor(id) {
    if (!images.has(id)) {
      const img = new Image(), entry = {img, ready: false, failed: false}; images.set(id, entry);
      img.onload = () => { entry.ready = true; draw(); }; img.onerror = () => { entry.failed = true; draw(); };
      img.src = `${root.dataset.assets}/${id}`;
    }
    return images.get(id);
  }
  function line(c, x1, y1, x2, y2, color = '#243d35', stroke = 1) {
    c.strokeStyle = color; c.lineWidth = stroke; c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
  }
  function label(c, value, x, y, size = 13, color = '#46594b', align = 'center') {
    c.fillStyle = color; c.font = `${size}px system-ui, sans-serif`; c.textAlign = align; c.textBaseline = 'middle'; c.fillText(value, x, y);
  }
  function axes(c, left, top, right, bottom, cx, cy) {
    line(c, left, cy, right, cy, '#526d55', 1.8); line(c, cx, bottom, cx, top, '#526d55', 1.8);
    line(c, right - 7, cy - 4, right, cy, '#526d55', 1.8); line(c, right - 7, cy + 4, right, cy, '#526d55', 1.8);
    line(c, cx - 4, top + 7, cx, top, '#526d55', 1.8); line(c, cx + 4, top + 7, cx, top, '#526d55', 1.8);
    label(c, 'x', right + 12, cy, 16); label(c, 'y', cx, top - 13, 16);
  }
  function template(c, e) {
    c.save(); c.scale(e.w / 600, e.h / 440); c.fillStyle = '#ffffff'; c.fillRect(0, 0, 600, 440);
    if (e.template === 'cartesian' || e.template === 'cartesian-blank') {
      const cx = 300, cy = 220, step = 17;
      for (let i = -10; i <= 10; i++) {
        line(c, cx + i*step, 50, cx + i*step, 390, '#d9e2d7');
        line(c, 130, cy + i*step, 470, cy + i*step, '#d9e2d7');
        if (i && e.template === 'cartesian') { label(c, String(i), cx+i*step, cy+12, 9); label(c, String(-i), cx-13, cy+i*step, 9); }
        if (i && e.template === 'cartesian-blank') { line(c, cx+i*step, cy-3, cx+i*step, cy+3); line(c, cx-3, cy+i*step, cx+3, cy+i*step); }
      }
      axes(c, 115, 35, 485, 405, cx, cy);
      if (e.template === 'cartesian') label(c, '0', cx-10, cy+12, 9);
    } else if (e.template === 'trig') {
      const cx=100;
      axes(c,45,40,550,400,cx,220);
      for(let i=1;i<=4;i++)line(c,cx+i*90,215,cx+i*90,225);
    } else if (e.template === 'unit-circle') {
      axes(c, 65, 40, 535, 400, 300, 220);
      c.strokeStyle = e.color; c.lineWidth = 2; c.beginPath(); c.arc(300, 220, 155, 0, Math.PI*2); c.stroke();
    } else if (e.template === 'two-way') {
      for (let i = 0; i <= 4; i++) line(c, 20+i*140, 55, 20+i*140, 367, '#82977e', 1.3);
      for (let i = 0; i <= 4; i++) line(c, 20, 55+i*78, 580, 55+i*78, '#82977e', 1.3);
    } else if (e.template === 'venn') {
      c.strokeStyle = '#82977e'; c.lineWidth = 1.3; c.strokeRect(25, 30, 550, 380);
      c.strokeStyle = e.color; c.lineWidth = 2;
      for (const x of [230, 370]) { c.beginPath(); c.arc(x, 220, 135, 0, Math.PI*2); c.stroke(); }
    }
    c.restore();
  }
  const mathImages = new Map();
  let mathQueue = Promise.resolve();
  function mathTokens(text) {
    const result=[], pattern=/(\$\$[\s\S]*?\$\$|\$(?:\\.|[^$\n])+\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\])/g;
    let last=0;
    for(const match of text.matchAll(pattern)) {
      if(match.index>last)result.push({text:text.slice(last,match.index)});
      const raw=match[0], pair=raw.startsWith('$$')||raw.startsWith('\\');
      result.push({tex:raw.slice(pair?2:1,pair?-2:-1)});last=match.index+raw.length;
    }
    if(last<text.length)result.push({text:text.slice(last)});
    if(result.length===1 && result[0].text && /\\(?:frac|sqrt|sum|int|alpha|beta|theta|pi|begin|lim|sin|cos|tan)\b/.test(text))return [{tex:text}];
    return result;
  }
  function mathImage(tex, color, font) {
    const key=JSON.stringify([tex,color,font]);
    if(mathImages.has(key))return mathImages.get(key);
    const entry={ready:false,failed:false,w:font*4,h:font*1.5};mathImages.set(key,entry);
    // A bounded cache prevents thousands of live keystrokes retaining SVGs.
    if(mathImages.size>300)mathImages.delete(mathImages.keys().next().value);
    entry.promise=mathQueue=mathQueue.then(async()=>{
      try {
        await MathJax.startup.promise;
        const node=await MathJax.tex2svgPromise(tex,{display:false}), svg=node.querySelector('svg');
        if(!svg || svg.querySelector('[data-mjx-error]'))throw new Error('Invalid LaTeX');
        const view=svg.getAttribute('viewBox').split(/\s+/).map(Number);
        entry.h=Math.max(font,font*view[3]/1000);entry.w=Math.max(font/2,font*view[2]/1000);
        svg.setAttribute('xmlns','http://www.w3.org/2000/svg');svg.setAttribute('width',String(entry.w));svg.setAttribute('height',String(entry.h));
        svg.setAttribute('color',color);svg.style.color=color;
        // Only mathematical paths and local glyph references enter the image.
        for(const child of svg.querySelectorAll('a,script,foreignObject,image'))child.remove();
        for(const child of [svg,...svg.querySelectorAll('*')])for(const attr of [...child.attributes])if(attr.name.startsWith('on') || (attr.name.endsWith('href')&&!attr.value.startsWith('#')))child.removeAttribute(attr.name);
        const url=URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(svg)],{type:'image/svg+xml'}));
        entry.img=new Image();
        await new Promise((resolve,reject)=>{entry.img.onload=resolve;entry.img.onerror=reject;entry.img.src=url;}).finally(()=>URL.revokeObjectURL(url));
        entry.ready=true;
      } catch {entry.failed=true;}
      if(editing)sizeText(editing);
      updateMathPreview(); draw();
    });
    return entry;
  }
  function wrapped(c, e, measure=false) {
    if(!measure){c.beginPath();c.rect(0,0,e.w,e.h);c.clip();}
    const font=e.font||24,pad=e.type==='note'?15:3,max=e.w-pad*2,lineHeight=font*1.35;
    c.fillStyle=e.color;c.font=`${font}px system-ui, sans-serif`;c.textAlign='left';c.textBaseline='top';
    let x=pad,y=pad,rowHeight=lineHeight;
    const newline=()=>{y+=rowHeight;x=pad;rowHeight=lineHeight;};
    for(const token of mathTokens(e.text)) {
      if(token.tex!==undefined) {
        const entry=mathImage(token.tex,e.color,font),scale=Math.min(1,max/entry.w),w=entry.w*scale,h=entry.h*scale;
        if(x>pad && x+w>e.w-pad)newline();
        if(entry.ready){if(!measure)c.drawImage(entry.img,x,y,w,h);}else if(!measure)c.fillText(entry.failed?token.tex:'…',x,y,max);
        rowHeight=Math.max(rowHeight,h+4);x+=w+4;
      } else for(const char of token.text) {
        if(char==='\n'){newline();continue;}
        const w=c.measureText(char).width;if(x>pad && x+w>e.w-pad)newline();
        if(!measure)c.fillText(char,x,y);x+=w;
      }
    }
    return y+rowHeight+pad;
  }
  function render(c, e) {
    c.save(); c.translate(e.x, e.y); c.strokeStyle = e.color; c.fillStyle = e.color; c.lineWidth = e.width; c.lineJoin = 'round'; c.lineCap = 'round';
    if (e.type === 'path') {
      c.beginPath(); c.moveTo(...e.points[0]);
      // Midpoint curves soften pen jitter without changing the stored stroke or endpoints.
      for (let i=1; i<e.points.length-1; i++) {
        const p=e.points[i], next=e.points[i+1];
        c.quadraticCurveTo(p[0], p[1], (p[0]+next[0])/2, (p[1]+next[1])/2);
      }
      if (e.points.length > 1) c.lineTo(...e.points.at(-1));
      if (e.points.length === 1) { c.arc(e.points[0][0], e.points[0][1], e.width/2, 0, 2*Math.PI); c.fill(); } else c.stroke();
    } else if (e.type === 'line') line(c, 0, 0, e.dx, e.dy, e.color, e.width);
    else if (['rect', 'ellipse', 'polygon'].includes(e.type)) {
      c.beginPath();
      if (e.type === 'rect') c.rect(0, 0, e.w, e.h);
      else if (e.type === 'ellipse') c.ellipse(e.w/2, e.h/2, e.w/2, e.h/2, 0, 0, Math.PI*2);
      else {
        for (let i = 0; i < e.sides; i++) { const a = -Math.PI/2+i*Math.PI*2/e.sides, x = e.w/2+Math.cos(a)*e.w/2, y = e.h/2+Math.sin(a)*e.h/2; i ? c.lineTo(x,y) : c.moveTo(x,y); } c.closePath();
      }
      if (e.fill && e.fill !== 'transparent') { c.fillStyle = e.fill; c.fill(); } c.stroke();
    } else if (e.type === 'text' || e.type === 'note') {
      if (e.type === 'note') { c.fillStyle = '#fff2ac'; c.fillRect(0, 0, e.w, e.h); } wrapped(c, e);
    } else if (e.type === 'template') template(c, e);
    else if (e.type === 'image') {
      const entry = imageFor(e.asset_id);
      if (entry.ready) c.drawImage(entry.img, 0, 0, e.w, e.h);
      else { c.fillStyle = '#edf1ec'; c.fillRect(0,0,e.w,e.h); label(c, entry.failed ? 'Image unavailable' : 'Loading page…', e.w/2, e.h/2, 16); }
    }
    c.restore();
  }
  function bounds(e) {
    if (e.type === 'line') return {x: Math.min(e.x,e.x+e.dx), y: Math.min(e.y,e.y+e.dy), w: Math.max(1,Math.abs(e.dx)), h: Math.max(1,Math.abs(e.dy))};
    if (e.type === 'path') { const xs=e.points.map(p=>p[0]), ys=e.points.map(p=>p[1]), x=Math.min(...xs), y=Math.min(...ys); return {x:e.x+x,y:e.y+y,w:Math.max(1,Math.max(...xs)-x),h:Math.max(1,Math.max(...ys)-y)}; }
    return {x:e.x, y:e.y, w:e.w, h:e.h};
  }
  function draw() {
    const ratio = window.devicePixelRatio || 1; ctx.setTransform(ratio,0,0,ratio,0,0); ctx.clearRect(0,0,width,height);
    let grid = 24*zoom; while (grid < 14) grid *= 5;
    ctx.beginPath(); ctx.strokeStyle = '#d7e1d2'; ctx.lineWidth = .8;
    for (let x = ((pan.x%grid)+grid)%grid; x < width; x+=grid) { ctx.moveTo(x,0); ctx.lineTo(x,height); }
    for (let y = ((pan.y%grid)+grid)%grid; y < height; y+=grid) { ctx.moveTo(0,y); ctx.lineTo(width,y); } ctx.stroke();
    ctx.translate(pan.x,pan.y); ctx.scale(zoom,zoom);
    const localIds = new Set(previewElements().map(e=>e.id));
    const visible = new Map(objects);
    for (const [id, e] of remote) if (!pending.has(id) && !localIds.has(id)) visible.set(id, e);
    for (const e of previewElements()) visible.set(e.id,e);
    [...visible.values()].sort((a,b)=>(a.z||0)-(b.z||0)).forEach(e=>render(ctx,e));
    for (const id of selection) {
      const e=objects.get(id); if (!e) continue; const b=bounds(e), pad=5/zoom;
      ctx.strokeStyle='#377df0'; ctx.lineWidth=1.5/zoom; ctx.setLineDash([5/zoom,3/zoom]); ctx.strokeRect(b.x-pad,b.y-pad,b.w+pad*2,b.h+pad*2); ctx.setLineDash([]);
      if (selection.size === 1) { ctx.fillStyle='#fff'; ctx.fillRect(b.x+b.w-4/zoom,b.y+b.h-4/zoom,8/zoom,8/zoom); ctx.strokeRect(b.x+b.w-4/zoom,b.y+b.h-4/zoom,8/zoom,8/zoom); }
    }
    if (gesture?.kind === 'marquee') { const b=gesture.box; ctx.fillStyle='#377df018'; ctx.strokeStyle='#377df0'; ctx.lineWidth=1/zoom; ctx.fillRect(b.x,b.y,b.w,b.h); ctx.strokeRect(b.x,b.y,b.w,b.h); }
    $('[data-board-zoom]').textContent=`${Math.round(zoom*100)}%`; buttons();
  }
  function resize() { const r=stage.getBoundingClientRect(); width=r.width; height=r.height; canvas.width=Math.round(width*(devicePixelRatio||1)); canvas.height=Math.round(height*(devicePixelRatio||1)); draw(); }
  function hit(p) { return ordered().reverse().find(e=>{const b=bounds(e), pad=8/zoom; return p.x>=b.x-pad && p.x<=b.x+b.w+pad && p.y>=b.y-pad && p.y<=b.y+b.h+pad;}); }
  function fit() {
    if (!objects.size) { zoom=1; pan={x:110,y:190}; draw(); return; }
    const all=ordered().map(bounds), x=Math.min(...all.map(b=>b.x)), y=Math.min(...all.map(b=>b.y)), right=Math.max(...all.map(b=>b.x+b.w)), bottom=Math.max(...all.map(b=>b.y+b.h));
    zoom=Math.max(.002,Math.min(2,(width-100)/(right-x+80),(height-280)/(bottom-y+80)));
    pan={x:(width-(right-x)*zoom)/2-x*zoom,y:185+(height-280-(bottom-y)*zoom)/2-y*zoom}; draw();
  }
  function zoomAt(factor,x=width/2,y=height/2) { const previous=zoom; zoom=Math.max(.002,Math.min(4,zoom*factor)); pan={x:x-(x-pan.x)*zoom/previous,y:y-(y-pan.y)*zoom/previous}; draw(); }
  function sizeText(e) {
    ctx.save(); e.h=Math.min(10000,Math.max(e.type==='note'?180:60,wrapped(ctx,e,true)));ctx.restore();
  }
  function updateMathPreview() {
    const preview=$('[data-board-math-preview]');
    const hasMath=!!editing && mathTokens(editing.text).some(token=>token.tex!==undefined);
    preview.hidden=!hasMath;
    if (!hasMath) return;
    // This canvas lives in the local editor dialog and never enters shared board state.
    const output=$('[data-board-math-canvas]'), c=output.getContext('2d');
    const e={...editing,w:Math.max(160,Math.min(440,editing.w))};
    e.h=Math.min(2000,Math.max(60,wrapped(c,e,true)));
    const ratio=window.devicePixelRatio||1;
    output.width=Math.ceil(e.w*ratio); output.height=Math.ceil(e.h*ratio);
    output.style.width=`${e.w}px`; output.style.height=`${e.h}px`;
    c.scale(ratio,ratio); wrapped(c,e);
  }
  function textEditor(e) { finish(); editing=clone(e); dialog.returnValue='cancel'; $('[data-board-text-input]').value=e.text||''; $('[data-board-text-title]').textContent=e.type==='note'?'Sticky note':'Board text'; dialog.showModal(); $('[data-board-text-input]').focus(); updateMathPreview(); draw(); }
  $('[data-board-text-input]').addEventListener('input',event=>{ if (editing) { editing.text=event.target.value; sizeText(editing); updateMathPreview(); draw(); } });
  $('[data-board-text-cancel]').addEventListener('click',()=>dialog.close('cancel'));
  dialog.addEventListener('close',async()=>{
    const saved=editing,save=dialog.returnValue==='save';editing=null;
    updateMathPreview();
    if(save && saved?.text?.trim()){
      sizeText(saved);await mathQueue;sizeText(saved);selection.clear();selection.add(saved.id);committing.set(saved.id,clone(saved));apply([{id:saved.id,element:saved}]);
    }
    live();canvas.focus();draw();
  });
  canvas.addEventListener('pointerdown',event=>{
    if (!loaded || event.button>1) return;
    const p=position(event); event.preventDefault(); canvas.focus(); canvas.setPointerCapture(event.pointerId); settingsOpen(false);
    $$('details[open]').forEach(e=>{e.open=false;});
    if (space || event.button===1) { gesture={kind:'pan',screen:{x:event.clientX,y:event.clientY},pan:{...pan}}; return; }
    if (tool==='text' || tool==='note') { const e=base(tool,p,tool==='note'?240:320,tool==='note'?180:60); e.font=24; e.text=''; textEditor(e); return; }
    if (tool==='select') {
      if (event.shiftKey) { gesture={kind:'marquee',start:p,box:{...p,w:0,h:0}}; selection.clear(); draw(); return; }
      const current=selection.size===1 && objects.get([...selection][0]), b=current&&bounds(current);
      if (b && Math.hypot(p.x-b.x-b.w,p.y-b.y-b.h)<14/zoom) { gesture={kind:'resize',start:p,before:[clone(current)]}; return; }
      const e=hit(p);
      if (e) { if (!selection.has(e.id)) {selection.clear();selection.add(e.id);} gesture={kind:'move',start:p,before:[...selection].map(id=>clone(objects.get(id)))}; }
      else { selection.clear(); gesture={kind:'marquee',start:p,box:{...p,w:0,h:0}}; }
      draw(); return;
    }
    if (tool==='eraser') { const e=hit(p); if(e)apply([{id:e.id,element:null}]); gesture={kind:'erase'}; return; }
    let kind=tool, shape=$('[data-board-shape]').value;
    if (tool==='shape') kind=['rect','square'].includes(shape)?'rect':['ellipse','circle'].includes(shape)?'ellipse':'polygon';
    draft=base(kind,p);
    if (kind==='path') draft.points=[[0,0]];
    if (kind==='line') {draft.dx=0;draft.dy=0;}
    if (['rect','ellipse','polygon'].includes(kind)) draft.fill=$('[data-board-no-fill]').checked?'transparent':$('[data-board-fill]').value;
    if (kind==='polygon') draft.sides=+shape;
    gesture={kind:'draw',start:p,points:[[0,0]],square:['square','circle'].includes(shape)&&tool==='shape',last:p}; draw();
  });
  function updateGesture(p, shift) {
    const g=gesture; if (!g) return;
    if (g.kind==='marquee') {g.box={x:Math.min(g.start.x,p.x),y:Math.min(g.start.y,p.y),w:Math.abs(p.x-g.start.x),h:Math.abs(p.y-g.start.y)};draw();return;}
    if (g.kind==='move') {for (const before of g.before) {const e=clone(before);e.x+=p.x-g.start.x;e.y+=p.y-g.start.y;objects.set(e.id,e);}draw();return;}
    if (g.kind==='resize') {
      const e=clone(g.before[0]),b=bounds(e);let nw=Math.min(10000,Math.max(10,p.x-b.x)),nh=Math.min(10000,Math.max(10,p.y-b.y));
      if (shift && ['rect','ellipse','polygon'].includes(e.type)) nw=nh=Math.max(nw,nh);
      if(e.type==='path')e.points=e.points.map(([x,y])=>[x*nw/e.w,y*nh/e.h]);
      if(e.type==='line'){e.dx*=nw/e.w;e.dy*=nh/e.h;e.x=b.x+(e.dx<0?nw:0);e.y=b.y+(e.dy<0?nh:0);}
      if(['text','note'].includes(e.type))e.font=Math.max(12,Math.min(120,e.font*Math.min(nw/e.w,nh/e.h)));
      e.w=nw;e.h=nh;objects.set(e.id,e);draw();return;
    }
    if (!draft) return; g.last=p;
    let dx=Math.max(-9900,Math.min(9900,p.x-g.start.x)),dy=Math.max(-9900,Math.min(9900,p.y-g.start.y));
    if (draft.type==='path') {
      const last=g.points.at(-1);if(g.points.length<3999 && Math.hypot(dx-last[0],dy-last[1])>1/zoom)g.points.push([dx,dy]);
      if (shift) { if(Math.abs(dy)<Math.abs(dx)*.2)dy=0;else if(Math.abs(dx)<Math.abs(dy)*.2)dx=0;draft.points=[[0,0],[dx,dy]]; }
      else draft.points=g.points.map(point=>[...point]);
    } else if (draft.type==='line') {
      if (shift) { const angle=Math.round(Math.atan2(dy,dx)/(Math.PI/4))*Math.PI/4,length=Math.min(Math.hypot(dx,dy),9900/Math.max(Math.abs(Math.cos(angle)),Math.abs(Math.sin(angle))));dx=Math.cos(angle)*length;dy=Math.sin(angle)*length;if(Math.abs(dx)<1e-9)dx=0;if(Math.abs(dy)<1e-9)dy=0; }
      draft.dx=dx;draft.dy=dy;draft.w=Math.max(1,Math.abs(dx));draft.h=Math.max(1,Math.abs(dy));
    } else {
      if (shift || g.square) { const size=Math.max(Math.abs(dx),Math.abs(dy));dx=(dx<0?-1:1)*size;dy=(dy<0?-1:1)*size; }
      draft.x=Math.min(g.start.x,g.start.x+dx);draft.y=Math.min(g.start.y,g.start.y+dy);draft.w=Math.max(1,Math.abs(dx));draft.h=Math.max(1,Math.abs(dy));
    }
    draw();
  }
  canvas.addEventListener('pointermove',event=>{
    if (!gesture) return; const p=position(event),g=gesture;
    if(g.kind==='pan'){pan={x:g.pan.x+event.clientX-g.screen.x,y:g.pan.y+event.clientY-g.screen.y};draw();return;}
    if(g.kind==='erase'){const e=hit(p);if(e)apply([{id:e.id,element:null}]);return;}
    updateGesture(p,event.shiftKey);
  });
  function finish(cancel=false) {
    if (!gesture) return;
    if (gesture.before) {
      const changes=gesture.before.map(before=>({id:before.id,element:clone(objects.get(before.id))}));
      gesture.before.forEach(before=>objects.set(before.id,before)); if(!cancel){for(const change of changes)committing.set(change.id,clone(change.element));apply(changes);}
    }
    if (gesture.kind==='marquee' && !cancel) { const b=gesture.box; for(const e of ordered()){const a=bounds(e);if(a.x<=b.x+b.w && a.x+a.w>=b.x && a.y<=b.y+b.h && a.y+a.h>=b.y)selection.add(e.id);} }
    if (draft && !cancel) {
      if(draft.type==='path'){const b=bounds(draft),dx=b.x-draft.x,dy=b.y-draft.y;draft.x=b.x;draft.y=b.y;draft.points=draft.points.map(p=>[p[0]-dx,p[1]-dy]);draft.w=b.w;draft.h=b.h;}
      committing.set(draft.id,clone(draft));apply([{id:draft.id,element:draft}]);
    }
    draft=null;gesture=null;draw();live();
  }
  canvas.addEventListener('pointerup',()=>finish());canvas.addEventListener('pointercancel',()=>finish(true));
  canvas.addEventListener('dblclick',event=>{if(!loaded)return;const e=hit(position(event));if(e&&['text','note'].includes(e.type))textEditor(e);});
  canvas.addEventListener('wheel',event=>{event.preventDefault();const r=canvas.getBoundingClientRect();if(event.ctrlKey||event.metaKey)zoomAt(Math.exp(-event.deltaY*.005),event.clientX-r.left,event.clientY-r.top);else{pan.x-=event.deltaX;pan.y-=event.deltaY;draw();}},{passive:false});
  $$('[data-board-tool]').forEach(b=>b.addEventListener('click',()=>choose(b.dataset.boardTool)));
  $('[data-board-settings-close]').addEventListener('click',()=>{settingsOpen(false);canvas.focus();});
  function changeStyle(field,value) { const changes=[...selection].map(id=>clone(objects.get(id))).filter(Boolean).filter(e=>field!=='fill'||['rect','ellipse','polygon'].includes(e.type)).map(e=>({id:e.id,element:{...e,[field]:value}}));apply(changes); }
  function refreshPresets() {
    $('[data-board-width-value]').textContent=`${$('[data-board-width]').value} px`;
    $$('[data-board-width-preset]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.boardWidthPreset===$('[data-board-width]').value)));
    $$('[data-ink-swatch]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.inkSwatch===$('[data-board-color]').value)));
    $$('[data-fill-swatch]').forEach(b=>b.setAttribute('aria-pressed',String(!$('[data-board-no-fill]').checked&&b.dataset.fillSwatch===$('[data-board-fill]').value)));
  }
  function swatches(container,colors,fill=false,personal=false) {
    container.replaceChildren();
    for (const color of colors) {
      const button=document.createElement('button');button.type='button';button.style.backgroundColor=color;button.title=color;button.setAttribute('aria-label',`${fill?'Fill':'Ink'} ${color}`);button.dataset[fill?'fillSwatch':'inkSwatch']=color;
      button.addEventListener('click',()=>{if(fill){$('[data-board-no-fill]').checked=false;$('[data-board-fill]').value=color;changeStyle('fill',color);}else{$('[data-board-color]').value=color;changeStyle('color',color);}refreshPresets();});
      if(personal)button.addEventListener('contextmenu',event=>{event.preventDefault();personalColors=personalColors.filter(c=>c!==color);savePalette();});
      container.append(button);
    }
  }
  function savePalette() {try{localStorage.setItem(paletteKey,JSON.stringify(personalColors));}catch{/* Keep usable for this session. */}swatches($('[data-board-personal]'),personalColors,false,true);refreshPresets();}
  swatches($('[data-board-swatches]'),presetColors);swatches($('[data-board-fill-swatches]'),presetColors,true);savePalette();
  $('[data-board-save-color]').addEventListener('click',()=>{const color=$('[data-board-color]').value;if(!personalColors.includes(color)){if(personalColors.length===16)personalColors.shift();personalColors.push(color);savePalette();}});
  $('[data-board-color]').addEventListener('input',event=>{refreshPresets();});
  $('[data-board-color]').addEventListener('change',event=>changeStyle('color',event.target.value));
  $('[data-board-width]').addEventListener('input',refreshPresets);
  $('[data-board-width]').addEventListener('change',event=>changeStyle('width',+event.target.value));
  $$('[data-board-width-preset]').forEach(b=>b.addEventListener('click',()=>{$('[data-board-width]').value=b.dataset.boardWidthPreset;changeStyle('width',+b.dataset.boardWidthPreset);refreshPresets();}));
  $('[data-board-fill]').addEventListener('change',event=>{$('[data-board-no-fill]').checked=false;changeStyle('fill',event.target.value);refreshPresets();});
  $('[data-board-no-fill]').addEventListener('change',event=>{changeStyle('fill',event.target.checked?'transparent':$('[data-board-fill]').value);refreshPresets();});
  function addTemplate(kind) {if(!loaded)return;const p=center(),e=base('template',{x:p.x-300,y:p.y-220},600,440);e.template=kind;e.color='#243d35';apply([{id:e.id,element:e}]);choose('select');selection.add(e.id);draw();$('.board-templates').open=false;}
  $$('[data-board-template]').forEach(b=>b.addEventListener('click',()=>addTemplate(b.dataset.boardTemplate)));
  async function importFiles(files,at=center()) {
    if(!loaded||importing)return;importing=true;
    try {
      let offset=0;
      for(const file of files){
        if(file.size>8*1024*1024)throw new Error('Each PDF or image must be 8 MB or smaller.');
        setStatus(`Importing ${file.name}…`);const form=new FormData();form.append('file',file);
        const response=await fetch(root.dataset.assets,{method:'POST',headers:{'X-CSRFToken':root.dataset.token},body:form});if(!response.ok)throw new Error(await responseError(response));
        const {assets}=await response.json(),changes=[];let y=at.y;
        for(const asset of assets){const w=Math.min(640,asset.width),h=w*asset.height/asset.width,e=base('image',{x:at.x+offset,y},w,h);e.asset_id=asset.id;changes.push({id:e.id,element:e});y+=h+35;}
        apply(changes);offset+=700;
      }
      choose('select');fit();
    }catch(error){setStatus(error.message,true);}finally{importing=false;$('[data-board-upload]').value='';}
  }
  $('[data-board-upload]').addEventListener('change',event=>importFiles([...event.target.files]));
  stage.addEventListener('dragenter',event=>{if(event.dataTransfer.types.includes('Files')){event.preventDefault();dragDepth++;$('[data-board-drop]').hidden=false;}});
  stage.addEventListener('dragover',event=>{event.preventDefault();event.dataTransfer.dropEffect='copy';});
  stage.addEventListener('dragleave',()=>{if(--dragDepth<=0){dragDepth=0;$('[data-board-drop]').hidden=true;}});
  stage.addEventListener('drop',event=>{event.preventDefault();dragDepth=0;$('[data-board-drop]').hidden=true;importFiles([...event.dataTransfer.files],position(event));});
  function duplicate(source=[...selection].map(id=>objects.get(id))) {if(!source.length)return;selection.clear();const changes=source.map(original=>{const e=clone(original);e.id=uid();e.x+=25;e.y+=25;e.z=Date.now()+Math.random();selection.add(e.id);return{id:e.id,element:e};});apply(changes);}
  async function exportPng() {
    // Initialize all formula images, then wait so downloaded boards include them.
    for(const e of ordered())if(['text','note'].includes(e.type))for(const token of mathTokens(e.text))if(token.tex!==undefined)mathImage(token.tex,e.color,e.font||24);
    await mathQueue;
    if(!objects.size){setStatus('Add some working before downloading.');return;}
    const button=actionButton('export');button.disabled=true;setStatus('Preparing full-board image…');
    try {
      const all=ordered();for(const e of all)if(e.type==='image')imageFor(e.asset_id);
      await Promise.all([...images.values()].map(entry=>entry.ready||entry.failed?Promise.resolve():new Promise(resolve=>{entry.img.addEventListener('load',resolve,{once:true});entry.img.addEventListener('error',resolve,{once:true});})));
      if(all.some(e=>e.type==='image'&&!imageFor(e.asset_id).ready))throw new Error('An uploaded page could not be loaded. Try again before exporting.');
      const boxes=all.map(bounds),left=Math.floor(Math.min(...boxes.map(b=>b.x))-40),top=Math.floor(Math.min(...boxes.map(b=>b.y))-40),w=Math.ceil(Math.max(...boxes.map(b=>b.x+b.w))-left+40),h=Math.ceil(Math.max(...boxes.map(b=>b.y+b.h))-top+40);
      const scale=Math.min(2,16000/w,16000/h,Math.sqrt(32000000/(w*h))),output=document.createElement('canvas');output.width=Math.ceil(w*scale);output.height=Math.ceil(h*scale);
      const c=output.getContext('2d');c.fillStyle='#fff';c.fillRect(0,0,output.width,output.height);c.scale(scale,scale);c.translate(-left,-top);all.forEach(e=>render(c,e));
      const blob=await new Promise(resolve=>output.toBlob(resolve,'image/png'));if(!blob)throw new Error('The image is too large for this browser.');
      const safe=value=>value.replace(/[^\p{L}\p{N}_ -]/gu,'').trim().slice(0,80);
      const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`${safe(root.dataset.date)}-${safe(root.dataset.student)||'student'}-${safe(root.dataset.name)||'lesson'}-whiteboard.png`;link.click();setTimeout(()=>URL.revokeObjectURL(url),30000);setStatus('Full-board PNG downloaded');
    }catch(error){setStatus(error.message,true);}finally{button.disabled=false;}
  }
  $$('[data-board-action]').forEach(b=>b.addEventListener('click',()=>{
    const action=b.dataset.boardAction;
    if(action==='export')return exportPng();
    if(action==='fullscreen'){if(document.fullscreenElement)document.exitFullscreen();else root.requestFullscreen?.().catch(()=>setStatus('Fullscreen is unavailable in this browser.'));return;}
    if(action==='zoom-in')return zoomAt(1.2);if(action==='zoom-out')return zoomAt(1/1.2);if(action==='fit')return fit();if(!loaded)return;
    if(action==='delete'&&selection.size){apply([...selection].map(id=>({id,element:null})));selection.clear();draw();}
    if(action==='duplicate')duplicate();
    if(action==='back'&&selection.size){const bottom=Math.min(0,...ordered().map(v=>v.z||0));apply([...selection].map((id,i)=>({id,element:{...objects.get(id),z:bottom-selection.size+i}})));}
    if(action==='undo'&&undo.length){const history=undo.pop();redo.push(history);apply(history.map(h=>({id:h.id,element:h.before})),false);}
    if(action==='redo'&&redo.length){const history=redo.pop();undo.push(history);apply(history.map(h=>({id:h.id,element:h.after})),false);}
  }));
  document.addEventListener('keydown',event=>{
    if(!loaded)return;
    if(event.key==='Escape' && event.target.closest('dialog:not([data-board-text-dialog])'))return;
    if(event.key==='Escape'){event.preventDefault();if(dialog.open)dialog.close('cancel');finish(true);choose('select',false);$$('details[open]').forEach(e=>{e.open=false;});canvas.focus();return;}
    if(event.target.closest('input,textarea,select,dialog'))return;
    if(event.code==='Space'){event.preventDefault();space=true;}
    if(event.key==='Shift'&&gesture?.last)updateGesture(gesture.last,true);
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){event.preventDefault();actionButton(event.shiftKey?'redo':'undo').click();return;}
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='c'&&selection.size){copied=[...selection].map(id=>clone(objects.get(id)));return;}
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='v'&&copied.length){event.preventDefault();duplicate(copied);return;}
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='d'){event.preventDefault();duplicate();return;}
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='a'){event.preventDefault();choose('select',false);for(const id of objects.keys())selection.add(id);draw();return;}
    if(event.key==='Delete'||event.key==='Backspace'){if(selection.size){event.preventDefault();actionButton('delete').click();}return;}
    if(event.key.startsWith('Arrow')&&selection.size){event.preventDefault();const step=event.shiftKey?10:1,dx=event.key==='ArrowLeft'?-step:event.key==='ArrowRight'?step:0,dy=event.key==='ArrowUp'?-step:event.key==='ArrowDown'?step:0;apply([...selection].map(id=>{const e=objects.get(id);return{id,element:{...e,x:e.x+dx,y:e.y+dy}};}));return;}
    if(!event.ctrlKey&&!event.metaKey&&!event.altKey){const keys={v:'select',p:'path',e:'eraser',t:'text',n:'note',l:'line',r:'rect',o:'ellipse',s:'shape'};if(keys[event.key.toLowerCase()]){event.preventDefault();choose(keys[event.key.toLowerCase()]);}if(['+','='].includes(event.key))zoomAt(1.2);if(event.key==='-')zoomAt(1/1.2);if(event.key.toLowerCase()==='f')fit();}
  });
  document.addEventListener('keyup',event=>{if(event.code==='Space')space=false;if(event.key==='Shift'&&gesture?.last)updateGesture(gesture.last,false);});
  window.addEventListener('blur',()=>{space=false;finish();});
  window.addEventListener('beforeunload',event=>{if(pending.size){event.preventDefault();event.returnValue='';}});
  window.addEventListener('pagehide',leave);
  window.addEventListener('pageshow',()=>{leaving=false;lastHeartbeat=0;live();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)leave();else{leaving=false;lastHeartbeat=0;live();poll();}});
  new ResizeObserver(resize).observe(stage);choose('select',false);poll();setInterval(poll,700);setInterval(()=>{if(previewElements().length || Date.now()-lastLiveReceived>=240)live();},120);
})();
