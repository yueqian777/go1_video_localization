'use strict';
// Node is only used for development tests; the PC runtime still needs only Python/browser.
const assert = require('node:assert/strict');
const {VERSION,describe} = require('../pc/web/state.js');
const robot = {configurable_speeds:true,control_timeout_ms:3000,command_echo_ms:3000,hold_to_move:true,continuous_limit_s:null,steady_motion_renewal:true,startup_ack_ms:1500,control_active:true,armed:true,fault:false,health:'OK',has_state:true,state_age_ms:5};
const online = {app_version:VERSION,connected:true,connecting:false,telemetry_age_ms:10,robot,history:{}};
let passed = 0;
function test(name, callback) { callback(); passed++; console.log('PASS '+name); }
test('normal live authority stays available', () => assert.equal(describe(online).motionAllowed, true));
test('offline cached fault never shown as current', () => {
  const view = describe({...online,connected:false,robot:{...robot,fault:true,fault_reason:'LINK_TIMEOUT'},
    history:{last_fault:{time:'10:00:00',reason:'LINK_TIMEOUT'}},disconnect_reason:'connection reset'});
  assert.equal(view.linkLabel,'未连接'); assert.equal(view.currentFault,false);
  assert.match(view.faultText,/未知/); assert.match(view.historyText,/上次报告.*LINK_TIMEOUT/);
  assert.equal(view.motionAllowed,false); assert.equal(view.measured,false);
});
test('idle old receiver fault is history only', () => {
  const view = describe({...online,robot:{...robot,control_active:false,fault:true,fault_reason:'LINK_TIMEOUT'}});
  assert.equal(view.activity,'未接管'); assert.equal(view.faultText,'未接管 SDK');
  assert.equal(view.currentFault,false); assert.match(view.historyText,/LINK_TIMEOUT/);
});
test('idle recovered gap has no persistent jitter warning', () => {
  const view = describe({...online,recovery_required:true,robot:{...robot,control_active:false},
    history:{last_network_gap:{time:'10:00:00',age_ms:650}}});
  assert.equal(view.linkLabel,'接收端已连接'); assert.equal(view.warning,'');
  assert.match(view.historyText,/上次状态间断/);
});
test('active recovery keeps manual rearm without claiming current jitter', () => {
  const view = describe({...online,recovery_required:true});
  assert.match(view.warning,/网络已恢复/); assert.doesNotMatch(view.warning,/网络曾有抖动/);
  assert.equal(view.activity,'等待重新解锁'); assert.equal(view.motionAllowed,false);
});
test('real current fault is still red and locked', () => {
  const view = describe({...online,robot:{...robot,fault:true,fault_reason:'LINK_TIMEOUT'}});
  assert.equal(view.currentFault,true); assert.equal(view.activity,'故障锁定');
  assert.equal(view.faultText,'LINK_TIMEOUT'); assert.equal(view.motionAllowed,false);
});
test('more than three seconds stale is unknown and never grants motion', () => {
  const view = describe({...online,telemetry_age_ms:3001,robot:{...robot,fault:true}});
  assert.equal(view.currentFault,false); assert.equal(view.motionAllowed,false);
  assert.match(view.linkLabel,/状态过期/); assert.match(view.faultText,/未知/);
});
test('500 ms through 3 s are delayed feedback, not timeout or forced rearm', () => {
  for (const age of [500,650,1500,2500,2999,3000]) {
    const view = describe({...online,telemetry_age_ms:age});
    assert.equal(view.fresh,true); assert.equal(view.motionAllowed,true);
    assert.equal(view.recovery,false); assert.equal(view.warning,'');
    assert.doesNotMatch(view.linkLabel,/状态过期/);
  }
});
test('old receiver cannot silently keep its short echo window', () => {
  const view = describe({...online,robot:{...robot,control_timeout_ms:undefined,command_echo_ms:undefined}});
  assert.equal(view.receiverCompatible,false); assert.equal(view.motionAllowed,false);
  assert.match(view.warning,/3 秒超时版本/);
});
test('time-limited receiver cannot silently appear to support held motion', () => {
  for (const capability of [{hold_to_move:undefined,continuous_limit_s:undefined},{continuous_limit_s:2},{continuous_limit_s:undefined}]) {
    const view = describe({...online,robot:{...robot,...capability}});
    assert.equal(view.receiverCompatible,false); assert.equal(view.motionAllowed,false);
    assert.match(view.warning,/连续运动时限/);
  }
});
test('pre-drift-fix receiver cannot silently appear fixed', () => {
  const view = describe({...online,robot:{...robot,steady_motion_renewal:undefined}});
  assert.equal(view.motionAllowed,false); assert.match(view.warning,/轻微横摆/);
});
test('old short firmware ACK window requires update', () => {
  const view = describe({...online,robot:{...robot,startup_ack_ms:600}});
  assert.equal(view.motionAllowed,false); assert.match(view.warning,/启动确认窗口/);
});
test('local HTTP loss clears live fault and authority', () => {
  const view = describe({...online,local_service_available:false,robot:{...robot,fault:true}});
  assert.equal(view.linkLabel,'本机服务失联'); assert.equal(view.currentFault,false);
  assert.equal(view.motionAllowed,false); assert.equal(view.measured,false);
});
test('connecting differs from fault and jitter', () => {
  const view = describe({...online,connected:false,connecting:true,connection_stage:'authenticating'});
  assert.equal(view.linkLabel,'正在认证接收端'); assert.equal(view.currentFault,false);
});
test('null network age is not interpreted as zero', () => assert.equal(describe({...online,telemetry_age_ms:null}).fresh,false));
test('null SDK age is not interpreted as fresh telemetry', () => assert.equal(describe({...online,robot:{...robot,state_age_ms:null}}).motionAllowed,false));
test('old PC backend still cannot grant motion', () => {
  const view = describe({...online,app_version:'0.2.0-20261018'});
  assert.equal(view.compatible,false); assert.equal(view.motionAllowed,false); assert.match(view.warning,/旧版本/);
});
console.log('UI_STATE_TESTS_PASS '+passed+'/'+passed);
