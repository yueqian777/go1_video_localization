"use strict";

const element = (id) => document.getElementById(id);
const cameraNames = ["", "前视相机", "下巴相机", "左侧相机", "右侧相机", "后下相机", "Gemini 335L RGB"];
const stateNames = {disconnected: "未连接", connecting: "连接中", previewing: "预览中", recording: "录制中", finalizing: "保存中", error: "采集中断"};
const actions = {connect: "连接相机", disconnect: "断开连接", start: "开始录制", stop: "停止录制", snapshot: "抓拍源图", export: "导出视频", "open-folder": "打开文件夹", shutdown: "退出录制台"};
const friendlyErrors = {
  rgb_camera_missing_or_ambiguous: "未找到已核验的 Gemini 335L RGB 接口，请检查这只外接相机的 USB 连接。",
  rgb_camera_busy: "外接相机正在被其他采集占用。不会强制停止其他程序。",
  rgb_capture_timeout: "外接 RGB 采集超时，已停止本次采集；请检查 USB 和网络后手动重连。",
  rgb_restore_failed: "外接 RGB 已退出，但格式恢复未确认。请检查相机连接后再继续。",
  manual_readonly_authorization_required: "请先勾选只读连接确认。",
  action_busy: "上一项操作仍在处理中，请稍候。",
  invalid_action_or_parameters: "参数无效。会话名称仅支持文字、数字、空格、下划线和短横线。",
  ssh_authentication_or_remote_permission_failed: "SSH 认证失败。请检查免密登录和权限；本页不会自动添加公钥。",
  ssh_host_key_verification_failed: "SSH 主机指纹尚未通过验证，请先按操作手册核对。",
  remote_agent_or_interpreter_missing: "板端只读代理或 Python 不存在，需要单独确认部署。",
  frame_timeout_no_live_preview: "采集接收超时，旧画面已隐藏。请检查网络或相机后手动重连。",
  snapshot_timeout_no_new_source_frame: "抓拍超时：没有收到下一次图像样本。",
  previous_writer_still_finalizing: "上一段录像仍在落盘，请稍后再开始。",
  stop_recording_before_export: "请先正常停止录像，再导出视频。",
  recording_incomplete_requires_explicit_override: "该段录像已中断，请保留 PNG，并按手册检查后再导出。",
  connection_start_failed_check_ssh_setup: "连接未能启动，请检查本机 SSH 配置。",
  session_renewed_action_not_replayed: "本机会话已恢复。为避免重复操作，刚才的命令没有自动重发，请检查状态后重新点击。",
  local_origin_required: "本机页面访问校验未通过，请从桌面快捷方式重新打开页面。"
};
let selectedCamera = 1;
let currentState = null;
let pending = false;
let exiting = false;
let previewKey = null;
let previewUrl = null;
let lastResult = "";
let sessionRecovered = false;
const previewRate = new PreviewRate();
const session = new CaptureSession(window.fetch.bind(window), () => {
  hidePreview();
  element("authorized").checked = false;
  sessionRecovered = true;
});

function errorText(code) {
  return friendlyErrors[code] || code || "操作未完成，请检查连接和启动日志。";
}

function report(message, isError = false) {
  element("activity-text").textContent = message;
  document.querySelector(".activity").classList.toggle("error", isError);
}

