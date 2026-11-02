'use strict';
const PAGE_INFO = {
  overview: ['总览', 'DASHBOARD', '设备状态、采样率和常用控制。'],
  input: ['输入', 'INPUT CHANNELS', '麦克风、乐器与 OTG 输入。'],
  mixer: ['混音', 'MIX A / B / C', '为三组输出分别设置电平、声像和监听。'],
  output: ['输出与回环', 'OUTPUT & LOOPBACK', '选择监听、OTG 和电脑录音的信号来源。'],
  dynamics: ['动态处理', 'DYNAMICS', '压缩、扩展与闪避。'],
  eq: ['均衡器', 'PARAMETRIC EQ', '调整频率、增益、Q 值和滤波类型。'],
  fx: ['效果', 'DELAY & REVERB', '输入发送、延迟、混响与效果返回。'],
  device: ['设备', 'DEVICE SETTINGS', '工作模式、屏幕、输出记忆和按键功能。'],
  profiles: ['配置', 'PRESETS', '保存、导出和恢复你的控制台配置。']
};
const $ = id => document.getElementById(id);
const PAGE_ICONS = {overview:'meter-bars',input:'input-gain',mixer:'mixer',output:'routing',dynamics:'dynamics',eq:'equalizer',fx:'effects',device:'settings',profiles:'profiles'};
function icon(name, className = '') {return el('img', {class:'app-icon ' + className, src:'/assets/icons/' + name + '.svg', alt:'', 'aria-hidden':'true'});}
function svgNode(tag, attrs) {const n = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [k,v] of Object.entries(attrs)) n.setAttribute(k,v); return n;}
const NAV_GROUPS = [['input','输入','input-gain'],['mixer','混音','mixer'],['output','输出','routing'],['eq dynamics fx','处理','equalizer'],['device profiles overview','设置','settings']];
const navMemory = {处理:'eq',设置:'device'};
let mixerFilter = 'all';
const views = new Map(), mixerViews = [], groupViews = [], eqViews = [];
let definitions = [], byId = new Map(), sources = [], state = null, currentPage = 'overview';
let busy = new Set(), mixerBusy = false, activeBus = 'A', activeEq = null, linkedEq = false;
let refreshing = false, queue = Promise.resolve(), toastTimer, built = false, serverOnline = false;
let selectedBank = null, changingMode = false;
let refreshGeneration = 0, editRevision = 0;
const writeQueue = new LatestWriteQueue();
const controlDrafts = new Map(), mixerDrafts = new Map();
let mixerRequests = 0;
function editContext() {
  return {expectedMode: state.mode, outputBank: state.mode === 'pro' ? state.device.viewBank : null,
    selection: selectedBank, ...(state.mode === 'hifi' ? {expectedPreset: state.device.values['hifi.preset']?.value} : {})};
}
function checkEditContext(context) {
  if (state.mode !== context.expectedMode || ('expectedPreset' in context && state.device.values['hifi.preset']?.value !== context.expectedPreset))
    throw new Error('工作模式或 EQ 槽位已改变，旧页面的调整未发送');
}
function invalidateRefresh() {refreshGeneration++; refreshing = false;}
function acceptWriteResult(result, context) {
  invalidateRefresh();
  if (result.state && selectedBank === context.selection && state.mode === context.expectedMode) {
    state = result.state; serverOnline = true;
  }
}
function mixerStrip(bus, source) {return mixerDrafts.get(bus + ':' + source)?.strip || state.mixer.buses[bus][source];}
function draftMixer(strip, change) {
  const next = structuredClone(strip);
  if ('link' in change) {
    next.link = change.link;
    if (next.link) {
      for (const key of ['gain','mute','solo','polarity']) next.right[key] = next.left[key];
      next.left.pan = 0; next.right.pan = 100;
    }
  }
  for (const side of ['left','right']) if (change[side]) {
    Object.assign(next[side], change[side]);
    if (next.link) for (const [key,value] of Object.entries(change[side])) if (key !== 'pan') next[side === 'left' ? 'right' : 'left'][key] = value;
  }
  return next;
}
const meterPeaks = new Map();
const meterClips = new Set();
let meterMode = null;
const METER_SCALE = [[-90,0],[-60,0],[-48,.1],[-36,.2],[-30,.3],[-24,.44],[-18,.58],[-12,.72],[-6,.86],[0,1]];
const FADER_SCALE = [[-90,0],[-60,.06],[-48,.15],[-36,.24],[-30,.33],[-24,.44],[-18,.55],[-12,.66],[-6,.78],[0,.88],[12,1]];
function scalePosition(value, points = METER_SCALE) {if(value<=points[0][0])return points[0][1];for(let i=1;i<points.length;i++){const [hi,y1]=points[i],[lo,y0]=points[i-1];if(value<=hi)return y0+(value-lo)/(hi-lo)*(y1-y0);}return points.at(-1)[1];}
function faderDecibels(position){const value=Number(position)/1000;for(let i=1;i<FADER_SCALE.length;i++){const [db1,hi]=FADER_SCALE[i],[db0,lo]=FADER_SCALE[i-1];if(value<=hi)return Math.round((db0+(value-lo)/(hi-lo)*(db1-db0))*10)/10;}return 12;}
function showFader(range,value){range.value=Math.round(scalePosition(value,FADER_SCALE)*1000);range.setAttribute('aria-valuetext',format(value,'dB'));range.style.setProperty('--fader-position',(100-scalePosition(value,FADER_SCALE)*100)+'%');const well=range.closest('.fader-well');if(well){const zero=1-scalePosition(0,FADER_SCALE);well.style.setProperty('--zero-position',(zero*100)+'%');well.style.setProperty('--zero-offset',((.5-zero)*31)+'px');}}
function channelMeter(signal, label) {
  const scale=el('div',{class:'meter-legend','aria-hidden':'true'});
  for(const db of [0,-6,-12,-18,-24,-30,-36,-48,-60]) {const tick=el('span',{text:db===0?'0':String(-db)});tick.style.top=((1-scalePosition(db))*100)+'%';scale.append(tick);}
  const peak=button('—',()=>{meterPeaks.delete(signal);meterClips.delete(signal);},'meter-peak');peak.title='点击重置峰值和削波指示';peak.setAttribute('aria-label',label+'峰值，点击重置');
  const rail=el('div',{class:'level-meter-track',role:'meter','aria-label':label,'aria-valuemin':-60,'aria-valuemax':0},el('div',{class:'level-meter-fill'}));
  return el('div',{class:'channel-meter','data-signal':signal},scale,el('div',{class:'meter-column'},peak,rail));
}
const meterAnimations = new Map();
let meterViews = [], meterLastPaint = 0, meterNextPaint = 0, meterMetaPaint = 0;
let meterFpsStart = 0, meterFpsFrames = 0, meterSnapshotTime = 0, meterSnapshotReceived = 0;
function cacheMeterViews(){
  meterViews=[...document.querySelectorAll('.channel-meter')].map(node=>({node,signal:node.dataset.signal,rail:node.querySelector('.level-meter-track'),fill:node.querySelector('.level-meter-fill'),peak:node.querySelector('.meter-peak'),page:node.closest('.page')?.id?.replace('page-',''),input:node.closest('#page-input')!==null,strip:false}));
  // Mixer strips use the same signal names as the HID meter decoder.  The
  // first implementation only registered IN 1/2 through data-input and
  // marked every computer playback strip unavailable, so Playback 1/2 could
  // never move even when 4909/490a reports were arriving.
  for(const node of document.querySelectorAll('.strip-meter[data-input],.strip-meter[data-signal]')){
    const signal=node.dataset.signal || 'input:'+node.dataset.input;
    meterViews.push({node,signal,fill:node.querySelector('.meter-fill'),strip:true});
  }
}
function renderMeters() {
  if(!state)return;
  ingestMeterSnapshot({time:state.time,mode:state.mode,connected:state.device.connected,meters:state.device.meters,outputMeters:state.device.outputMeters});
}
function ingestMeterSnapshot(snapshot){
  if(!state||snapshot.mode!==state.mode||snapshot.time<meterSnapshotTime)return;
  if(meterMode!==snapshot.mode){meterMode=snapshot.mode;meterPeaks.clear();meterClips.clear();meterAnimations.clear();}
  meterSnapshotTime=snapshot.time;meterSnapshotReceived=performance.now();
  state.time=snapshot.time;state.device.meters=snapshot.connected?snapshot.meters||{}:{};state.device.outputMeters=snapshot.connected?snapshot.outputMeters||{}:{};
  for(const signal of new Set(['input:1','input:2','input:3','output',...meterViews.map(view=>view.signal)])){
    const entry=signal.startsWith('input:')?state.device.meters[signal.slice(6)]:state.device.outputMeters[signal];
    const fresh=entry&&Number.isFinite(entry.value)&&snapshot.time-entry.time<2&&snapshot.connected;
    const motion=meterAnimations.get(signal)||{display:0,target:0,value:null,report:0,fresh:false};
    motion.fresh=!!fresh;motion.value=fresh?Math.max(-90,Math.min(0,entry.value)):null;motion.report=fresh?entry.time:0;motion.target=fresh?scalePosition(motion.value):0;
    if(fresh)meterPeaks.set(signal,Math.max(motion.value,meterPeaks.get(signal)??-90));
    if(fresh&&entry.clip)meterClips.add(signal);
    meterAnimations.set(signal,motion);
  }
}
function animateMeters(timestamp){
  requestAnimationFrame(animateMeters);
  if(document.hidden||!state||!built){meterLastPaint=timestamp;meterNextPaint=timestamp;return;}
  if(timestamp<meterNextPaint-1)return;
  meterNextPaint=Math.max(meterNextPaint+1000/60,timestamp-1);
  const dt=Math.min(100,Math.max(1,timestamp-(meterLastPaint||timestamp-16.67)));meterLastPaint=timestamp;
  const now=meterSnapshotTime+(timestamp-meterSnapshotReceived)/1000,updateMeta=timestamp-meterMetaPaint>100;
  for(const motion of meterAnimations.values()){
    motion.fresh=motion.fresh&&serverOnline&&now-motion.report<2;
    const target=motion.fresh?motion.target:0;
    // Fast attack retains peaks; a 120 ms release avoids coarse USB steps.
    const tau=target>motion.display?18:120;
    motion.display+=(target-motion.display)*(1-Math.exp(-dt/tau));
    if(Math.abs(target-motion.display)<.0001)motion.display=target;
  }
  for(const view of meterViews){
    const visible=view.strip?currentPage==='mixer'&&state.mode==='pro':view.page?currentPage===view.page:innerWidth>800;
    if(!visible)continue;
    const motion=meterAnimations.get(view.signal);if(!motion)continue;
    view.fill.style.clipPath='inset('+(100-motion.display*100).toFixed(3)+'% 0 0 0)';
    if(updateMeta){
      view.node.classList.toggle(view.strip?'unavailable':'no-signal-data',!motion.fresh);
      if(view.peak){view.peak.classList.toggle('clipped',motion.fresh&&meterClips.has(view.signal));view.peak.textContent=motion.fresh?(meterClips.has(view.signal)?'CLIP':format(meterPeaks.get(view.signal))):'—';if(motion.fresh){view.rail.setAttribute('aria-valuenow',motion.value);view.rail.setAttribute('aria-valuetext',format(motion.value,'dBFS'));}else{view.rail.removeAttribute('aria-valuenow');view.rail.setAttribute('aria-valuetext','等待设备电平');}}
      else view.node.title=motion.fresh?format(motion.value,'dBFS')+' · 设备电平':'等待设备电平';
    }
  }
  if(updateMeta)meterMetaPaint=timestamp;
  if(!meterFpsStart)meterFpsStart=timestamp;
  meterFpsFrames++;
  if(timestamp-meterFpsStart>=1000){document.documentElement.dataset.meterFps=String(Math.round(meterFpsFrames*1000/(timestamp-meterFpsStart)));document.documentElement.dataset.meterFrames=String(Number(document.documentElement.dataset.meterFrames||0)+meterFpsFrames);meterFpsStart=timestamp;meterFpsFrames=0;}
}

