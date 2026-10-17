const assert = require('node:assert/strict');
const test = require('node:test');
const {CaptureSession} = require('../src/go1loc/capture_frontend/session.js');

function fixture() {
  let generation = 1, cookie = 0, dropReply = false;
  const calls = [], actions = [], recovered = [];
  const fetcher = async (path, options) => {
    calls.push({path, method: options.method || 'GET'});
    const reply = (data, status = 200) => new Response(JSON.stringify(status === 200 ? {data} : {error: {code: data}}), {status});
    if (path === '/api/v1/session') {
      assert.equal(options.headers['X-Go1-Session'], 'renew');
      cookie = generation;
      return reply({csrf: `csrf-${generation}`, instance_id: `instance-${generation}`});
    }
    if (cookie !== generation) return reply('local_session_required', 403);
    if (options.method === 'POST') {
      if (options.headers['X-Go1-CSRF'] !== `csrf-${generation}`) return reply('csrf_required', 403);
      actions.push(JSON.parse(options.body));
      if (dropReply) throw Error('network reply lost');
      return reply({accepted: true});
    }
    return reply({state: 'disconnected', received: 0});
  };
  const client = new CaptureSession(fetcher, () => recovered.push(true));
  return {client, calls, actions, recovered, restart() {generation++;},
          otherTabRefreshes() {cookie = generation;}, dropReplies() {dropReply = true;}};
}

test('initial connection only initializes local session and reads state', async () => {
  const f = fixture();
  assert.equal((await f.client.readJSON('/api/v1/state')).state, 'disconnected');
  assert.equal(f.actions.length, 0);
  assert.equal(f.recovered.length, 0);
});
test('server restart recovers GET without reconnecting or recording', async () => {
  const f = fixture();
  await f.client.readJSON('/api/v1/state');
  f.restart();
  assert.equal((await f.client.readJSON('/api/v1/state')).received, 0);
  assert.equal(f.recovered.length, 1);
  assert.equal(f.actions.length, 0);
});
test('stale POST is not replayed after cookie renewal', async () => {
  const f = fixture();
  await f.client.readJSON('/api/v1/state');
  f.restart();
  await assert.rejects(f.client.action({action: 'start', name: 'x'}), /session_renewed_action_not_replayed/);
  assert.equal(f.actions.length, 0);
  assert.equal(f.calls.filter(c => c.method === 'POST').length, 1);
  await f.client.action({action: 'start', name: 'x'});
  assert.equal(f.actions.length, 1);
});
test('cookie shared from refreshed tab still renews stale CSRF safely', async () => {
  const f = fixture();
  await f.client.readJSON('/api/v1/state');
  f.restart();
  f.otherTabRefreshes();
  await assert.rejects(f.client.action({action: 'connect'}), /session_renewed_action_not_replayed/);
  assert.equal(f.actions.length, 0);
});
test('unknown POST outcome is never retried', async () => {
  const f = fixture();
  await f.client.readJSON('/api/v1/state');
  f.dropReplies();
  await assert.rejects(f.client.action({action: 'start'}), /network reply lost/);
  assert.equal(f.actions.length, 1);
  assert.equal(f.calls.filter(c => c.method === 'POST').length, 1);
});
test('concurrent initial reads coalesce session requests', async () => {
  const f = fixture();
  await Promise.all([f.client.readJSON('/api/v1/state'), f.client.readJSON('/api/v1/state')]);
  assert.equal(f.calls.filter(c => c.path === '/api/v1/session').length, 1);
});
test('cross-origin rejection does not enter a renewal loop', async () => {
  let calls = 0;
  const client = new CaptureSession(async () => {calls++; return new Response(JSON.stringify({error: {code: 'local_origin_required'}}), {status:403});});
  await assert.rejects(client.readJSON('/api/v1/state'), /local_origin_required/);
  assert.equal(calls, 1);
});