function selectCamera(id) {
  selectedCamera = id;
  element("camera-title").textContent = cameraNames[id];
  element("camera-tag").textContent = `CAM ${String(id).padStart(2, "0")}`;
  document.querySelectorAll("[data-camera]").forEach((button) => {
    const selected = Number(button.dataset.camera) === id;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function hidePreview() {
  element("preview").hidden = true;
  element("empty-preview").hidden = false;
  element("preview-stage").classList.remove("has-image");
  previewKey = null;
  previewRate.reset();
  element("preview-rate").textContent = "预览 — updates/s";
}

function updateControls(status) {
  const busy = status.busy || pending || exiting;
  const disconnected = status.state === "disconnected";
  const available = ["previewing", "recording"].includes(status.state) && status.transport_live;
  const demo = element("mode").value === "demo";
  element("mode").disabled = busy || !disconnected;
  document.querySelectorAll("[data-camera]").forEach((button) => { button.disabled = busy || !disconnected; });
  element("authorized").disabled = busy || !disconnected;
  element("authorization-label").hidden = demo;
  element("connection-hint").textContent = demo ? "演示图像由本机生成，不访问网络相机，不会被标记为真实机器狗素材。" : "需要已连接 Go1 网络，并配置免密 SSH 与主机指纹。页面不自动配置这些内容。";
  element("connect").textContent = demo ? "启动离线演示" : "连接相机";
  element("connect").disabled = busy || !disconnected || (!demo && !element("authorized").checked);
  element("disconnect").disabled = busy || disconnected;
  element("record").disabled = busy || status.state !== "previewing" || !available;
  element("stop").disabled = busy || status.state !== "recording";
  element("snapshot").disabled = busy || !available;
  element("export").disabled = busy || !status.output || ["recording", "finalizing"].includes(status.state);
  element("session-name").disabled = busy || status.state === "recording";
  element("open-folder").disabled = busy;
  element("quit").disabled = busy;
  element("try-demo").hidden = !disconnected || demo;
  element("try-demo").disabled = busy;
  element("fullscreen").disabled = !status.frame_key;
  element("busy-indicator").hidden = !busy;
}

function render(status) {
  currentState = status;
  if (status.state !== "disconnected") {
    selectCamera(status.camera);
    element("mode").value = status.mode;
  }
  updateControls(status);
  const pill = element("connection-state");
  pill.textContent = stateNames[status.state] || status.state;
  pill.className = "state-pill" + (status.state === "recording" ? " recording" : status.state === "error" ? " error" : status.transport_live ? " connected" : "");
  element("rate").textContent = status.received ? status.receive_rate_hz.toFixed(1) : "—";
  element("preview-rate").textContent = `预览 ${previewRate.value(performance.now()).toFixed(1)} updates/s`;
  element("received").textContent = status.received.toLocaleString();
  element("written").textContent = status.written.toLocaleString();
  element("queue").textContent = `${status.queue} / ${status.dropped}`;
  element("resolution").textContent = status.width ? `${status.width} × ${status.height} px` : "— × —";
  element("source-label").textContent = status.mode === "demo" && status.state !== "disconnected" ? "合成演示 · 非真实机器狗图像" : status.camera === 6 ? "RGB · 相机原生 MJPEG · 无文字烧录" : "原始画面 · 无文字烧录";
  element("save-directory").textContent = status.save_directory;
  element("config-path").textContent = status.config_path;
  const seconds = Math.floor(status.recording_seconds);
  element("timer").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  const faulty = status.state === "error";
  const frozen = !faulty && status.transport_live && status.content_status === "suspected_frozen";
  element("health").className = "health-note" + (faulty ? " error" : frozen ? " warning" : "");
  element("health-title").textContent = faulty ? "采集已中断" : frozen ? "画面疑似冻结" : status.transport_live ? "图像接收中 · 曝光新鲜度未验证" : "等待图像";
  element("health-text").textContent = status.health_message;
  element("empty-title").textContent = faulty ? "连接已中断，旧图已隐藏" : "画面将在连接后显示";
  element("empty-description").textContent = faulty ? errorText(status.error) : "请确认网络与 SSH 配置，然后手动连接相机。";
  if (!status.frame_key) hidePreview();
  const result = JSON.stringify([status.busy, status.action, status.action_error, status.snapshot, status.export, status.output, status.error]);
  if (result !== lastResult) {
    lastResult = result;
    if (status.action_error || status.error) report(errorText(status.action_error || status.error), true);
    else if (status.busy) report(`正在${actions[status.action] || "处理"}，请稍候…`);
    else if (status.action === "snapshot" && status.snapshot) report(`抓拍已保存：${status.snapshot}`);
    else if (status.action === "export" && status.export) report(`无损视频已导出：${status.export}`);
    else if (status.action === "stop" && status.output) report(`录像已停止并保存：${status.output}`);
    else if (status.action) report(`${actions[status.action] || "操作"}已完成。`);
  }
  if (sessionRecovered) {
    report("本机会话已恢复，当前状态已重新读取。没有自动连接相机或重发录制操作。");
    sessionRecovered = false;
  }
}

async function refreshPreview(status) {
  if (exiting || !status.frame_key || status.frame_key === previewKey) return;
  const response = await session.readResponse(`/api/v1/frame.png?sample=${encodeURIComponent(status.frame_key)}`);
  if (response.status === 204) { hidePreview(); return; }
  if (!response.ok) throw new Error("preview_unavailable");
  const blob = await response.blob();
  if (currentState?.frame_key !== status.frame_key || exiting) return;
  // The source can advance between the state and image requests.
  const actualKey = response.headers.get("X-Go1-Frame-Key") || status.frame_key;
  if (actualKey === previewKey) return;
  const nextUrl = URL.createObjectURL(blob);
  const previousUrl = previewUrl;
  previewUrl = nextUrl;
  previewKey = actualKey;
  element("preview").src = nextUrl;
  element("preview").hidden = false;
  element("empty-preview").hidden = true;
  element("preview-stage").classList.add("has-image");
  if (previousUrl) URL.revokeObjectURL(previousUrl);
}

async function perform(action, extra = {}) {
  if (pending || currentState?.busy || exiting) return;
  pending = true;
  if (currentState) updateControls(currentState);
  try {
    await session.action({action, ...extra});
    if (action === "shutdown") {
      exiting = true;
      hidePreview();
      element("empty-title").textContent = "录制台已退出";
      element("empty-description").textContent = "可以关闭此页面，下次双击桌面快捷方式重新启动。";
      element("connection-state").textContent = "已退出";
      report("本地服务正在清理并关闭。不会停止或控制机器人。");
    }
  } catch (error) {
    report(errorText(error.message), true);
  } finally {
    pending = false;
    if (currentState) updateControls(currentState);
  }
}

async function poll() {
  if (exiting) return;
  const started = performance.now();
  let failed = false;
  try {
    const status = await session.readJSON("/api/v1/state");
    if (exiting) return;
    render(status);
    await refreshPreview(status);
  } catch (error) {
    if (exiting) return;
    failed = true;
    hidePreview();
    element("connection-state").textContent = error.status === 403 ? "页面访问校验失败" : "页面服务不可达";
    element("health-title").textContent = "无法确认采集状态";
    element("health-text").textContent = "已隐藏旧画面。网页失联不代表后台已停止录像，请检查本机服务。";
    element("health").className = "health-note error";
    document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    report(`本机页面暂不可用：${errorText(error.code || error.message)}。会自动尝试恢复页面会话，不会自动连接相机。`, true);
  } finally {
    if (!exiting) setTimeout(poll, nextPollDelay(currentState?.state, performance.now() - started, failed));
  }
}

element("preview").addEventListener("load", () => {
  if (!element("preview").hidden) previewRate.observe(previewKey, performance.now());
});
element("preview").addEventListener("error", hidePreview);

document.querySelectorAll("[data-camera]").forEach((button) => button.addEventListener("click", () => selectCamera(Number(button.dataset.camera))));
element("mode").addEventListener("change", () => currentState && updateControls(currentState));
element("authorized").addEventListener("change", () => currentState && updateControls(currentState));
element("connect").addEventListener("click", () => perform("connect", {mode: element("mode").value, camera: selectedCamera, authorized: element("authorized").checked}));
element("try-demo").addEventListener("click", () => { element("mode").value = "demo"; perform("connect", {mode: "demo", camera: selectedCamera}); });
element("disconnect").addEventListener("click", () => perform("disconnect"));
element("record").addEventListener("click", () => perform("start", {name: element("session-name").value}));
element("stop").addEventListener("click", () => perform("stop"));
element("snapshot").addEventListener("click", () => perform("snapshot", {name: element("session-name").value}));
element("export").addEventListener("click", () => perform("export"));
element("open-folder").addEventListener("click", () => perform("open-folder"));
element("fullscreen").addEventListener("click", () => element("preview-stage").requestFullscreen().catch(() => report("当前浏览器未允许全屏。", true)));
element("quit").addEventListener("click", () => { if (window.confirm("结束当前采集并退出本机录制台？正在录制的内容会保存为中断会话。此操作不会停止机器人。")) perform("shutdown"); });

poll();
