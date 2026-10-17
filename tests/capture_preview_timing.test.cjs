const {test} = require('node:test');
const assert = require('node:assert/strict');
const {nextPollDelay, PreviewRate} = require('../src/go1loc/capture_frontend/preview-timing.js');

test('live preview targets 20 polls/s including request time, without a catch-up burst', () => {
  assert.equal(nextPollDelay('previewing', 12), 38);
  assert.equal(nextPollDelay('recording', 45), 10);
  assert.equal(nextPollDelay('finalizing', 90), 10);
});
test('idle and failed connections back off instead of spinning', () => {
  assert.equal(nextPollDelay('disconnected', 12), 488);
  assert.equal(nextPollDelay('connecting', 0), 500);
  assert.equal(nextPollDelay('error', 900), 10);
  assert.equal(nextPollDelay('previewing', 12, true), 2000);
});
test('preview rate counts unique loaded samples, not polls or duplicate frames', () => {
  const rate = new PreviewRate();
  assert.equal(rate.value(0), 0);
  rate.observe('stream:1', 0);
  assert.equal(rate.value(0), 0);
  rate.observe('stream:2', 100);
  rate.observe('stream:2', 110);
  assert.equal(rate.value(110), 10);
  assert.equal(rate.value(2200), 0);
  rate.reset();
  rate.observe('new:1', 2300);
  assert.equal(rate.value(2300), 0);
});
test('preview rate window and memory remain bounded', () => {
  const rate = new PreviewRate();
  for (let i = 0; i < 500; i++) rate.observe(String(i), i * 50);
  assert.ok(Math.abs(rate.value(24950) - 20) < .001);
  assert.ok(rate.samples.length <= 41);
});