let referenceCurves = {};
try {
  const saved = JSON.parse(localStorage.getItem('kb22-reference-curves') || '{}');
  for (const target of ['in1', 'in2', 'outL', 'outR', 'hifiL', 'hifiR']) {
    if (saved?.[target]) referenceCurves[target] = validateCurves(saved[target]);
  }
} catch {referenceCurves = {};}
let token = localStorage.getItem('kb22-token') || '';
const initialHash = new URLSearchParams(location.hash.slice(1));
const incomingToken = initialHash.get('token') || new URLSearchParams(location.search).get('token');
if (incomingToken) {token = incomingToken; localStorage.setItem('kb22-token', token); history.replaceState(null, '', location.pathname);}
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') n.className = value;
    else if (key === 'text') n.textContent = value;
    else if (key.startsWith('on')) n.addEventListener(key.slice(2), value);
    else if (key === 'checked' || key === 'disabled' || key === 'hidden') n[key] = value;
    else n.setAttribute(key, value);
  }
  children.flat().forEach(child => {if (child != null) n.append(child.nodeType ? child : document.createTextNode(String(child)));});
  return n;
}
function button(text, action, className = '') {return el('button', {type: 'button', class: className, text, onclick: action});}
function toast(text, error = false) {
  $('toast').textContent = text; $('toast').className = 'toast' + (error ? ' error' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.add('hidden'), error ? 6500 : 3500);
}
function showToken() {if (!$('token-dialog').open) $('token-dialog').showModal();}
function confirmAction(title, text) {
  $('confirm-title').textContent = title; $('confirm-text').textContent = text;
  const dialog = $('confirm-dialog'); dialog.returnValue = ''; dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {once: true}));
}
function askName(title, initial, submitLabel = '保存') {
  const input = el('input', {type: 'text', value: initial, maxlength: 80, required: '', 'aria-label': title});
  const form = el('form', {method: 'dialog'}, el('h2', {text: title}), input,
    el('div', {class: 'dialog-actions'}, el('button', {value: 'cancel', formnovalidate: '', text: '取消'}), el('button', {value: 'save', class: 'primary', text: submitLabel})));
  const dialog = el('dialog', {}, form); document.body.append(dialog); dialog.showModal(); input.select();
  return new Promise(resolve => dialog.addEventListener('close', () => {const result = dialog.returnValue === 'save' ? input.value.trim() : null; dialog.remove(); resolve(result);}, {once: true}));
}
async function api(path, payload) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 60000);
  try {
    const response = await fetch('/api/' + path, {method: payload === undefined ? 'GET' : 'POST',
      headers: {'Content-Type': 'application/json', 'X-KB22-Token': token},
      body: payload === undefined ? undefined : JSON.stringify(payload), signal: controller.signal});
    const data = await response.json();
    if (response.status === 401) showToken();
    if (!response.ok) throw new Error(data.error || '请求失败');
    return data;
  } catch (error) {if (error.name === 'AbortError') throw new Error('请求超时，请刷新并核对设备状态'); throw error;}
  finally {clearTimeout(timer);}
}
function format(value, unit = '') {
  if (value == null) return '—';
  const number = Number(value); return (Number.isInteger(number) ? String(number) : String(Number(number.toFixed(3)))) + (unit ? ' ' + unit : '');
}
function rateText(value) {return value ? format(value / 1000, 'kHz') : '—';}
function effectiveMode() {return state?.mode || null;}
function applicable(c) {return c.modes.includes(effectiveMode());}
function restriction(c) {
  if (!serverOnline || !state?.device?.connected) return '设备未连接';
  if (!state.mode) return '等待确认工作模式';
  if (state.device.syncing || changingMode) return '正在同步设备';
  if (c.banked && state.mode === 'pro' && selectedBank !== null && state.device.viewBank !== selectedBank) return '正在切换输出记忆槽';
  if (state.mode && !c.modes.includes(state.mode)) return '当前工作模式不支持';
  const rate = state.audio?.sampleRate?.value;
  if (rate && c.rateLimit && rate > c.rateLimit) return '需要 ≤ ' + rateText(c.rateLimit);
  return c.readonly ? '只读' : '';
}
function sourceText(entry) {
  if (!entry || entry.value == null) return entry?.mixed ? '左右声道不同' : '尚未读取';
  if (entry.source === 'device') return entry.complete === false ? '设备回读 · 单侧' : '设备回读';
  if (entry.source === 'sent') return '已发送';
  return '电脑保存值';
}
function registerView(id, view) {if (!views.has(id)) views.set(id, []); views.get(id).push(view);}
function control(c, presentation = '') {
  if (typeof presentation !== 'string') presentation = '';
  const row = el('div', {class: 'control-row', 'data-control': c.id});
  if (presentation) row.classList.add(presentation);
  const status = el('span', {class: 'control-status', text: '尚未读取'});
  const title = el('span', {class: 'control-label', text: c.label});
  const top = el('div', {class: 'control-top'}, title, status);
  const limits = el('p', {class: 'restriction hidden'});
  const widgets = []; let setter;
  row.append(top);
  const send = value => sendControl(c, value);
  if (c.kind === 'toggle') {
    row.classList.add('toggle-row');
    const widget = el('button', {type: 'button', class: 'toggle', role: 'switch', 'aria-checked': 'mixed', 'aria-label': c.label,
      onclick: () => send(widget.getAttribute('aria-checked') !== 'true')});
    widgets.push(widget); row.append(widget);
    setter = value => {widget.setAttribute('aria-checked', value == null ? 'mixed' : String(!!value)); widget.textContent = widget.dataset.caption || (value == null ? '—' : value ? '开' : '关');};
  } else if (c.kind === 'select') {
    const widget = el('select', {'aria-label': c.label}, el('option', {value: '', text: '选择…'}), c.choices.map(o => el('option', {value: o.value, text: o.label})));
    widget.addEventListener('change', () => {if (widget.value !== '') send(Number(widget.value));});
    widgets.push(widget); row.append(widget); setter = value => {
      widget.querySelector('.unknown-option')?.remove();
      if (value != null && !c.choices.some(o => o.value === value)) widget.append(el('option', {value, disabled: true, class: 'unknown-option', text: '设备值 ' + value + '（未识别）'}));
      widget.value = value == null ? '' : String(value);
    };
  } else if (c.kind === 'bitmask') {
    const wrap = el('div', {class: 'choice-checks'});
    c.choices.forEach(option => {
      const check = el('input', {type: 'checkbox', value: option.value}); widgets.push(check);
      check.addEventListener('change', () => send(widgets.reduce((mask, w) => mask | (w.checked ? Number(w.value) : 0), 0)));
      wrap.append(el('label', {}, check, option.label));
    }); row.append(wrap); setter = value => widgets.forEach(w => {w.checked = value != null && !!(value & Number(w.value));});
  } else {
    const range = el('input', {type: 'range', min: c.min, max: c.max, step: c.step || 'any', 'aria-label': c.label + '滑杆'});
    const number = el('input', {type: 'number', min: c.min, max: c.max, step: c.step || 'any', placeholder: '—', inputmode: 'decimal', 'aria-label': c.label});
    let showDial = () => {};
    range.addEventListener('input', () => {number.value = range.value; showDial(range.value); send(Number(range.value));});
    range.addEventListener('change', () => send(Number(range.value)));
    const commitNumber = () => {if (number.value !== '') {if (!number.checkValidity()) {toast('请填写有效范围内的数值', true); return;} if (Number(number.value) !== state?.device.values[c.id]?.value) send(Number(number.value));}};
    number.addEventListener('change', commitNumber); number.addEventListener('blur', commitNumber);
    number.addEventListener('keydown', event => {if (event.key === 'Enter') number.blur();});
    widgets.push(range, number);
    if (presentation === 'rotary-control') {
      const dial = el('div', {class:'rotary'}), svg = svgNode('svg', {viewBox:'0 0 120 120', 'aria-hidden':'true'});
      const arc = svgNode('circle', {cx:60,cy:60,r:49,fill:'none',class:'knob-arc',transform:'rotate(135 60 60)','stroke-dasharray':'230.91 307.88'});
      const progress = svgNode('circle', {cx:60,cy:60,r:49,fill:'none',class:'knob-progress',transform:'rotate(135 60 60)'});
      const needle = svgNode('line', {x1:60,y1:26,x2:60,y2:36,class:'knob-needle'});
      const face = el('span', {class:'knob-face'});
      svg.append(arc, progress, needle); dial.append(face, svg, range);
      showDial = value => {const f = Math.max(0,Math.min(1,(Number(value)-c.min)/(c.max-c.min))); progress.setAttribute('stroke-dasharray', (f * 230.91) + ' 307.88'); needle.setAttribute('transform','rotate(' + (-135+270*f) + ' 60 60)'); dial.classList.toggle('unknown', value == null);};
      let drag = null;
      range.addEventListener('pointerdown', event => {if (range.disabled) return; event.preventDefault(); range.focus(); range.setPointerCapture(event.pointerId); drag={x:event.clientX,y:event.clientY,value:Number(range.value)};});
      range.addEventListener('pointermove', event => {if (!drag) return; const delta=(drag.y-event.clientY+(event.clientX-drag.x)*.45)*(c.max-c.min)/180, step=c.step||1; range.value=Math.max(c.min,Math.min(c.max,Math.round((drag.value+delta)/step)*step)); number.value=range.value;showDial(range.value);send(Number(range.value));});
      range.addEventListener('pointerup', () => {if (!drag) return; drag=null;send(Number(range.value));});
      range.addEventListener('pointercancel', () => {if (drag) {drag=null;send(Number(range.value));}});
      row.append(dial,el('div',{class:'knob-value'},number,el('span',{class:'unit',text:c.unit})),el('div',{class:'knob-limits'},el('span',{text:c.min}),el('span',{text:c.max})));
    } else row.append(el('div', {class: 'control-value'}, range, number, el('span', {class: 'unit', text: c.unit})));
    setter = value => {number.value = value == null ? '' : Number(Number(value).toFixed(3)); if (value != null) range.value = value; showDial(value);};
  }
  if (c.hint) row.append(el('p', {class: 'hint', text: c.hint}));
  if (c.kind !== 'toggle') row.append(limits);
  registerView(c.id, {row, status, widgets, setter, limits, c});
  return row;
}
async function sendControl(c, value) {
  if (controlDrafts.get(c.id)?.value === value) return writeQueue.idle();
  if (!busy.has(c.id) && state?.device.values[c.id]?.source !== 'saved' && state?.device.values[c.id]?.value === value) return;
  const reason = restriction(c); if (reason) return toast(reason, true);
  const context = editContext(), changes = {[c.id]: value}, acknowledged = [];
  if (c.id === 'fx.unit' && value === 2) {
    for (const id of ['fx.bpm', 'fx.subdivision']) {
      const definition = byId.get(id), current = state.device.values[id]?.value;
      if (current == null || current < definition.min || current > definition.max) changes[id] = definition.min;
    }
  }
  if (c.confirm && value) {if (!await confirmAction(c.label, c.confirm)) {renderControls(); return;} acknowledged.push(c.id);}
  if (linkedEq && /^eq\.(outL|outR|hifiL|hifiR)\./.test(c.id)) {
    const peer = c.id.replace(/(out|hifi)(L|R)/, (_, prefix, side) => prefix + (side === 'L' ? 'R' : 'L'));
    if (byId.has(peer)) changes[peer] = value;
  }
  const revision = ++editRevision;
  for (const [id, draftValue] of Object.entries(changes)) {
    busy.add(id); controlDrafts.set(id, {value:draftValue, revision, context});
  }
  invalidateRefresh(); renderControls();
  const key = 'control:' + JSON.stringify(context) + ':' + Object.keys(changes).sort().join(',');
  try {
    await writeQueue.enqueue(key, async () => {
      checkEditContext(context);
      const result = await api('control', {changes, acknowledged, ...context});
      acceptWriteResult(result, context);
      return result;
    });
  } catch (error) {toast(error.message, true);}
  finally {
    let changed = false;
    for (const id of Object.keys(changes)) if (controlDrafts.get(id)?.revision === revision) {
      controlDrafts.delete(id); busy.delete(id); changed = true;
    }
    // Superseded drag events settle together; paint only the newest result.
    if (changed) render();
  }
}

