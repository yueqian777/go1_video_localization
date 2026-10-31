'use strict';
// Pure presentation model: historical observations never grant control authority.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Go1Status = api;
})(globalThis, function () {
  const VERSION = '0.3.4-20261029';
  const NETWORK_TIMEOUT_MS = 3000;
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  function describe(s) {
    const local = s.local_service_available !== false;
    const connected = local && s.connected === true;
    const fresh = connected && finite(s.telemetry_age_ms) && s.telemetry_age_ms >= 0 && s.telemetry_age_ms <= NETWORK_TIMEOUT_MS;
    const r = fresh ? s.robot || {} : {};
    const active = fresh && r.control_active === true;
    const currentFault = active && r.fault === true;
    const compatible = s.app_version === VERSION;
    const networkCompatible = r.configurable_speeds === true && r.control_timeout_ms === NETWORK_TIMEOUT_MS && r.command_echo_ms === NETWORK_TIMEOUT_MS;
    const holdCompatible = networkCompatible && r.hold_to_move === true && r.continuous_limit_s === null;
    const steadyCompatible = holdCompatible && r.steady_motion_renewal === true;
    const receiverCompatible = steadyCompatible && r.startup_ack_ms === 1500;
    const recovery = active && s.recovery_required === true;
    const measured = active && r.has_state === true && finite(r.state_age_ms) && r.state_age_ms >= 0 && r.state_age_ms <= 150;
    const moving = active && !!(r.command_vx || r.command_vy || r.command_yaw);
    const history = s.history || {};
    const details = [];
    const previousFault = history.last_fault || (fresh && !active && r.fault_reason ? {reason:r.fault_reason} : null);
    if (previousFault && !currentFault) details.push('上次报告的故障'+(previousFault.time ? ' '+previousFault.time : '')+'：'+previousFault.reason);
    if (history.last_disconnect) details.push('上次断开 '+history.last_disconnect.time+'：'+history.last_disconnect.reason);
    if (history.last_network_gap) details.push('上次状态间断 '+history.last_network_gap.time+'（当时帧龄 '+history.last_network_gap.age_ms+' ms）');
    let linkLabel = !local ? '本机服务失联' : connected ? fresh ? '接收端已连接' : '状态过期 · TCP 仍连接' : s.connecting ? s.connection_stage === 'authenticating' ? '正在认证接收端' : '正在连接接收端' : '未连接';
    if (fresh && s.telemetry_age_ms > 500) linkLabel = '接收端已连接 · 反馈延迟（3 秒内）';
    let activity = !local ? '本机服务失联' : !connected ? s.connecting ? '正在连接' : '未连接' : !fresh ? '状态过期 · 已停续发' : r.stopping ? '停止释放中' : !active ? '未接管' : currentFault ? '故障锁定' : recovery ? '等待重新解锁' : r.armed ? moving ? '运动请求中' : '已解锁' : '未解锁';
    let warning = '';
    if (!local) warning = '电脑本机服务不可用，已停止续发。请启动 start_pc.cmd；当前无法判断机器狗的实时状态。';
    else if (!compatible) warning = '电脑服务仍是旧版本，请停止旧服务、重新运行 start_pc.cmd，再刷新网页。';
    else if (!connected) warning = s.connecting ? '正在建立网络连接，尚未接管 SDK。' : s.disconnect_reason || '当前未连接。机器狗关机、热点未连接或服务不可达时都无法获取实时状态；充电期间不要接管。';
    else if (!fresh) warning = '当前状态已过期，已停止续发并请求解除解锁；不代表实机已经停稳。请等待恢复，持续异常时断开后手动重连。';
    else if (!networkCompatible) warning = '机器狗接收端仍使用旧网络阈值，请更新为 3 秒超时版本后再接管。';
    else if (!holdCompatible) warning = '机器狗接收端尚未移除连续运动时限，请更新为 v0.3.1 后再接管。';
    else if (!steadyCompatible) warning = '接收端仍可能误判轻微横摆而中断续行，请更新为 v0.3.2 后再接管。';
    else if (!receiverCompatible) warning = '接收端启动确认窗口仍是旧值，请更新为 v0.3.3 后再接管。';
    else if (recovery) warning = currentFault ? '网络已恢复，但控制故障仍锁定。现场安全且健康静止后先 R，再 A；不会自动续走。' : '网络已恢复，运动仍锁定。检查现场后手动 A；不会恢复旧方向。';
    const faultText = !local || !connected ? '未知（未连接）' : !fresh ? '未知（状态过期）' : !active ? '未接管 SDK' : currentFault ? r.fault_reason || 'FAULT' : 'NONE';
    const reasonText = !connected ? '没有实时控制状态；历史记录见下方' : !fresh ? '当前反馈不可用；停止请求不等于停稳确认' : !active ? '没有当前控制会话；历史故障不代表正在故障' : r.disarm_reason || 'NONE';
    const health = !local ? '本机服务不可用' : !connected ? '接收端离线 / 无实时状态' : !fresh ? 'TCP 仍连接 / 等待新鲜状态' : !active ? '网络在线 / 未接管 SDK' : r.health || '等待 SDK 状态';
    const stage = !local ? 'PC SERVICE / OFFLINE' : !connected ? 'NETWORK / OFFLINE' : !fresh ? 'NETWORK / STALE — STOP REQUESTED' : !active ? 'NETWORK ONLINE / MONITOR ONLY' : currentFault ? 'SDK / SAFETY GATE BLOCKED' : !measured ? 'SDK / WAITING STATE' : r.armed && !recovery ? 'SDK READY / ARMED' : 'SDK READY / DISARMED';
    return {connected,fresh,r,active,currentFault,compatible,receiverCompatible,recovery,measured,moving,linkLabel,activity,warning,faultText,reasonText,health,stage,
      motionAllowed:fresh && compatible && receiverCompatible && active && measured && r.health === 'OK' && r.armed === true && !currentFault && !recovery,
      historyText:details.join('；')};
  }
  return {VERSION,NETWORK_TIMEOUT_MS,describe};
});
