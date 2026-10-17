"use strict";

// Keep one serial poll: slow requests never create an accumulating work queue.
function nextPollDelay(state, elapsedMs, failed = false) {
  if (failed) return 2000;
  const active = ["previewing", "recording", "finalizing"].includes(state);
  return Math.max(10, (active ? 50 : 500) - elapsedMs);
}

class PreviewRate {
  constructor() { this.reset(); }
  reset() { this.samples = []; this.key = null; }
  observe(key, now) {
    if (!key || key === this.key) return;
    this.key = key;
    this.samples = [...this.samples.filter(t => now - t <= 2000), now].slice(-121);
  }
  value(now) {
    const samples = this.samples.filter(t => now - t <= 2000);
    const span = samples.length > 1 ? samples[samples.length - 1] - samples[0] : 0;
    return span > 0 ? (samples.length - 1) * 1000 / span : 0;
  }
}

if (typeof module !== "undefined") module.exports = {nextPollDelay, PreviewRate};
