const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const script = fs.readFileSync(path.join(__dirname, '../src/go1loc/capture_frontend/workspace.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', hidden: true, disabled: false, textContent: '', attrs: {}, events: {}, children: [],
      setAttribute(key, value) { this.attrs[key] = value; },
      getAttribute(key) { return this.attrs[key]; },
      addEventListener(key, handler) { this.events[key] = handler; },
      replaceChildren(...children) { this.children = children; this.value = children[0]?.value || ''; },
      add(child) { this.children.push(child); this.value = child.value; },
    });
    return elements.get(id);
  };
  const model = {
    data: {enabled: true, viewer_url: 'http://127.0.0.1:18873/', map_ready: true,
      current_pose_available: false, navigation_status: 'waiting', build_status: 'ready',
      recordings: [{capture_id: 'real', camera_id: 6, frames: 20, source_kind: 'orbbec_rgb_mjpeg'},
        {capture_id: 'demo', camera_id: 1, frames: 20, source_kind: 'synthetic_test'}]},
    actions: [], timers: [], unavailable: false,
  };
  const context = {
    document: {getElementById: element, querySelector: element, title: ''},
    currentState: {live: false, mode: 'robot', busy: false}, exiting: false, pending: false,
    updateControls: () => {},
    session: {
      readJSON: async () => { if (model.unavailable) throw Error('offline'); return model.data; },
      action: async data => { model.actions.push(data); },
    },
    Option: function(text, value) { this.text = text; this.value = value; }, URL,
    setTimeout: callback => model.timers.push(callback), errorText: text => text,
  };
  vm.runInNewContext(script, context);
  return {model, context, element, refresh: async () => { model.timers.shift()(); await settle(); }};
}

test('workspace startup lists real recordings but never connects, records or starts navigation', async () => {
  const h = harness(); await settle();
  assert.equal(h.element('workspace-bar').hidden, false);
  assert.deepEqual(h.element('workspace-recordings').children.map(x => x.value), ['real']);
  assert.equal(h.element('workspace-live').disabled, true);
  assert.deepEqual(h.model.actions, []);
  h.element('tab-map').events.click();
  assert.equal(h.element('workspace-map').src, 'http://127.0.0.1:18873/');
  assert.equal(h.element('capture-main').hidden, true);
  assert.equal(h.element('map-main').hidden, false);
});

test('one explicit handoff selects recording and opens map without starting reconstruction', async () => {
  const h = harness(); await settle();
  await h.element('workspace-build').events.click();
  assert.equal(h.context.pending, true);
  assert.equal(h.model.actions.length, 1);
  assert.equal(h.model.actions[0].action, 'mapping-recording');
  assert.equal(h.model.actions[0].capture_id, 'real');
  h.context.currentState.action = 'mapping-recording';
  await h.refresh();
  assert.equal(h.context.pending, false);
  assert.match(h.element('workspace-message').textContent, /没有自动启动重建/);
  await h.refresh();
  assert.equal(h.model.actions.length, 1);
});

test('lost workspace status removes current-position claim, disables handoff, never replays actions', async () => {
  const h = harness(); await settle();
  h.context.currentState.live = true; h.model.data.current_pose_available = true;
  await h.refresh();
  assert.equal(h.element('workspace-live').disabled, false);
  assert.match(h.element('workspace-status').textContent, /当前源帧已定位/);
  h.model.unavailable = true; await h.refresh();
  assert.equal(h.element('workspace-live').disabled, true);
  assert.equal(h.element('workspace-build').disabled, true);
  assert.match(h.element('workspace-status').textContent, /不显示旧位置/);
  assert.deepEqual(h.model.actions, []);
});

test('failed handoff explains error and never activates a remote iframe', async () => {
  const h = harness(); await settle();
  await h.element('workspace-live').events.click();
  Object.assign(h.context.currentState, {action: 'navigation-live', action_error: '相机已断开'});
  await h.refresh();
  assert.equal(h.element('workspace-message').textContent, '相机已断开');
  assert.equal(h.element('workspace-map').src, undefined);
  h.model.data.viewer_url = 'https://outside.example/';
  await h.refresh();
  assert.throws(() => h.element('tab-map').events.click(), /invalid_local_viewer/);
});
