"use strict";

// Reuse the authenticated capture session; no camera/action replay on recovery.
(() => {
  let workspace = null;
  let pendingAction = null;
  let catalogSignature = null;
  const find = (id) => document.getElementById(id);
  const message = (text) => { find("workspace-message").textContent = text; };
  function releaseAction() {
    if (!pendingAction) return;
    pendingAction = null;
    pending = false; // Shared with capture controls, so a handoff cannot be overwritten.
    if (currentState) updateControls(currentState);
  }

  function showTab(map) {
    find("capture-main").hidden = map;
    find("map-main").hidden = !map;
    find("tab-capture").setAttribute("aria-selected", String(!map));
    find("tab-map").setAttribute("aria-selected", String(map));
    if (map && workspace && !find("workspace-map").getAttribute("src")) {
      const url = new URL(workspace.viewer_url);
      if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port || url.pathname !== "/") {
        throw new Error("invalid_local_viewer");
      }
      find("workspace-map").src = url.href;
    }
  }

  function renderWorkspace(data) {
    workspace = data;
    if (!data.enabled) return;
    find("workspace-bar").hidden = false;
    document.title = "Go1 采集 · 建图 · 定位工作台";
    document.querySelector(".brand-light").textContent = "Workbench";
    document.querySelector(".brand small").textContent = "采集 · 建图 · 视觉定位 · 方向引导";
    find("quit").textContent = "退出整个工作台";
    const recordings = data.recordings.filter((row) => row.source_kind !== "synthetic_test");
    const signature = JSON.stringify(recordings);
    const select = find("workspace-recordings");
    if (signature !== catalogSignature) {
      const previous = select.value;
      select.replaceChildren(...recordings.map((row) => new Option(
        `${row.capture_id} · 相机${row.camera_id} · ${row.frames}帧`, row.capture_id)));
      if (recordings.some((row) => row.capture_id === previous)) select.value = previous;
      if (!recordings.length) select.add(new Option("暂无完整真实录像（不使用合成演示）", ""));
      catalogSignature = signature;
    }
    const busy = Boolean(pending || pendingAction || currentState?.busy || exiting);
    find("workspace-build").disabled = busy || !select.value;
    find("workspace-live").disabled = busy || !data.map_ready || !currentState?.live || currentState.mode !== "robot";
    find("tab-map").disabled = exiting;
    find("tab-capture").disabled = exiting;
    const position = data.current_pose_available ? "当前源帧已定位" : "尚无有效当前位置";
    find("workspace-status").textContent = `${data.map_ready ? "地图已就绪" : "等待最终地图"} · ${position} · 不控制运动`;
    find("workspace-status").title = `${data.navigation_status}\n${data.build_status}`;
    if (pendingAction && currentState?.action === pendingAction && !currentState.busy) {
      const action = pendingAction;
      releaseAction();
      if (currentState.action_error) message(errorText(currentState.action_error));
      else {
        message(action === "mapping-recording"
          ? "录像已送入建图：在下方展开“新建地图”，检查首帧后点击“开始建图”。没有自动启动重建。"
          : "已选用同一连接的源帧：在下方核对相机安装、框选目标，再点击“开始方向引导”。曝光时间未知，位置由实际计算更新。");
        showTab(true);
      }
    }
  }

  async function action(name, extra = {}) {
    if (pending || pendingAction || currentState?.busy || exiting) return;
    pendingAction = name;
    pending = true;
    if (currentState) updateControls(currentState);
    message("正在交接，请稍候…");
    try { await session.action({action: name, ...extra}); }
    catch (error) { releaseAction(); message(errorText(error.message)); }
  }

  find("tab-capture").addEventListener("click", () => showTab(false));
  find("tab-map").addEventListener("click", () => showTab(true));
  find("workspace-build").addEventListener("click", () => action("mapping-recording", {capture_id: find("workspace-recordings").value}));
  find("workspace-live").addEventListener("click", () => action("navigation-live"));
  async function refresh() {
    if (exiting) return;
    try { renderWorkspace(await session.readJSON("/api/v1/workspace")); }
    catch {
      releaseAction();
      find("workspace-status").textContent = "工作台状态不可用，不显示旧位置；不会自动重发操作。";
      find("workspace-build").disabled = find("workspace-live").disabled = true;
    }
    if (!exiting) setTimeout(refresh, 1000);
  }
  refresh();
})();
