"use strict";

// Recover only the local browser session. Never replay camera/recording actions.
class CaptureSession {
  constructor(fetcher, onRecovery = () => {}) {
    this.fetcher = fetcher;
    this.onRecovery = onRecovery;
    this.csrf = "";
    this.instanceId = null;
    this.renewing = null;
  }

  async response(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await this.fetcher(path, {...options, signal: controller.signal,
        credentials: "same-origin", cache: "no-store"});
      if (!response.ok) {
        let code = `HTTP ${response.status}`;
        try { code = (await response.json()).error?.code || code; } catch { /* Non-JSON error. */ }
        const error = new Error(code);
        error.status = response.status;
        error.code = code;
        throw error;
      }
      return response;
    } finally {
      clearTimeout(timer);
    }
  }

  isExpired(error) {
    return error.status === 403 && ["local_session_required", "csrf_required"].includes(error.code);
  }

  async renew() {
    if (this.renewing) return this.renewing;
    const existed = Boolean(this.csrf);
    this.renewing = (async () => {
      const response = await this.response("/api/v1/session", {headers: {"X-Go1-Session": "renew"}});
      const {data} = await response.json();
      if (!data?.csrf || !data.instance_id) throw new Error("invalid_local_session_reply");
      this.csrf = data.csrf;
      this.instanceId = data.instance_id;
      if (existed) this.onRecovery();
    })();
    try { await this.renewing; } finally { this.renewing = null; }
  }

  async readResponse(path) {
    if (!this.csrf) await this.renew();
    try {
      return await this.response(path);
    } catch (error) {
      if (!this.isExpired(error)) throw error;
      await this.renew();
      return this.response(path); // At most one retry, GET only.
    }
  }

  async readJSON(path) {
    return (await (await this.readResponse(path)).json()).data;
  }

  async action(data) {
    if (!this.csrf) await this.renew();
    try {
      const response = await this.response("/api/v1/actions", {method: "POST",
        headers: {"Content-Type": "application/json", "X-Go1-CSRF": this.csrf}, body: JSON.stringify(data)});
      return (await response.json()).data;
    } catch (error) {
      if (!this.isExpired(error)) throw error;
      await this.renew();
      throw new Error("session_renewed_action_not_replayed");
    }
  }
}

if (typeof module !== "undefined" && module.exports) module.exports = {CaptureSession};