function renderControls() {
  if (!state) return;
  for (const [id, collection] of views) {
    const entry = state.device.values[id], candidate = controlDrafts.get(id);
    const draft = candidate?.context.selection === selectedBank && candidate?.context.expectedMode === state.mode ? candidate : null;
    for (const v of collection) {
      const editing = v.row.contains(document.activeElement) && document.activeElement.matches('input[type=number],input[type=range]'), pending = busy.has(id), reason = restriction(v.c);
      if (!editing) v.setter(draft ? draft.value : entry?.value);
      v.row.classList.toggle('pending', pending);
      v.status.textContent = pending ? '写入中…' : sourceText(entry);
      v.status.title = entry?.source==='sent'?'USB 已发送，尚无设备回传确认':v.status.textContent;
      v.status.dataset.source = entry?.source || 'unknown';
      for (const w of v.widgets) w.disabled = !!reason || (pending && !!v.c.confirm);
      v.limits.textContent = reason; v.limits.classList.toggle('hidden', !reason);
      v.row.title = reason;
      v.row.hidden = !applicable(v.c);
    }
  }
  groupViews.forEach(g => {g.card.hidden = !g.controls.some(applicable);});
  const dynTabs=$('page-dynamics').querySelector('.local-tabs');if(dynTabs)dynTabs.hidden=state.mode!=='pro';
  for(const [id,hide] of [['fx.time',state.device.values['fx.unit']?.value===2],['fx.bpm',state.device.values['fx.unit']?.value!==2],['fx.subdivision',state.device.values['fx.unit']?.value!==2]])
    for(const v of views.get(id)||[])v.row.classList.toggle('conditional-hidden',hide);
  document.querySelectorAll('.bank-details').forEach(n=>n.hidden=state.mode!=='pro');
  eqViews.forEach(v => {v.tab.hidden = !v.controls.some(applicable); v.panel.hidden = activeEq !== v.target || v.tab.hidden;});
  const visibleEq = eqViews.find(v => !v.tab.hidden);
  if (visibleEq && !eqViews.find(v => v.target === activeEq && !v.tab.hidden)) selectEq(visibleEq.target);
  drawEq();
}
function addGroup(page, name, controls) {
  const head = el('div', {class:'card-head'}, el('h2', {text:name}));
  const card = el('section', {class:'card'}, head);
  if (controls[0]?.page === 'input' && /^(IN [12]|OTG IN)$/.test(name)) {
    card.classList.add('input-channel');
    const channel=name==='IN 1'?1:name==='IN 2'?2:3;card.dataset.channel=channel;
    const gain = controls.find(c => /^input\.\d\.gain$/.test(c.id));
    if (gain) card.append(control(gain,'rotary-control'));
    if(channel<3) {const auto=button('Auto Gain',async()=>{try {const job=state?.autoGain;if(job?.status==='ready'&&job.channel===channel){await sendControl(byId.get('input.'+channel+'.gain'),job.recommended);}else {await api('autogain',{channel});await refresh(true);}}catch(e){toast(e.message,true);}},'channel-auto-gain');auto.dataset.autoGain=channel;card.append(auto);}
    else card.append(el('div',{class:'channel-source',text:'USB / OTG'}));
    card.append(channel===3?el('div',{class:'stereo-meter-pair input-stereo-meter'},channelMeter('input:3:L','OTG 输入左声道'),channelMeter('input:3:R','OTG 输入右声道')):channelMeter('input:'+channel,name+'输入电平'));
    const order=['phantom','instrument','mute','polarity'];
    const primary=order.map(key=>controls.find(c=>c.id==='input.'+channel+'.'+key)).filter(Boolean);
    const switches=el('div',{class:'input-switches'});
    const shortNames={phantom:'+48V',instrument:'Inst',mute:'Mute',polarity:'Ø'};
    for(const c of primary){const row=control(c,'channel-button'),widget=row.querySelector('.toggle');widget.dataset.caption=shortNames[c.id.split('.').at(-1)];switches.append(row);}
    card.append(switches);
    const extra = controls.filter(c => c !== gain && !primary.includes(c));
    const tail=el('div',{class:'channel-tail'},el('span',{text:channel<3?String(channel):'OTG'}));
    if(extra.length){const dialog=el('dialog',{class:'channel-dialog'},el('h2',{text:name+' · 通道设置'}),extra.map(c=>control(c)));dialog.append(el('div',{class:'dialog-actions'},button('完成',()=>dialog.close())));document.body.append(dialog);const more=button('⋯',()=>dialog.showModal(),'channel-more');more.setAttribute('aria-label',name+'更多设置');tail.append(more);}
    card.append(tail);
  } else {
    if (controls[0]?.page === 'output' || controls[0]?.page === 'loopback') {card.classList.add('routing-card');head.prepend(icon(controls[0].page === 'loopback' ? 'loopback' : 'headphones'));}
    card.append(...controls.map(c=>control(c)));
  }
  card.dataset.group=name; card.dataset.section=controls[0]?.page || '';
  page.append(card); groupViews.push({card, controls}); return card;
}
function buildMonitor() {
  const panel = $('monitor-panel');
  panel.append(el('div',{class:'monitor-heading'},icon('headphones'),el('h2',{text:'监听输出'})),el('p',{class:'monitor-subtitle',text:'MONITOR OUTPUT'}));
  for(const id of ['output.selection','hifi.output','output.volume','hifi.volume']) {const c=byId.get(id);if(c)panel.append(control(c,id.endsWith('.volume')?'rotary-control':''));}
  panel.append(channelMeter('output','监听输出电平'));
  for(const id of ['output.mute','hifi.mute','output.highGain','hifi.highGain']){const c=byId.get(id);if(c)panel.append(control(c));}
  const more=button('输出路由',()=>navigate('output'),'monitor-more');more.prepend(icon('routing'));panel.append(more);
}
function buildSchemaPages() {
  for (const pageName of ['input', 'mixer', 'output', 'dynamics', 'fx', 'device']) {
    const container = el('div', {class: 'cards'}); $('page-' + pageName).append(container);
    const groups = new Map();
    definitions.filter(c => c.page === pageName || pageName === 'output' && c.page === 'loopback').forEach(c => {
      const group = pageName === 'fx' ? (['fx.input1','fx.input2','fx.pan1','fx.pan2','fx.return','fx.mute','fx.process'].includes(c.id) ? '发送与返回' : ['fx.room','fx.reverbMix','fx.decay','fx.reverb','fx.width','fx.channel'].includes(c.id) ? '混响' : '延迟') : c.group;
      if (!groups.has(group)) groups.set(group, []); groups.get(group).push(c);
    }); groups.forEach((list, group) => addGroup(container, group, list));
    if(pageName==='output')for(const [signal,label,ids] of [['output','监听输出电平',['output.volume','hifi.volume']],['otgOut','OTG 输出电平',['otg.volume']]]){
      const controls=ids.map(id=>byId.get(id)).filter(Boolean);
      const card=el('section',{class:'card output-meter-card'},el('h2',{text:label}),el('div',{class:'stereo-meter-pair'},channelMeter(signal+':L',label+' L'),channelMeter(signal+':R',label+' R')),el('p',{class:'hint',text:'L / R · dBFS · 点击峰值清除削波指示'}));
      const target = [...container.children].find(n=>n.dataset.group===(signal==='output'?'耳机与线路':'OTG OUT'));
      if(target){card.classList.add('routing-meter');target.prepend(card);}else container.append(card);groupViews.push({card,controls});
    }
  }
}
function buildOverview() {
  const metrics = el('div', {class: 'metrics'});
  for (const [id, label, sub] of [['mode', '工作模式', '当前 USB 配置'], ['rate', '采样率', 'macOS CoreAudio'], ['volume', '输出音量', '耳机 / 线路'], ['parameters', '已回读参数', '来自设备']]) {
    metrics.append(el('div', {class: 'metric'}, el('span', {text: label}), el('b', {id: 'metric-' + id, text: '—'}), el('small', {text: sub})));
  }
  const rates = el('section', {class: 'card full'}, el('div', {class: 'card-head'}, el('h2', {text: '采样率'})), el('div', {id: 'rates', class: 'rates'}), el('p', {class: 'hint', text: '改变采样率会中断正在使用设备的音频流。部分 DSP 在 44.1 / 48 kHz 下可用。'}));
  const shortcuts = el('div', {class: 'overview-links'});
  for (const [id, name, caption] of [['input', '输入通道', '增益 · 静音 · 48V'], ['mixer', '混音总线', 'Mix A / B / C'], ['output', '输出路由', '监听 · OTG · 回环'], ['eq', '参数均衡器', '逐段调整 · 左右联动'], ['fx', '延迟 / 混响', '发送 · 返回 · 干湿比'], ['profiles', '保存配置', '导入 · 导出 · 恢复']]) {
    shortcuts.append(button('', () => navigate(id))); shortcuts.lastChild.append(el('b', {text: name}), el('span', {text: caption}));
  }
  const system = el('section', {class: 'card full'}, el('div', {class: 'card-head'}, el('h2', {text: '系统音频控制'})), el('div', {id: 'audio-controls'}));
  $('page-overview').append(metrics, el('div', {class: 'cards'}, rates, el('section', {class: 'card full'}, shortcuts), system));
}
function buildInputTools() {
  const actions = el('div', {class: 'actions'});
  actions.append(button('取消测量', async () => {try {await api('autogain', {action:'cancel'}); await refresh(true);} catch (e) {toast(e.message,true);}}));
  const apply = button('应用建议增益', async () => {const job = state?.autoGain; if (job?.status === 'ready') await sendControl(byId.get('input.' + job.channel + '.gain'), job.recommended);}, 'primary');
  apply.id = 'auto-gain-apply'; apply.hidden = true;
  $('page-input').append(el('section', {id: 'input-tools', class: 'auto-gain-notice',hidden:true},
    el('p', {class: 'hint', text: '以正常音量说话或演奏 10 秒。根据测得的峰值计算增益，让峰值接近 −12 dBFS；测量后可应用建议值。'}),
    actions, el('p', {id: 'auto-gain-status', class: 'muted', role: 'status'}), apply));
}
function renderInputTools() {
  $('input-tools').hidden = !['pro', 'game'].includes(state.mode)||!state.autoGain;
  const job = state.autoGain, status = $('auto-gain-status');
  status.textContent = !job ? '' : job.status === 'measuring' ? 'IN ' + job.channel + ' 测量中，剩余 ' + Math.max(0, Math.ceil(job.end - state.time)) + ' 秒。请正常说话或演奏。' :
    job.status === 'error' ? job.error : '测得峰值 ' + format(job.peak, 'dBFS') + '，建议将 IN ' + job.channel + ' 增益从 ' + job.gain + ' 调整至 ' + job.recommended + ' dB。';
  $('auto-gain-apply').hidden = job?.status !== 'ready';
  $('auto-gain-apply').disabled = !serverOnline || !state.device.connected || state.device.syncing;
  document.querySelectorAll('[data-auto-gain]').forEach(b=>{const own=job?.channel===Number(b.dataset.autoGain);b.textContent=own&&job.status==='measuring'?Math.max(0,Math.ceil(job.end-state.time))+'s':own&&job.status==='ready'?'应用 '+job.recommended+' dB':'Auto Gain';b.disabled=!serverOnline||!state.device.connected||state.device.syncing||job?.status==='measuring';});
  renderMeters();
}
let audioSignature = '';
function renderOverview() {
  const audio = state.audio, mode = state.mode, volume = state.device.values[mode === 'hifi' ? 'hifi.volume' : 'output.volume'];
  $('metric-mode').textContent = ({pro: 'PRO', game: 'GAME', hifi: 'HiFi'})[mode] || '—';
  $('metric-rate').textContent = rateText(audio.sampleRate?.value);
  $('metric-volume').textContent = format(volume?.value, 'dB');
  $('metric-volume').nextElementSibling.textContent = mode === 'pro' ? '正在查看记忆槽 ' + ((state.device.viewBank ?? 0) + 1) : '耳机 / 线路';
  $('metric-parameters').textContent = Object.values(state.device.values).filter(v => v.source === 'device').length;
  const rates = (audio.supportedRates || []).filter(r => r.min === r.max).map(r => r.min);
  const signature = JSON.stringify([audio.connected, rates, audio.sampleRate?.settable, (audio.controls || []).map(c => [c.scope, c.channel, c.volume.present, c.volume.settable, c.mute.present, c.mute.settable])]);
  if (audioSignature !== signature) {
    audioSignature = signature; $('rates').replaceChildren(...rates.map(rate => {
      const b = button(rateText(rate), () => audioAction({action: 'sampleRate', value: rate})); b.dataset.rate = rate; return b;
    }));
    if (!rates.length) $('rates').append(el('p', {class: 'empty', text: '等待 CoreAudio 设备连接'}));
    $('audio-controls').replaceChildren();
    (audio.controls || []).filter(c => c.volume.present || c.mute.present).forEach(c => {
      const key = c.scope + '-' + c.channel, title = (c.scope === 'input' ? '输入' : '输出') + ' · ' + ['总控', '左声道', '右声道'][c.channel];
      const row = el('div', {class: 'control-row'}, el('div', {class: 'control-top'}, el('span', {text: title}), el('span', {id: 'audio-value-' + key, class: 'control-status'})));
      const wrap = el('div', {class: 'control-value'});
      if (c.volume.present) {
        const slider = el('input', {id: 'audio-slider-' + key, type: 'range', min: 0, max: 1, step: .01, 'aria-label': title + '音量', disabled: !c.volume.settable});
        slider.addEventListener('input', () => {$('audio-value-' + key).textContent = Math.round(slider.value * 100) + '%';});
        slider.addEventListener('change', () => audioAction({action: 'volume', scope: c.scope, channel: c.channel, value: Number(slider.value)})); wrap.append(slider);
      }
      if (c.mute.present) wrap.append(el('button', {id: 'audio-mute-' + key, type: 'button', class: 'toggle', role: 'switch', 'aria-label': title + '静音', disabled: !c.mute.settable,
        onclick: () => audioAction({action: 'mute', scope: c.scope, channel: c.channel, value: $('audio-mute-' + key).getAttribute('aria-checked') !== 'true'})}));
      row.append(wrap); $('audio-controls').append(row);
    });
    if (!$('audio-controls').children.length) $('audio-controls').append(el('p', {class: 'empty', text: '没有可用的系统音量属性'}));
  }
  $('rates').querySelectorAll('button').forEach(b => {b.classList.toggle('active', Number(b.dataset.rate) === audio.sampleRate?.value); b.disabled = !serverOnline || !audio.sampleRate?.settable;});
  (audio.controls || []).forEach(c => {
    const key = c.scope + '-' + c.channel, slider = $('audio-slider-' + key), mute = $('audio-mute-' + key);
    if (slider) {slider.disabled = !serverOnline || !audio.connected || !c.volume.settable; if (document.activeElement !== slider) {slider.value = c.volume.value; $('audio-value-' + key).textContent = c.volume.value == null ? '—' : Math.round(c.volume.value * 100) + '%';}}
    if (mute) {mute.disabled = !serverOnline || !audio.connected || !c.mute.settable; mute.setAttribute('aria-checked', String(!!c.mute.value));}
  });
}
async function audioAction(payload) {
  if (payload.action === 'sampleRate' && !await confirmAction('切换采样率', '音频流会暂时中断。确认切换至 ' + rateText(payload.value) + '？')) return;
  queue = queue.catch(() => {}).then(async () => {try {await api('audio', payload); await refresh(true); toast('已应用系统音频设置');} catch (e) {toast(e.message, true);}});
}
function buildMixer() {
  const tabs = el('div', {class: 'tabs mixer-tabs'});
  ['A', 'B', 'C'].forEach(bus => tabs.append(button('Mix ' + bus, () => {activeBus = bus; renderMixer();}, bus === activeBus ? 'active' : '')));
  const meta = el('p', {id: 'mixer-meta', class: 'muted mixer-meta'}), grid = el('div', {class: 'mixer-grid'});
  sources.forEach(source => {
    const link = el('input', {type: 'checkbox', 'aria-label': source.label + '立体声联动'});
    link.addEventListener('change', () => sendMixer(source.id, {link: link.checked}));
    const card = el('section', {class: 'card mixer-strip','data-source':source.id}, el('div', {class: 'card-head'},el('h2', {text: source.label})),el('label', {class: 'inline-label channel-link'}, link,icon('stereo-link'),'联动'));
    const sides = el('div', {class: 'mixer-sides'}), view = {source: source.id, card, link, sides: {},panDetail:{}};
    ['left', 'right'].forEach(side => {
      const panel = el('div', {class: 'mixer-side'}, el('h3', {text: side === 'left' ? 'L' : 'R'})), sv = {widgets: {},panel};
      for (const [key, title, min, max, step, unit] of [['pan', '声像', 0, 100, 1, '%'], ['gain', '电平', -90, 12, .1, 'dB']]) {
        const range = el('input', {type: 'range', min, max, step, 'aria-label': source.label + ' ' + side + ' ' + title});
        const number = el('input', {type: 'number', min, max, step, inputmode: 'decimal', 'aria-label': source.label + ' ' + side + ' ' + title + '数值'});
        if(key==='gain'){range.min=0;range.max=1000;range.step=1;range.dataset.fader='true';}
        range.addEventListener('input', () => {number.value=key==='gain'?faderDecibels(range.value):range.value;if(key==='gain')showFader(range,Number(number.value));commit(Number(number.value));});
        const commit=value=>{const strip=mixerStrip(activeBus, source.id);if(key==='pan'&&strip.link){sendMixer(source.id,{left:{pan:Math.max(0,(value-50)*2)},right:{pan:Math.min(100,value*2)}});}else sendMixer(source.id,{[side]:{[key]:value}});};
        range.addEventListener('change', () => commit(key==='gain'?faderDecibels(range.value):Number(range.value)));
        const commitNumber=()=>{if(number.value===''||!number.checkValidity())return;const strip=mixerStrip(activeBus, source.id),current=key==='pan'&&strip.link?(strip.left.pan>0?50+strip.left.pan/2:strip.right.pan/2):strip[side][key];if(Number(number.value)!==current)commit(Number(number.value));};
        number.addEventListener('change',commitNumber);number.addEventListener('blur',commitNumber);number.addEventListener('keydown',event=>{if(event.key==='Enter')number.blur();});
        if (key === 'gain') {
          range.className='vertical-fader';range.setAttribute('aria-orientation','vertical');
          const scale=el('div',{class:'fader-scale','aria-hidden':'true'},...[12,6,0,-6,-12,-18,-24,-30,-36,-48,-60].map(value=>{const tick=el('span',{text:value>0?'+'+value:String(value)});tick.style.top=((1-scalePosition(value,FADER_SCALE))*100)+'%';return tick;}));
          const meterSignal = source.id === 'IN12' ? 'input:' + (side === 'left' ? '1' : '2')
            : ({PB12:'playback:1',PB34:'playback:2',PB56:'playback:3',PB78:'playback:4',FX:'fx',OTGIN:'input:3'}[source.id] || null);
          const sideLabel = side === 'left' ? ' L' : ' R';
          const meter=el('div',{class:'strip-meter','aria-label':meterSignal ? source.label + sideLabel + '电平' : '此通道无电平回读'},el('span',{class:'meter-fill'}));
          if(source.id==='IN12') meter.dataset.input=side==='left'?'1':'2';
          else if(meterSignal) meter.dataset.signal=meterSignal + (meterSignal === 'input:3' ? (side === 'left' ? ':L' : ':R') : ':' + (side === 'left' ? 'L' : 'R'));
          else meter.classList.add('unavailable');
          const stereoMeter = side === 'left' && meterSignal ? el('div', {class:'strip-meter linked-stereo-meter', 'data-signal': source.id === 'IN12' ? 'input:2' : meterSignal + ':R', 'aria-label':source.label + ' R电平'}, el('span',{class:'meter-fill'})) : null;
          panel.append(el('div',{class:'fader-well'},range,meter,stereoMeter,scale),el('div',{class:'fader-value'},number,el('span',{class:'unit',text:unit})));
        } else panel.append(el('div', {class: 'pan-control'},el('div',{class:'pan-caption'},el('span',{text:'L'}),el('span',{text:'R'})),range,el('div',{class:'pan-value'},number,el('span',{class:'unit',text:unit}))));
        sv.widgets[key] = [range, number];
      }
      const actions = el('div', {class: 'mixer-actions'});
      for (const [key, title] of [['mute', 'M'], ['solo', 'S'], ['polarity', 'Ø']]) {
        const b = button(title, () => sendMixer(source.id, {[side]: {[key]: !mixerStrip(activeBus, source.id)[side][key]}}));
        b.dataset.key = key; b.setAttribute('aria-label', source.label + ' ' + side + ' ' + ({mute:'静音',solo:'独奏',polarity:'反相'})[key]);b.title=({mute:'静音',solo:'独奏',polarity:'反相'})[key]; actions.append(b); sv.widgets[key] = [b];
      }
      panel.append(actions); sides.append(panel); view.sides[side] = sv;
    }); card.append(sides);
    const detail=el('dialog',{class:'mixer-detail-dialog'},el('h2',{text:source.label+' · 通道设置'}),el('p',{class:'muted',text:'调整立体声联动、独立声像和反相。'}),card.querySelector('.channel-link'));
    for(const side of ['left','right']){const phase=view.sides[side].widgets.polarity[0];phase.classList.add('mixer-phase');phase.textContent=(side==='left'?'左声道':'右声道')+'反相 Ø';detail.append(phase);}
    for(const side of ['left','right']){const range=el('input',{type:'range',min:0,max:100,step:1,'aria-label':source.label+' '+side+'独立声像'}),number=el('input',{type:'number',min:0,max:100,step:1,'aria-label':source.label+' '+side+'独立声像数值'});range.addEventListener('input',()=>{number.value=range.value;commit(range);});const commit=w=>{if(w.value!==''&&w.checkValidity()&&Number(w.value)!==mixerStrip(activeBus, source.id)[side].pan)sendMixer(source.id,{[side]:{pan:Number(w.value)}});};for(const w of [range,number])w.addEventListener('change',()=>commit(w));number.addEventListener('blur',()=>commit(number));number.addEventListener('keydown',e=>{if(e.key==='Enter')number.blur();});view.panDetail[side]=[range,number];detail.append(el('div',{class:'control-row'},el('div',{class:'control-top',text:side==='left'?'左声道':'右声道'}),el('div',{class:'control-value'},range,number)));}
    detail.append(el('div',{class:'dialog-actions'},button('完成',()=>detail.close())));document.body.append(detail);const more=button('⋯',()=>detail.showModal(),'mixer-detail-button');more.setAttribute('aria-label',source.label+'左右声像设置');card.append(more);grid.append(card);mixerViews.push(view);
  });
  const filters=el('div',{class:'channel-filters','aria-label':'显示混音通道'});
  for(const [key,label] of [['all','全部通道'],['inputs','输入'],['playback','电脑播放'],['fx','效果']]) {const b=button(label,()=>{mixerFilter=key;renderMixer();});b.dataset.filter=key;filters.append(b);}
  $('page-mixer').append(el('div',{class:'mixer-toolbar'},tabs,filters), meta, el('p', {id: 'mixer-mode-note', class: 'notice hidden', text: 'HiFi 模式没有混音总线。切换工作模式后可调整。'}), grid);
}
function renderMixer() {
  $('page-mixer').dataset.bus=activeBus;
  document.querySelectorAll('[data-filter]').forEach(b=>{b.classList.toggle('active',b.dataset.filter===mixerFilter);b.setAttribute('aria-pressed',String(b.dataset.filter===mixerFilter));});
  $('page-mixer').querySelector('.mixer-toolbar').hidden=effectiveMode()!=='pro';
  const disabled = !serverOnline || !state.device.connected || state.device.syncing || effectiveMode() !== 'pro' || changingMode;
  $('page-mixer').querySelectorAll('.tabs, #mixer-meta, .mixer-grid').forEach(n => {n.hidden = effectiveMode() !== 'pro';});
  $('page-mixer').querySelectorAll('.tabs button').forEach((b, i) => b.classList.toggle('active', ['A', 'B', 'C'][i] === activeBus));
  $('mixer-meta').textContent = ({saved: '来自电脑保存配置；修改某个通道后会将该通道发送到设备。', draft: '尚无已保存的混音配置；修改前请核对通道电平。', sent: '当前会话已发送的混音配置。设备不回传推子、独奏和联动状态。', device: '混音矩阵已回读。'})[state.mixer.source];
  $('mixer-mode-note').hidden = effectiveMode() !== 'hifi';
  for (const v of mixerViews) {
    const strip = mixerStrip(activeBus, v.source);
    v.card.classList.toggle('linked',strip.link);v.sides.right.panel.hidden=strip.link;
    if (!v.card.contains(document.activeElement) ) v.link.checked = strip.link;
    v.link.disabled = disabled;
    for (const side of ['left', 'right']) for (const [key, widgets] of Object.entries(v.sides[side].widgets)) for (const w of widgets) {
      w.disabled = disabled;
      if (w.tagName === 'BUTTON') {w.classList.toggle('on', strip[side][key]); w.setAttribute('aria-pressed', String(strip[side][key]));}
      else if (document.activeElement !== w ){if(w.dataset.fader)showFader(w,strip[side][key]);else w.value = key==='pan'&&strip.link?(strip.left.pan>0?50+strip.left.pan/2:strip.right.pan/2):strip[side][key];}
    }
    for(const side of ['left','right'])for(const w of v.panDetail[side]){w.disabled=disabled;if(document.activeElement!==w)w.value=strip[side].pan;}
    v.card.hidden = effectiveMode() !== 'pro' || !(mixerFilter==='all' || mixerFilter==='playback'&&v.source.startsWith('PB') || mixerFilter==='inputs'&&['IN12','OTGIN'].includes(v.source) || mixerFilter==='fx'&&v.source==='FX');
  }
}
async function sendMixer(source, change) {
  if (!serverOnline || !state.device.connected || state.device.syncing || changingMode || state.mode !== 'pro') return;
  const bus = activeBus, context = editContext(), draftKey = bus + ':' + source;
  const current = mixerStrip(bus, source), strip = draftMixer(current, change);
  if (JSON.stringify(strip) === JSON.stringify(current)) return writeQueue.idle();
  const revision = ++editRevision;
  mixerDrafts.set(draftKey, {strip, revision}); mixerRequests++; mixerBusy = true;
  invalidateRefresh(); renderMixer();
  const fields = Object.entries(change).flatMap(([side, values]) => typeof values === 'object' ? Object.keys(values).map(key => side + '.' + key) : [side]).sort();
  // Different fields remain ordered; only unsent updates of the same field are replaced.
  const key = 'mixer:' + bus + ':' + source + ':' + fields.join(',');
  try {
    await writeQueue.enqueue(key, async () => {
      checkEditContext(context);
      const result = await api('mixer', {bus, source, change, expectedMode:context.expectedMode});
      acceptWriteResult(result, context);
      return result;
    });
  } catch (error) {toast(error.message, true);}
  finally {
    const changed = mixerDrafts.get(draftKey)?.revision === revision;
    if (changed) mixerDrafts.delete(draftKey);
    mixerBusy = --mixerRequests > 0; if (changed || !mixerBusy) render();
  }
}

