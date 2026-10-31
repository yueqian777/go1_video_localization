'use strict';
(() => {
if (!globalThis.Go1Status) {
  document.getElementById('link-label').textContent = '状态模块未加载 · 请重启电脑服务';
  const warning = document.getElementById('link-warning');
  warning.hidden = false;
  warning.textContent = '状态模块未加载，可能仍是旧后台或静态资源加载失败。请退出旧服务、重新运行 start_pc.cmd，再刷新网页；当前禁用控制。';
  document.querySelectorAll('button,input,select').forEach(element => { element.disabled = true; });
  return;
}
const $ = id => document.getElementById(id);
const localToken = document.querySelector('meta[name="local-token"]').content;
const browserId = Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join('');
let seq = 0, tick = 0, captured = false, held = '', state = {}, polling = false, heartbeatBusy = false;
let lastMessage = '', lastStateAt = 0, wasConnected = false, wasControlling = false;
let lastHeartbeatErrorAt = -2000;
const localEvents = [];
const appVersion = Go1Status.VERSION;
const fmt = (value, digits = 2) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
function event(message) { localEvents.unshift(new Date().toLocaleTimeString()+'　'+message); localEvents.splice(10); renderEvents(); }
function renderEvents() { const all = [...localEvents, ...(state.events || []).slice().reverse().map(e => e.time+'　'+e.message)].slice(0, 12); $('events').replaceChildren(...all.map(text => { const p = document.createElement('p'); p.textContent = text; return p; })); }
async function send(action, extra = {}, quiet = false) {
  const body = {action, browser: browserId, seq: ++seq, tick, ...extra};
  try {
    const response = await fetch('/api/control', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Go1-Local-Token': localToken}, body: JSON.stringify(body), keepalive: ['disarm', 'stop'].includes(action)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求被拒绝');
    if (!quiet && !['heartbeat', 'stop', 'disarm'].includes(action)) event('已发送：'+action+'（以接收端反馈为准）');
    return true;
  } catch (error) { if (!quiet) event(error.message); return false; }
}
function capture(value) { captured = value; if (!value) held = ''; $('capture-state').textContent = value ? '键盘已捕获' : '键盘未捕获'; $('capture-state').classList.toggle('captured', value); $('capture').textContent = value ? '暂停键盘捕获' : '启用键盘捕获'; renderKeys(); }
function renderKeys() { document.querySelectorAll('.move').forEach(b => b.classList.toggle('pressed', b.dataset.key === held)); }
function stop(disarm = false) { held = ''; if (disarm) capture(false); renderKeys(); void send(disarm ? 'disarm' : 'stop', {}, true); }
function movement(key) { if (!Go1Status.describe(state).motionAllowed || !captured || performance.now()-lastStateAt > Go1Status.NETWORK_TIMEOUT_MS) return; held = key; renderKeys(); void heartbeat(); }
async function heartbeat() { if (state.app_version !== appVersion || !captured || !state.connected || heartbeatBusy || document.hidden || performance.now()-lastStateAt > Go1Status.NETWORK_TIMEOUT_MS) return; heartbeatBusy = true; try { const ok = await send('heartbeat', {key: held}, true); if (!ok && held && performance.now()-lastHeartbeatErrorAt > 2000) { lastHeartbeatErrorAt = performance.now(); event('按键心跳发送失败，请查看本机输入间隔和停止原因'); } } finally { heartbeatBusy = false; } }
function render(s) {
  const view = Go1Status.describe(s), {r,fresh,measured,compatible} = view;
  if ((wasConnected && !s.connected) || (fresh && wasControlling && !r.control_active)) $('standing').checked = false;
  wasConnected = s.connected; if (fresh) wasControlling = !!r.control_active;
  $('standing').disabled = !fresh || !!r.control_active;
  if ((!fresh || view.recovery || !compatible) && captured) capture(false);
  $('address').textContent = s.address || '等待本机服务'; $('link-label').textContent = view.linkLabel; $('link-dot').classList.toggle('online', fresh);
  $('link-warning').textContent = view.warning; $('link-warning').hidden = !view.warning;
  $('connect').disabled = s.connected || s.connecting || s.local_service_available === false; $('disconnect').disabled = !s.connected && !s.connecting;
  $('mode-banner').classList.toggle('sim', s.simulation === true);
  $('mode-banner').textContent = s.simulation ? 'SIMULATION ONLY / 离线模拟 · 当前只连接电脑上的模拟接收端，不连接机器狗。' : 'REAL ROBOT / 实机配置 · 接管后会发送高层控制帧。请确认已拔充电器、现场有人看护。';
  $('soc').replaceChildren(document.createTextNode(measured ? r.soc : '—'), Object.assign(document.createElement('small'), {textContent: '%'}));
  $('battery-fill').style.width = measured ? Math.max(0, Math.min(100, r.soc))+'%' : '0%';
  const moving = view.moving;
  $('activity').textContent = view.activity;
  $('health').textContent = view.health;
  $('age').replaceChildren(document.createTextNode(s.telemetry_age_ms == null ? '—' : fmt(s.telemetry_age_ms, 0)), Object.assign(document.createElement('small'), {textContent: 'ms'}));
  $('rtt').textContent = '往返估计 '+fmt(s.rtt_ms, 0)+' ms（含上报周期）';
  $('avoidance').textContent = r.avoidance_selected ? 'ON' : 'OFF';
  $('gait').textContent = !measured ? 'UNKNOWN / 无新鲜反馈' : !moving ? r.feedback_mode === 2 && r.feedback_gait === 4 ? 'WAIT_CLEAR / 等待退出步态4' : 'IDLE / 当前未请求避障行走' : r.gait_feedback === 'ACKED' ? 'ACKED / 仅确认步态 '+r.feedback_gait : 'PENDING / 等待固件确认';
  for (const axis of ['vx', 'vy', 'yaw']) $(axis).textContent = (view.active ? fmt(r['command_'+axis], 3) : '—')+' / '+(measured ? fmt(r['actual_'+axis], 3) : '—');
  $('height').textContent = measured ? fmt(r.height)+' m' : '—'; $('pose').textContent = measured ? fmt(r.roll)+' / '+fmt(r.pitch)+' rad' : '—'; $('temperature').textContent = measured ? fmt(r.max_temperature, 0)+' °C' : '—';
  $('mode').textContent = measured ? r.feedback_mode+' / '+r.feedback_gait : '—'; $('ready').textContent = view.active ? fmt(Math.min(500, r.ready_ms || 0), 0)+' / 500 ms' : '—'; $('ttl').textContent = view.active ? fmt(r.ttl_ms, 0)+' ms / 已持续 '+fmt(r.motion_elapsed_s, 1)+' s' : '—';
  $('fault').textContent = view.faultText; $('reason').textContent = view.reasonText; document.querySelector('.fault').classList.toggle('danger', view.currentFault);
  const labels = {USER_STOP:'松键 / 停止',DIRECTION_CHANGE:'切换方向',FEEDBACK_NOT_SETTLED:'启动或偏移反馈未满足',COMMAND_TTL_EXPIRED:'运动指令续发中断',UI_INACTIVE:'浏览器输入中断',UI_DISARM:'解除解锁',STARTUP:'尚无运动停止'};
  const input = s.input_diagnostics || {};
  $('motion-diagnostics').textContent = view.active ? '最近停止：'+(labels[r.last_stop_reason] || r.last_stop_reason || '—')+'；TTL 中断 '+fmt(r.ttl_expirations,0)+' 次；指令最大间隔 '+fmt(r.max_move_gap_ms,0)+' ms；本机按键心跳最大间隔 '+fmt(input.max_heartbeat_gap_ms,0)+' ms。' : '接管后显示停止原因、运动指令间隔和浏览器输入间隔；不会为显示这些数据自动接管。';
  $('history-status').hidden = !view.historyText; $('history-message').textContent = view.historyText;
  $('robot-status').textContent = view.stage;
  document.body.classList.toggle('moving', !!moving && fresh);
  $('take').disabled = !fresh || !!r.control_active || !!r.stopping || !compatible || !view.receiverCompatible;
  for (const id of ['speed', 'lateral', 'turn']) $(id).disabled = !!r.control_active || (s.connected && !fresh);
  $('speed-summary').textContent = r.control_active ? '接收端生效设定：前后 '+fmt(r.speed_vx, 2)+' m/s · 横移 '+fmt(r.speed_vy, 2)+' m/s · 转向 '+fmt(r.speed_yaw, 2)+' rad/s。修改需先 Q，再重新接管。' : '横移、转向独立选档；角速度不是固定转角。更改参数需先 Q 释放，再重新接管。';
  for (const id of ['arm', 'avoid', 'reset', 'release']) $(id).disabled = !fresh || !r.control_active;
  $('arm').disabled = $('arm').disabled || !compatible || !view.receiverCompatible;
  $('capture').disabled = !fresh || !view.active || !compatible;
  document.querySelectorAll('.move').forEach(b => { b.disabled = !view.motionAllowed; });
  if (!view.motionAllowed && held) { held = ''; renderKeys(); }
  if (r.message && r.message !== lastMessage && r.message !== 'COMMAND_ACCEPTED') { event(r.message); lastMessage = r.message; }
  renderEvents();
}
async function poll() { if (polling) return; polling = true; try { const response = await fetch('/api/state', {cache: 'no-store', signal: AbortSignal.timeout(Go1Status.NETWORK_TIMEOUT_MS)}); if (!response.ok) throw Error('本机状态服务异常'); const s = await response.json(); tick = s.browser_tick; state = s; lastStateAt = performance.now(); render(s); } catch (_) { if (state.connected) event('本机服务失联；停止续发运动'); state = {...state, connected:false, connecting:false, robot:{}, telemetry_age_ms:null, local_service_available:false, recovery_required:false}; capture(false); render(state); } finally { polling = false; } }
$('connect').onclick = () => { capture(false); $('standing').checked = false; void send('connect'); };
$('disconnect').onclick = () => { stop(true); $('standing').checked = false; void send('disconnect'); };
$('take').onclick = async () => { if (!$('standing').checked) { event('请先确认已站立、已拔充电器且手柄可用'); return; } capture(true); await send('take', {standing: true, speed: Number($('speed').value), lateral: Number($('lateral').value), turn: Number($('turn').value)}); };
$('capture').onclick = () => { if (captured) stop(true); else capture(true); };
$('arm').onclick = () => { capture(true); void send('arm'); };
$('reset').onclick = () => { held = ''; capture(true); void send('reset'); };
$('avoid').onclick = () => { held = ''; capture(true); void send('avoid'); };
$('release').onclick = () => { held = ''; capture(false); $('standing').checked = false; void send('release'); };
$('stop').onclick = () => stop();
document.querySelectorAll('.move').forEach(button => { button.addEventListener('pointerdown', e => { e.preventDefault(); capture(true); button.setPointerCapture(e.pointerId); movement(button.dataset.key); }); button.addEventListener('pointerup', () => { stop(); void heartbeat(); }); button.addEventListener('pointercancel', () => stop(true)); });
window.addEventListener('keydown', e => {
  const key = e.key.toLowerCase();
  if ((key === ' ' || key === 'x') && (captured || (state.robot || {}).control_active)) { e.preventDefault(); stop(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey || ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName) || !captured) return;
  if (!'wsjlzcaorq'.includes(key) || key.length !== 1) return; e.preventDefault();
  if ('wsjlzc'.includes(key)) { if (!e.repeat) movement(key); return; }
  if (e.repeat) return;
  if (key === 'q') { capture(false); void send('release'); }
  else if (key === 'a') void send('arm'); else if (key === 'r') { held = ''; void send('reset'); } else if (key === 'o') { held = ''; void send('avoid'); }
});
window.addEventListener('keyup', e => { if (e.key.toLowerCase() === held) { stop(); void heartbeat(); } });
window.addEventListener('blur', () => stop(true));
document.addEventListener('visibilitychange', () => { if (document.hidden) stop(true); });
window.addEventListener('pagehide', () => stop(true));
setInterval(poll, 100); setInterval(heartbeat, 50); void poll();
})();