function buildEq() {
  const toolbar = el('div', {class: 'eq-toolbar'}), tabs = el('div', {class: 'tabs'});
  const link = el('input', {type: 'checkbox'}); link.addEventListener('change', () => {linkedEq = link.checked;});
  toolbar.append(tabs, el('label', {class: 'inline-label'}, link, '输出左右联动'));
  const panels = el('div'); $('page-eq').append(toolbar, panels);
  const roots = [...new Set(definitions.filter(c => c.page === 'eq' && /^eq\./.test(c.id)).map(c => c.id.split('.')[1]))];
  for (const target of roots) {
    if (target === 'output') continue;
    const controls = definitions.filter(c => c.id.startsWith('eq.' + target + '.'));
    if (!controls.length) continue;
    const tab = button(controls[0].group.replace(/ · \d+ 段/, ''), () => selectEq(target)); tabs.append(tab);
    const panel = el('section', {class: 'card', hidden: true}), canvas = el('canvas', {class: 'eq-canvas', 'aria-label': controls[0].group + '频响估算曲线', role: 'img'});
    const masters = controls.filter(c => c.id.split('.').length === 3);
    if (target.startsWith('out')) masters.push(...definitions.filter(c => c.id.startsWith('eq.output.')));
    if (target.startsWith('hifi')) masters.push(...definitions.filter(c => c.page === 'eq' && c.id.startsWith('hifi.')));
    const masterBox=el('div',{class:'eq-masters'},...masters.map(control));
    panel.append(masterBox, canvas, el('p', {class: 'eq-caption', text: '绿色为 EQ 估算，蓝色为源响应加 EQ，黄色为目标响应。灰色表示参数尚未读全。'}));
    const curveActions = el('div', {class:'actions eq-caption'});
    for (const [kind, title] of [['source','源响应'], ['target','目标响应']]) {
      const file = el('input', {type:'file', accept:'.txt,.csv', hidden:true});
      file.addEventListener('change', async () => {try {if (file.files[0]) await importCurve(file.files[0], target, kind);} catch(e) {toast(e.message,true);} finally {file.value='';}});
      curveActions.append(button('导入' + title, () => file.click()), file);
    }
    curveActions.append(button('清除参考曲线', () => {delete referenceCurves[target]; saveCurves(); drawEq();}));
    const reference=el('details',{class:'reference-tools'},el('summary',{text:'参考曲线'}),curveActions);panel.append(reference);
    const bands = el('div', {class: 'eq-bands'}), bandTabs=el('div',{class:'eq-band-tabs','aria-label':'选择 EQ 频段'});
    panel.classList.add('eq-editor');
    const bandIds = [...new Set(controls.map(c => c.id.split('.')[2]).filter(x => /^\d+$/.test(x)))];
    for (const band of bandIds) {
      const row=el('div', {class:'eq-band'+(band===bandIds[0]?' selected':''),'data-band':band},el('h3',{text:band.padStart(2,'0')}));
      for(const field of ['enabled','type','frequency','gain','q']){const c=byId.get('eq.'+target+'.'+band+'.'+field);if(c)row.append(control(c));}
      const b=button(band,()=>{bands.querySelectorAll('.eq-band').forEach(n=>n.classList.toggle('selected',n===row));bandTabs.querySelectorAll('button').forEach(n=>n.classList.toggle('active',n===b));},band===bandIds[0]?'active':'');b.setAttribute('aria-label','编辑频段 '+band);bandTabs.append(b);bands.append(row);
    }
    const headers=el('div',{class:'eq-table-heading','aria-hidden':'true'},...['段','开关','类型','频率 · Hz','增益 · dB','Q'].map(text=>el('span',{text})));
    const fileInput = el('input', {type: 'file', accept: '.json,.txt', hidden: true});
    fileInput.addEventListener('change', () => importEq(fileInput.files[0], target).finally(() => {fileInput.value = '';}));
    panel.append(bandTabs,headers,bands, el('div', {class: 'actions eq-caption eq-file-actions'}, button('导出此 EQ', () => exportEq(target)), button('导入 EQ', () => fileInput.click()), button('重置此 EQ', () => resetEq(target)), fileInput));
    panels.append(panel); eqViews.push({target, controls, panel, tab, canvas, bandIds});
  }
  if (eqViews.length) selectEq(eqViews[0].target);
}
function selectEq(target) {activeEq = target; eqViews.forEach(v => {v.panel.hidden = v.target !== target; v.tab.classList.toggle('active', v.target === target);}); drawEq();}
function eqValue(target, band, field) {return state?.device.values['eq.' + target + '.' + band + '.' + field]?.value;}
function eqBandCount(target) {return state?.device.values['eq.' + target + '.count']?.value ?? state?.device.values['hifi.eqCount']?.value;}
function biquadMagnitude(type, freq, gain, q, probe, sampleRate) {
  const w = 2 * Math.PI * freq / sampleRate, sn = Math.sin(w), cs = Math.cos(w), alpha = sn / (2 * q), A = 10 ** (gain / 40);
  let b0, b1, b2, a0, a1, a2;
  if (type === 0) {b0 = 1 + alpha * A; b1 = -2 * cs; b2 = 1 - alpha * A; a0 = 1 + alpha / A; a1 = -2 * cs; a2 = 1 - alpha / A;}
  else if (type === 1 || type === 2) {const high = type === 2; b0 = (1 + (high ? cs : -cs)) / 2; b1 = high ? -(1 + cs) : 1 - cs; b2 = b0; a0 = 1 + alpha; a1 = -2 * cs; a2 = 1 - alpha;}
  else {
    const beta = 2 * Math.sqrt(A) * alpha;
    if (type === 3) {b0 = A * ((A + 1) - (A - 1) * cs + beta); b1 = 2 * A * ((A - 1) - (A + 1) * cs); b2 = A * ((A + 1) - (A - 1) * cs - beta); a0 = (A + 1) + (A - 1) * cs + beta; a1 = -2 * ((A - 1) + (A + 1) * cs); a2 = (A + 1) + (A - 1) * cs - beta;}
    else {b0 = A * ((A + 1) + (A - 1) * cs + beta); b1 = -2 * A * ((A - 1) + (A + 1) * cs); b2 = A * ((A + 1) + (A - 1) * cs - beta); a0 = (A + 1) - (A - 1) * cs + beta; a1 = 2 * ((A - 1) - (A + 1) * cs); a2 = (A + 1) - (A - 1) * cs - beta;}
  }
  const p = 2 * Math.PI * probe / sampleRate;
  const power = (x0, x1, x2) => (x0 + x1 * Math.cos(p) + x2 * Math.cos(2 * p)) ** 2 + (x1 * Math.sin(p) + x2 * Math.sin(2 * p)) ** 2;
  return 10 * Math.log10(Math.max(1e-15, power(b0, b1, b2) / power(a0, a1, a2)));
}
function drawEq() {
  const view = eqViews.find(v => v.target === activeEq); if (!view || view.panel.hidden || currentPage !== 'eq') return;
  const canvas = view.canvas, rect = canvas.getBoundingClientRect(); if (!rect.width) return;
  const scale = devicePixelRatio || 1; canvas.width = rect.width * scale; canvas.height = rect.height * scale;
  const ctx = canvas.getContext('2d'); ctx.scale(scale, scale); const w = rect.width, h = rect.height, pad = 29;
  const x = f => pad + Math.log10(f / 20) / 3 * (w - pad - 12), y = db => h / 2 - db / 24 * (h / 2 - 20);
  ctx.font = '10px system-ui'; ctx.fillStyle = '#82909c'; ctx.strokeStyle = '#29323b'; ctx.lineWidth = 1;
  for (const db of [-24, -12, 0, 12, 24]) {ctx.beginPath(); ctx.moveTo(pad, y(db)); ctx.lineTo(w - 12, y(db)); ctx.stroke(); ctx.fillText(String(db), 3, y(db) + 3);}
  for (const f of [20, 100, 1000, 10000, 20000]) {ctx.beginPath(); ctx.moveTo(x(f), 12); ctx.lineTo(x(f), h - 18); ctx.stroke(); ctx.fillText(f >= 1000 ? f / 1000 + 'k' : String(f), Math.min(w - 28, x(f) - 7), h - 4);}
  let complete = true; const filters = [];
  for (const band of view.bandIds) {
    const values = ['enabled', 'type', 'frequency', 'gain', 'q'].map(field => eqValue(activeEq, band, field));
    if (values.some(v => v == null)) {complete = false; continue;} if (values[0]) filters.push(values.slice(1));
  }
  const master = state?.device.values[activeEq.startsWith('hifi') ? 'hifi.peq' : 'eq.' + (activeEq.startsWith('out') ? 'output' : activeEq) + '.enabled']?.value;
  if (master == null) complete = false;
  const preamp = state?.device.values['eq.' + (activeEq.startsWith('out') ? 'output' : activeEq) + '.preamp']?.value || 0;
  ctx.strokeStyle = complete ? '#9fe8c4' : '#667682'; ctx.lineWidth = 2; ctx.beginPath();
  for (let n = 0; n <= 250; n++) {const f = 20 * 1000 ** (n / 250); let db = master === false ? 0 : preamp;
    if (master !== false) for (const [type, freq, gain, q] of filters) db += biquadMagnitude(type, freq, gain, q, f, Math.max(44100, state?.audio.sampleRate?.value || 48000));
    const xx = x(f), yy = y(Math.max(-25, Math.min(25, db))); if (!n) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy);
  } ctx.stroke();
  const references = referenceCurves[activeEq];
  for (const [kind, color] of [['source','#8bbcff'], ['target','#e7bf77']]) {
    const points = references?.[kind]?.points; if (!points?.length) continue;
    ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.beginPath(); let started = false;
    for (const [freq, original] of points) {
      if (freq < 20 || freq > 20000) continue;
      let db = original;
      if (kind === 'source' && master !== false) {db += preamp; for (const [type, f0, gain, q] of filters) db += biquadMagnitude(type, f0, gain, q, freq, Math.max(44100, state?.audio.sampleRate?.value || 48000));}
      const xx = x(freq), yy = y(Math.max(-25,Math.min(25,db))); if (!started) {ctx.moveTo(xx,yy); started=true;} else ctx.lineTo(xx,yy);
    } ctx.stroke();
  }
}
function validateCurves(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('参考曲线数据无效');
  const curves = {};
  for (const kind of ['source', 'target']) {
    const curve = data[kind]; if (!curve) continue;
    if (!Array.isArray(curve.points) || curve.points.length < 2 || curve.points.length > 10000 || curve.points.some(p => !Array.isArray(p) || p.length !== 2 || !p.every(Number.isFinite) || p[0] <= 0 || p[0] > 500000 || Math.abs(p[1]) > 300)) throw new Error('参考曲线数据无效');
    curves[kind] = {name: String(curve.name || kind), points: curve.points.slice().sort((a, b) => a[0] - b[0])};
  }
  return curves;
}
function saveCurves() {try {localStorage.setItem('kb22-reference-curves',JSON.stringify(referenceCurves));} catch {toast('浏览器储存空间不足，曲线仅保留到关闭页面',true);}}
async function importCurve(file, target, kind) {
  if (file.size > 262144) throw new Error('曲线文件最大为 256 KB');
  const points = [];
  for (const line of (await file.text()).split(/\r?\n/)) {
    const fields = line.trim().split(/[\s,;]+/), frequency = Number(fields[0]), db = Number(fields[1]);
    if (fields.length >= 2 && Number.isFinite(frequency) && Number.isFinite(db) && frequency > 0 && frequency <= 500000 && Math.abs(db) <= 300) points.push([frequency,db]);
  }
  if (points.length < 2 || points.length > 10000) throw new Error('曲线需要 2–10000 行“频率 Hz，响应 dB”数据');
  points.sort((a,b)=>a[0]-b[0]); referenceCurves[target] ||= {}; referenceCurves[target][kind] = {name:file.name,points}; saveCurves(); drawEq(); toast('已导入' + (kind === 'source' ? '源响应' : '目标响应') + '：' + file.name);
}
function download(data, filename, type = 'application/json') {const blob = new Blob([typeof data === 'string' ? data : JSON.stringify(data, null, 2)], {type}), url = URL.createObjectURL(blob), a = el('a', {href: url, download: filename}); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);}
function exportEq(target) {
  const values = {}; definitions.filter(c => c.id.startsWith('eq.' + target + '.')).forEach(c => {const entry = state.device.values[c.id]; if (entry?.value != null) values[c.id.split('.').slice(2).join('.')] = entry.value;});
  if (target.startsWith('out')) for (const key of ['enabled', 'preamp']) {const value = state.device.values['eq.output.' + key]?.value; if (value != null) values[key] = value;}
  const bandCount = target.startsWith('hifi') ? eqBandCount(target) : undefined;
  delete values.count;
  download({format: 'kb22-eq', version: 1, target, values, bandCount, curves: referenceCurves[target]}, 'KB22-EQ-' + target + '.json');
}
async function importEq(file, target) {
  if (!file) return; if (file.size > 262144) return toast('EQ 文件过大', true);
  try {
    const text = await file.text(); let values, importedCurves, bandCount;
    if (file.name.toLowerCase().endsWith('.json')) {const data = JSON.parse(text); if (data.format !== 'kb22-eq' || data.version !== 1) throw new Error('不是 KB22 EQ 文件'); values = data.values;
      if (data.curves) importedCurves = validateCurves(data.curves);
      if (data.bandCount != null) {if (!Number.isInteger(data.bandCount) || data.bandCount < 0 || data.bandCount > 10) throw new Error('EQ 有效段数必须为 0–10'); bandCount = data.bandCount;}
    }
    else {
      values = {}; const types = {PK: 0, LP: 1, HP: 2, LS: 3, HS: 4}; let band = 0;
      for (const line of text.split(/\r?\n/)) {
        const preamp = line.match(/^\s*Preamp:\s*([-\d.]+)\s*dB\s*$/i); if (preamp) {values.preamp = Number(preamp[1]); continue;}
        const match = line.match(/Filter\s+\d+:\s+(ON|OFF)\s+(PK|LP|HP|LS|HS)\s+Fc\s+([\d.]+)\s+Hz(?:\s+Gain\s+([-\d.]+)\s+dB)?\s+Q\s+([\d.]+)/i);
        if (match) {band++; for (const [field, value] of Object.entries({enabled: match[1].toUpperCase() === 'ON', type: types[match[2].toUpperCase()], frequency: Number(match[3]), gain: Number(match[4] || 0), q: Number(match[5])})) values[band + '.' + field] = value;}
        else if (/^\s*Filter\s+\d+:/i.test(line)) throw new Error('存在不支持的 APO 滤波器；请使用带 Q 值的 PK / LP / HP / LS / HS');
      } if (!band) throw new Error('没有识别到 Equalizer APO 的 PK / LP / HP / LS / HS 参数');
      const maxBands = eqViews.find(v => v.target === target).bandIds.length;
      if (band > maxBands) throw new Error('滤波器数量超过此通道的上限');
      for (let n = band + 1; n <= maxBands; n++) values[n + '.enabled'] = false;
      bandCount = Object.entries(values).filter(([key, value]) => key.endsWith('.enabled') && value).length;
    }
    if (!values || typeof values !== 'object' || Array.isArray(values)) throw new Error('EQ 参数格式无效');
    const changes = {}; for (const [suffix, value] of Object.entries(values)) {const group = target.startsWith('out') && ['enabled', 'preamp'].includes(suffix) ? 'output' : target; const id = 'eq.' + group + '.' + suffix; if (!byId.has(id)) throw new Error('EQ 段数或字段不适用于此通道'); changes[id] = value;}
    if (target.startsWith('hifi') && bandCount != null) changes['eq.' + target + '.count'] = bandCount;
    if (!await confirmAction('导入 EQ', '将 ' + Object.keys(changes).length + ' 个参数应用到当前 EQ 通道？')) return;
    if (linkedEq) linkEqChanges(changes);
    await writeQueue.idle(); const result = await api('control', {changes, outputBank: selectedBank, verify:true}); if (importedCurves) {referenceCurves[target] = importedCurves; saveCurves();} await refresh(true); toast(result.pending?.length ? 'EQ 已发送，仍有 ' + result.pending.length + ' 项待设备回读确认' : 'EQ 已导入并经设备确认', Boolean(result.pending?.length));
  } catch (e) {toast(e.message, true);}
}
async function resetEq(target) {
  if (!await confirmAction('重置 EQ', '将此通道各段设为旁通、0 dB，并恢复频率和 Q 默认值？')) return;
  const view = eqViews.find(v => v.target === target), changes = {};
  view.bandIds.forEach(band => {for (const [key, value] of Object.entries({enabled: false, type: 0, gain: 0, frequency: 632, q: .707})) changes['eq.' + target + '.' + band + '.' + key] = value;});
  if (linkedEq) linkEqChanges(changes);
  try {await writeQueue.idle(); const result = await api('control', {changes, outputBank: selectedBank, verify:true}); await refresh(true); toast(result.pending?.length ? '重置已发送，仍有 ' + result.pending.length + ' 项待设备回读确认' : 'EQ 已重置并经设备确认', Boolean(result.pending?.length));} catch (e) {toast(e.message, true);}
}
function linkEqChanges(changes) {
  for (const [key, value] of Object.entries(changes)) {
    if (!/^eq\.(out|hifi)[LR]\./.test(key)) continue;
    const peer = key.replace(/(out|hifi)(L|R)/, (_, root, side) => root + (side === 'L' ? 'R' : 'L'));
    if (byId.has(peer)) changes[peer] = value;
  }
}
function buildModeAndBanks() {
  const selector = el('select', {id: 'mode-select', 'aria-label': '工作模式'},
    el('option', {value: '', text: '等待设备'}), ...[['pro', '专业录音'], ['game', '电竞'], ['hifi', 'HiFi']].map(([value, text]) => el('option', {value, text})));
  selector.addEventListener('change', async () => {
    const mode = selector.value; if (!mode || mode === state?.mode) return;
    if (!await confirmAction('切换工作模式', '设备会重新连接 USB，并中断当前音频。确认切换到' + selector.selectedOptions[0].textContent + '模式？')) {selector.value = state?.mode || ''; return;}
    try {changingMode = true; selectedBank = null; render(); await writeQueue.idle(); await api('mode', {mode}); toast('已发送模式切换，正在等待设备重新连接');}
    catch (e) {toast(e.message, true);}
    finally {changingMode = false; await refresh(true);}
  });
  $('page-device').prepend(el('section', {class: 'card'}, el('h2', {text: '工作模式'}), selector,
    el('p', {class: 'hint', text: '模式决定可用通道、DSP 和采样率。切换后页面会自动更新。'})));
  const reset = button('恢复出厂设置', async () => {
    const mode = state?.mode;
    if (!await confirmAction('恢复出厂设置', '这会还原声卡设置和设备记忆，并中断音频。电脑上的命名配置会保留；当前已读设置会先保存到 Mac。确认继续？')) return;
    if (await askName('输入 RESET KB22 确认', '', '恢复出厂') !== 'RESET KB22') return;
    try {
      changingMode = true; render();
      await writeQueue.idle();
      const result = await api('factory-reset', {mode, confirmation: 'RESET KB22'});
      selectedBank = null;
      toast('指令已发送，等待设备回读。恢复记录：data/' + result.backup);
    } catch (e) {toast(e.message, true);}
    finally {changingMode = false; await refresh(true);}
  });
  reset.id = 'factory-reset';
  $('page-device').append(el('section', {class: 'card'}, el('h2', {text: '恢复出厂'}),
    el('p', {class: 'hint', text: '还原设备设置及记忆。此操作需要再次确认并输入确认文字。'}), reset));
  for (const page of ['output', 'eq']) {
    const choice = el('select', {'aria-label': '输出记忆槽'}, el('option', {value: '', text: '当前设备槽位'}),
      ...['全部输出', '线路', '6.35 mm 耳机', '3.5 mm 耳机', '全部耳机', '线路 + 3.5 mm', '线路 + 6.35 mm', '共享'].map((name, value) => el('option', {value, text: name})));
    choice.addEventListener('change', async () => {const bank = choice.value === '' ? null : Number(choice.value); await writeQueue.idle(); selectedBank = bank; await refresh(true);});
    $('page-' + page).prepend(el('section', {class: 'card bank-selector'}, el('h2', {text: '输出记忆槽'}), choice,
      el('p', {class: 'hint', text: '选择要编辑的输出音量与 EQ 记忆。切换此列表不会切换物理输出接口。'})));
  }
  let taps = [];
  $('page-fx').prepend(el('section', {class: 'card pro-only'}, el('h2', {text: '节拍输入'}), button('TAP', async () => {
    const now = performance.now(); if (taps.length && now - taps.at(-1) > 2500) taps = [];
    taps.push(now); taps = taps.slice(-5); if (taps.length < 2) return toast('再点一次以计算节拍');
    const ms = (taps.at(-1) - taps[0]) / (taps.length - 1), bpmMode = state?.device.values['fx.unit']?.value === 2;
    const id = bpmMode ? 'fx.bpm' : 'fx.time', c = byId.get(id), value = Math.round(bpmMode ? 60000 / ms : ms);
    if (value < c.min || value > c.max) return toast('节拍超出当前范围', true);
    await sendControl(c, value);
  }, 'primary')));
}
function buildProfiles() {
  const name = el('input', {type: 'text', placeholder: '配置名称，例如：直播 / 录音', maxlength: 80, 'aria-label': '配置名称'});
  const save = button('保存', async () => {try {const result = await api('profiles', {name: name.value, outputBank: selectedBank}); name.value = ''; toast('已保存：' + result.name); await loadProfiles();} catch (e) {toast(e.message, true);}}, 'primary');
  const file = el('input', {type: 'file', accept: '.json', hidden: true}); file.addEventListener('change', async () => {try {if (!file.files[0]) return; if (file.files[0].size > 262144) throw new Error('配置文件过大'); await applyProfile(JSON.parse(await file.files[0].text()));} catch (e) {toast(e.message, true);} finally {file.value = '';}});
  const exportButton = button('导出当前配置', async () => {try {download(await api('snapshot' + (selectedBank == null ? '' : '?bank=' + selectedBank)), 'KB22-' + new Date().toISOString().slice(0, 10) + '.json');} catch (e) {toast(e.message, true);}});
  $('page-profiles').append(el('div', {class: 'cards'},
    el('section', {class: 'card full'}, el('h2', {text: '保存当前配置'}), el('p', {class: 'muted', text: '保存当前模式的参数和选中的输出记忆槽；专业模式还包含三组混音。未回传参数使用已发送值或电脑保存值，未知项会列入缺失清单。'}), el('div', {class: 'input-line'}, name, save), el('div', {id: 'profile-list', class: 'profile-list'})),
    el('section', {class: 'card full'}, el('h2', {text: '配置文件'}), el('p', {class: 'muted', text: '导入时会先校验模式、参数范围和混音完整性，确认后写入。'}), el('div', {class: 'actions'}, exportButton, button('导入配置', () => file.click()), file), el('p', {id: 'profile-coverage', class: 'hint'}))));
}
async function loadProfiles() {
  try {const {profiles} = await api('profiles'); $('profile-list').replaceChildren(...profiles.map(p => el('div', {class: 'profile-entry'}, el('div', {}, el('b', {text: p.name}), el('small', {text: (p.mode || '').toUpperCase() + ' · ' + new Date(p.time * 1000).toLocaleString()})), el('div', {class: 'actions'}, button('应用', async () => {try {await applyProfile(await api('profiles/' + p.id));} catch (e) {toast(e.message, true);}}), button('导出', async () => {try {download(await api('profiles/' + p.id), p.name + '.json');} catch (e) {toast(e.message, true);}}),
      button('重命名', async () => {const name = await askName('配置名称', p.name); if (name) manageProfile({id:p.id, action:'rename', name});}),
      button('覆盖', async () => {if (await confirmAction('覆盖配置', '用当前参数替换“' + p.name + '”？')) manageProfile({id:p.id, action:'replace', outputBank:selectedBank});}),
      button('删除', async () => {if (await confirmAction('删除配置', '删除电脑中保存的“' + p.name + '”？')) manageProfile({id:p.id, action:'delete'});})
    ))));
    if (!profiles.length) $('profile-list').append(el('p', {class: 'empty', text: '还没有保存的配置'}));
  } catch (e) {toast(e.message, true);}
}
async function manageProfile(payload) {try {await api('profiles/manage', payload); await loadProfiles(); toast('配置已更新');} catch (e) {toast(e.message, true);}}
async function applyProfile(snapshot) {
  const preview = await api('import', {snapshot, preview: true});
  const warning = preview.confirm.length ? ' 其中包含开启 48V 幻象电源，请确认麦克风支持。' : '';
  if (!await confirmAction('应用配置', '即将写入 ' + preview.controls + ' 个参数' + (preview.mixer ? '和三组混音' : '') + '，会改变当前音频设置。' + warning)) return;
  await api('import', {snapshot, preview: false, acknowledged: preview.confirm}); await refresh(true); toast('配置已发送，请核对各项回读状态');
}
function navigate(page, writeHash = true) {
  if (!PAGE_INFO[page]) page = 'input'; currentPage = page;
  window.scrollTo({top:0,left:0,behavior:'instant'});
  document.body.dataset.page=page;
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + page));
  $('navigation').querySelectorAll('button').forEach(b => {b.classList.toggle('active', b.dataset.pages?.split(' ').includes(page));b.setAttribute('aria-current',b.dataset.pages?.split(' ').includes(page)?'page':'false');});
  $('mobile-navigation')?.querySelectorAll('button').forEach(b=>{const active=b.dataset.pages.split(' ').includes(page);b.classList.toggle('active',active);b.setAttribute('aria-current',active?'page':'false');});
  const [title, eyebrow, description] = PAGE_INFO[page]; $('page-title').textContent = title; $('page-eyebrow').textContent = eyebrow; $('page-description').textContent = description;
  renderPageDescription(); renderSectionNavigation();
  if (writeHash) history.replaceState(null, '', '#' + page);
  if (page === 'profiles' && built) loadProfiles();
  requestAnimationFrame(drawEq);
}
function renderPageDescription() {
  if (currentPage==='input'){$('page-description').textContent=state?.mode==='hifi'?'选择 HiFi 数字音频输入。':PAGE_INFO.input[2];return;}
  if (currentPage !== 'mixer') return;
  const game = state?.mode === 'game';
  $('page-description').textContent = game ? '分别设置输入和电脑播放到耳机、OTG 与录音通道的路由。' : PAGE_INFO.mixer[2];
  $('page-eyebrow').textContent = game ? 'SIGNAL ROUTING' : 'MIX A / B / C';
}
function buildMobileNavigation(){
  const nav=el('nav',{id:'mobile-navigation','aria-label':'手机控制页面'});
  for(const [pages,label,iconName] of NAV_GROUPS){
    const b=button('',()=>navigate(navMemory[label] || pages.split(' ')[0]));
    b.dataset.pages=pages;b.append(icon(iconName),el('span',{text:label}));nav.append(b);
  }
  document.body.append(nav);
}
function renderSectionNavigation(){
  const group=NAV_GROUPS.find(([pages])=>pages.split(' ').includes(currentPage));
  const nav=$('section-navigation');nav.replaceChildren();
  nav.hidden=!group || group[0].split(' ').length===1;
  if(nav.hidden)return;
  navMemory[group[1]]=currentPage;
  for(const page of group[0].split(' ')) {
    const b=button(PAGE_INFO[page][0],()=>navigate(page),page===currentPage?'active':'');
    b.setAttribute('aria-current',page===currentPage?'page':'false');nav.append(b);
  }
}
function disclosure(card,label,ids){
  const rows=[...card.querySelectorAll(':scope > .control-row')].filter(n=>ids.includes(n.dataset.control));
  if(!rows.length)return;
  const detail=el('details',{class:'advanced-settings'},el('summary',{text:label}),rows);card.append(detail);
}
function refinePageGroups(){
  for(const editor of document.querySelectorAll('.eq-editor')) {
    const extra=[...editor.querySelectorAll('.eq-masters > [data-control]')].filter(n=>['hifi.eqCount','hifi.preset'].includes(n.dataset.control));
    if(extra.length){const detail=el('details',{class:'advanced-settings eq-memory-tools'},el('summary',{text:'设备槽位与左右共同段数'}),extra);editor.append(detail);}
  }
  const tap=$('page-fx').querySelector(':scope > .card');
  const delay=[...$('page-fx').querySelectorAll('.cards > .card')].find(c=>c.dataset.group==='延迟');
  if(tap&&delay){tap.classList.add('tap-inline');delay.append(tap);}
  const system=$('audio-controls')?.closest('.card');if(system){const details=el('details',{class:'system-details'},el('summary',{text:'系统音频控制'}));system.before(details);details.append(system);}

  for(const [name,ids] of [['延迟',['fx.delayBypass','fx.unit','fx.time','fx.bpm','fx.subdivision','fx.feedback','fx.delayMix']],['混响',['fx.reverb','fx.reverbMix','fx.decay','fx.room']]]) {
    const card=[...$('page-fx').querySelectorAll('.cards > .card')].find(n=>n.dataset.group===name);
    if(card){let after=card.querySelector('.card-head');for(const id of ids){const row=card.querySelector('[data-control="'+id+'"]');if(row){after.after(row);after=row;}}}
  }
  // Common controls stay visible. These are original controls moved in place.
  for(const card of document.querySelectorAll('#page-dynamics .card'))
    disclosure(card,'时间与高级设置',[...card.querySelectorAll('[data-control]')].map(n=>n.dataset.control).filter(id=>/\.(attack|release|mix|autoGain|map)$/.test(id)));
  for(const card of document.querySelectorAll('#page-fx .card'))
    disclosure(card,'更多设置',['fx.pan1','fx.pan2','fx.process','fx.width','fx.channel','fx.pingpong']);
  for(const card of document.querySelectorAll('#page-output .routing-card'))
    disclosure(card,'更多输出设置',['output.options','output.crossfeed','hifi.options','hifi.pcm','hifi.volumeStep','hifi.polarity',...['1','2','3'].flatMap(n=>['loopback.'+n+'.left','loopback.'+n+'.right'])]);
  const dynamics=$('page-dynamics'), tabs=el('div',{class:'tabs local-tabs','aria-label':'动态处理通道'});
  let chosen='1';
  const show=()=>{for(const card of dynamics.querySelectorAll('.cards > .card')) card.classList.toggle('section-hidden',card.dataset.group.startsWith('IN ')&&!card.dataset.group.startsWith('IN '+chosen) || chosen!=='duck'&&card.dataset.group==='闪避 Ducking' || chosen==='duck'&&card.dataset.group.startsWith('IN '));};
  for(const [value,label] of [['1','IN 1'],['2','IN 2'],['duck','闪避']]){const b=button(label,()=>{chosen=value;tabs.querySelectorAll('button').forEach(n=>n.classList.toggle('active',n===b));show();},value===chosen?'active':'');tabs.append(b);}
  dynamics.prepend(tabs);show();
  // The app-level technical explanation remains available without dominating strips.
  const meta=$('mixer-meta'), info=el('details',{class:'mixer-info pro-only'},el('summary',{text:'混音状态'}));meta.before(info);info.append(meta);
  for(const page of ['output','eq']){const bank=$('page-'+page).querySelector('.bank-selector');if(bank){const advanced=el('details',{class:'bank-details'},el('summary',{text:'输出记忆槽'}));bank.before(advanced);advanced.append(bank);}}
}

function build() {
  for (const page of ['input','mixer','output','eq','dynamics','fx','overview','device','profiles']) {

    $('pages').append(el('div', {id: 'page-' + page, class: 'page'}));
  }
  for(const [pages,label,iconName] of NAV_GROUPS){const b=button('',()=>navigate(navMemory[label]||pages.split(' ')[0]));b.dataset.pages=pages;b.append(icon(iconName),el('span',{text:label}));$('navigation').append(b);}
  document.querySelector('.page-heading').after(el('nav',{id:'section-navigation','aria-label':'当前分区',hidden:true}));
  buildOverview(); buildSchemaPages(); buildInputTools(); buildMixer(); buildEq(); buildProfiles(); buildModeAndBanks(); buildMonitor();buildMobileNavigation(); refinePageGroups(); refineGameRouting(); cacheMeterViews(); built = true;
  document.querySelector('.page-heading').append(document.querySelector('.top-actions'));
  for (const page of ['dynamics', 'eq', 'fx']) $('page-' + page).append(el('p', {id: 'unavailable-' + page, class: 'notice hidden'}));
  document.querySelector('.page-heading').append(el('span', {id: 'preview-notice', class: 'preview-indicator hidden', text: '离线预览',title:'使用模拟设备数据与电平，操作不会控制真实声卡。'}));
  navigate(PAGE_INFO[location.hash.slice(1)] ? location.hash.slice(1) : 'input', false);
}
function render() {
  if (!state) return; const connected = serverOnline && state.device.connected;
  $('connection').textContent = connected ? 'KB22 已连接' : serverOnline ? '等待 KB22' : '服务未连接';
  $('connection').className = 'connection ' + (connected ? 'connected' : 'offline');
  $('mode-label').textContent = ({pro: 'PRO', game: 'GAME', hifi: 'HiFi'})[state.mode] || '模式未知';
  $('rate-label').textContent = rateText(state.audio?.sampleRate?.value);
  $('sync').disabled = !serverOnline || changingMode || state.device.syncing;
  const error = !serverOnline ? 'Mac 服务未连接。请保持 Mac 开机并检查 Wi-Fi。' : !connected ? state.device.error || '请连接 KB22 的 USB 接口。' : '';
  $('connection-error').textContent = error; $('connection-error').classList.toggle('hidden', !error);
  $('sync-time').textContent = state.device.lastReport ? '最近回读 ' + new Date(state.device.lastReport * 1000).toLocaleTimeString() : '尚未收到设备数据';
  renderControls(); renderOverview(); renderMixer(); renderInputTools();
  $('preview-notice').classList.toggle('hidden', !state.preview);
  for (const page of ['dynamics', 'eq', 'fx']) {
    const note = $('unavailable-' + page), unavailable = !definitions.some(c => c.page === page && applicable(c));
    note.classList.toggle('hidden', !unavailable);
    note.textContent = state.mode ? page === 'eq' && state.mode === 'game' ? '电竞模式通过“输出与回环”页面的声音增强调整音色。参数均衡器用于专业录音和 HiFi 模式。' : '当前模式不提供此处理模块。可在“设备”页面切换工作模式。' : '正在等待设备工作模式。';
  }
  renderPageDescription();
  $('mode-select').value = state.mode || ''; $('mode-select').disabled = !connected || changingMode || state.device.syncing;
  $('factory-reset').disabled = !connected || changingMode || state.device.syncing;
  document.querySelectorAll('.bank-selector').forEach(n => {n.hidden = state.mode !== 'pro'; n.querySelector('select').value = selectedBank ?? '';});
  document.querySelectorAll('.pro-only').forEach(n => {n.hidden = state.mode !== 'pro';});
  const relevant = definitions.filter(applicable), known = relevant.filter(c => state.device.values[c.id]?.value != null);
  $('profile-coverage').textContent = '当前模式有 ' + known.length + ' / ' + relevant.length + ' 项数值可保存。';
}
async function refresh(force = false) {
  if (busy.size || mixerBusy || (refreshing && !force)) return; refreshing = true;
  const generation = ++refreshGeneration, requestedBank = selectedBank;
  try {
    if (!built) {const result = await api('schema'); definitions = result.controls; byId = new Map(definitions.map(c => [c.id, c])); sources = result.mixerSources; build();}
    const next = await api('state' + (requestedBank == null ? '' : '?bank=' + requestedBank));
    if (generation !== refreshGeneration) return;
    if (state?.mode && next.mode && state.mode !== next.mode) {selectedBank = null;meterPeaks.clear();meterAnimations.clear();meterSnapshotTime=0;}
    state = next; serverOnline = true; render();startMeterStream();
  } catch (e) {
    if (generation !== refreshGeneration) return;
    serverOnline = false; if (state) render(); else {$('connection').textContent = '服务未连接'; $('connection-error').textContent = e.message; $('connection-error').classList.remove('hidden');}
  } finally {if (generation === refreshGeneration) refreshing = false;}
}
let meterStreamController=null,meterStreamRetry=null;
function stopMeterStream(){clearTimeout(meterStreamRetry);if(meterStreamController){meterStreamController.abort();meterStreamController=null;}}
async function startMeterStream(){
  if(meterStreamController||!state||!serverOnline||document.hidden||$('token-dialog').open)return;
  const controller=new AbortController();meterStreamController=controller;
  try{
    const response=await fetch('/api/meters/stream',{headers:{'X-KB22-Token':token},signal:controller.signal,cache:'no-store'});
    if(!response.ok||!response.body)throw new Error('Meter stream unavailable');
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
    while(!controller.signal.aborted){const result=await reader.read();if(result.done)break;buffer+=decoder.decode(result.value,{stream:true});let boundary;while((boundary=buffer.indexOf('\n\n'))>=0){const event=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);for(const line of event.split('\n'))if(line.startsWith('data: ')){try{ingestMeterSnapshot(JSON.parse(line.slice(6)));}catch{}}}}
  }catch{}finally{if(meterStreamController===controller){meterStreamController=null;if(!document.hidden)meterStreamRetry=setTimeout(startMeterStream,1000);}}
}
$('token-form').addEventListener('submit', event => {
  event.preventDefault(); let value = $('token-input').value.trim();
  if (/^https?:\/\//.test(value)) {try {const url = new URL(value); value = new URLSearchParams(url.hash.slice(1)).get('token') || url.searchParams.get('token') || '';} catch {return toast('访问地址无效', true);}}
  stopMeterStream();token = value; localStorage.setItem('kb22-token', token); $('token-dialog').close(); refresh(true);
});
$('token-cancel').addEventListener('click', () => $('token-dialog').close());
$('change-token').addEventListener('click', showToken);
$('sync').addEventListener('click', async () => {try {$('sync').disabled = true; await writeQueue.idle(); const result = await api('sync', {}); await refresh(true); toast(result.device?.reconnecting ? '正在重新连接设备' : '已重新读取设备');} catch (e) {toast(e.message, true);} finally {render();}});
window.addEventListener('hashchange', () => navigate(location.hash.slice(1), false));
window.addEventListener('resize', () => requestAnimationFrame(drawEq));
document.addEventListener('visibilitychange', () => {if (!document.hidden){refresh();startMeterStream();}else stopMeterStream();});
window.addEventListener('pagehide',stopMeterStream);
refresh();
// Settings reports are cached in memory and do not trigger USB uploads. Poll
// at 500 ms so a change made on the hardware or the desktop app reaches the
// phone promptly while leaving the 60 fps meter stream independent.
setInterval(() => {if (!document.hidden && !$('token-dialog').open) refresh();}, 500);
requestAnimationFrame(animateMeters);
