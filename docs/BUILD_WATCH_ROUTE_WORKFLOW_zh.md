# 首次建图监看、路线审核、当前照片接入与离线回放

本轮基于 `codex/evidence-viser-route-preview` 的 `15ff93c3c3e7fd883fef7ce9245335d883d2d09e` 增量修改。
沿用 hloc、SuperPoint、LightGlue、PyCOLMAP、Viser 和有向 Dijkstra。无需重装现有主视觉环境。
所有路线均是**离线相机路线预览**，`execution_permitted=false`，不会连接 Go1。

## 1. 在第一次建图完成前观看

在项目根目录打开两个 PowerShell 窗口。下面输出路径必须使用新的目录，不能覆盖旧地图。

窗口一：

```powershell
$env:PYTHONUTF8 = '1'
& .\runtime\python312\python.exe -m go1loc.cli build-map `
  --images data/processed/my_clip/images `
  --output results/my_clip_map `
  --map-id my-clip --max-keypoints 2048 `
  --snapshot-frames-freq 5 --snapshot-max-count 20 `
  --sidecar results/my_clip_build_runs
```

`--images` 使用 `prepare-video` 实际输出的图像文件夹；查看其 `frames_manifest.json`，不要猜目录。
建图首先发布 `results/my_clip_build_runs/<run_id>/run.json`，然后才进行特征与重建。
在窗口二选择**这一轮** run，不要误选旧实验：

```powershell
Get-ChildItem results/my_clip_build_runs -Directory
& .\runtime\python312\python.exe -m go1loc.cli watch-build `
  --run results/my_clip_build_runs/这里替换为本轮run_id `
  --sidecar results/my_clip_watch --port 8766
```

打开 `http://127.0.0.1:8766`。不需要最终 `map_manifest.json`，但需要建图进程已发布有效 `run.json`。
若误选不存在的 run，提示 `run_metadata_not_ready`。这是独立监看入口，不是给最终地图伪造清单。

界面操作：

1. `Build stage / events / failure` 显示实际阶段、最新事件序号、READY 数量及失败原因。
2. 勾选 `Automatically follow latest snapshot / final map`，自动装载最新**完整**原生快照。特征阶段还没有模型时保持空白并显示阶段。
3. 取消自动跟随可保持当前画面。选 `History snapshot index`，按 `Pause and load selected history` 回看；控件中的序号是 READY 的一基序号。
4. 建图成功且最终清单通过校验后自动切换冻结地图；也可按 `Load completed frozen map`。
5. 建图失败或取消后不删除已发布快照，仍可回看。显示任务本身失败也保留上一份完整画面。
6. `Save 3D canvas screenshot` 保存三维画布，**不含侧栏**。sidecar 的 `watch-*.jsonl` 另存自动/手动切换、版本和点/相机计数。

只解析原子 READY 发布的五文件原生模型，校验 run、build-attempt、分量、帧身份、计数和文件哈希；不读活动中的半写入模型。
每次切换完整替换场景，不把不同 build-attempt 或最终地图叠在一起。这个窗口没有查询、对象和路线叠加入口。
模型被原生后端保留为快照才可能显示；它不是连续逐帧 SLAM。较慢机器可能跳过过时中间显示请求、跟上最新完整快照，历史文件仍保留。

冻结之后进入原有完整工作界面：

```powershell
& .\runtime\python312\python.exe -m go1loc.cli view `
  --root results/my_clip_map --sidecar results/my_clip_viewer --port 8767
```

所有 sidecar 必须与输入图像、地图、构建 run 隔离。旧格式快照仍能在原 `view --snapshots` 冻结地图入口查看；缺少新版 run 元数据的旧实验不能直接冒充首次建图监看。

## 2. 查询切换和任务依赖

加载新查询时，先让原对象结果、支持点、观察终点和路线失效，再读取新文件。
新查询成功、定位失败、文件不存在或证据校验失败，都不能继续使用旧目标的路线。
可以保留明确选定的当前起点；这不代表目标和路线仍有效。

只有新对象求解/加载完成，规划按钮才接收该目标。完整 `object_result.json` 会先核验内部查询，随后提交对象状态。
旧任务在后台晚完成不会恢复旧目标/路线，输出文件仍保留供检查。浏览器断开不取消计算。

## 3. 不编辑内部 JSON 的历史路线审核

在 `view` 的 `Historical segment review` 区域操作：

1. 设置最大抽样时间缺口，按 `Generate PENDING candidates from frozen map inputs`。
   worker 核验冻结地图和真实原生参考位姿，读取地图绑定的 `frames_manifest.json`。
   原清单的**字节哈希**及其 JSON 内容都必须与冻结记录相符。
2. 阅读 `Review source / timeline / diagnostics`：来源类型、设备类型、来源路径/哈希、缺帧/时间缺口/跳切等。
   无序照片没有连续视频时间线，得到 0 候选是正确结果。`synthetic_test` 明确保留合成标签，未知设备保持 `unknown`。
3. 选择 `PENDING segment`，查看紫色候选相机轨迹及 `Selected segment frames / real PTS`。
   这只是待审核历史顺序，不是可通行路线。逐项选择 `Inspect timeline row` 并按 `Show review frame / PTS / registration`，查看注册、质量、断点信息；未注册/其他分量的图像可能需回原视频查看。
4. 必须检查原视频，特别是未自动标出的跳切。填写审核人和实际依据。
5. 整段连续且可作为历史相机路径时按 `Confirm entire reviewed segment`；不接受则 `Reject entire segment`。
6. 仅认可部分时先用首/尾帧选范围，按 `Split selected range`，再分别确认或拒绝子段。
   拆分不会自动确认子段，也不会跨拆分边界补边。部分范围不能直接整段确认。
7. 按 `Save reviewed segments and directed graph`。worker 从原始输入重新生成候选、校验输入身份并重放决定，而不是信任界面拼接的边。

输出位于这次 worker 的独立 `output/reviewed/`：

- `route_review.json`：原候选、完整时间线、诊断、审核决定和依据；
- `route_segments.json`：兼容现有 `build_route_graph` 的明确确认路段；
- `route_graph.json`：当前分量的有向图，界面自动填入该路径。

没有确认的候选不会生成边。未注册、质量拒绝、跨分量、过大时间缺口、已标注跳切都是断边条件。
不使用共视边、匹配边、空间近邻替代路线；人类确认也不能修复构图器拒绝的跨缺口边。
确认的是过去相机行进的依据，不保证现实当前无障碍，更不证明机身/足步可执行。

## 4. 独立当前照片：定位 → 检查附件 → 明确采用

先加载目标对象结果，再选择与目标照片不同且未参与建图的当前照片：

1. 填 `INDEPENDENT current photo path` 和真实 `Actual capture time`，按 `Localize independent current photo`。
   拍摄时间必须带时区；未知就不能以“现在”补齐，也不能使用文件修改时间或定位完成时间。
2. 已计算过的当前查询可在 `Saved INDEPENDENT current query directory` 填目录并按 `Load verified current query`。
   新版导出将定位结果的完整位姿、质量、时间和证据哈希绑定。旧结果没有这项绑定时需要重新定位到**新目录**，不能手工补一个哈希。
3. 选择合法 `route_graph.json`，按 `Inspect graph and current connection`。
   界面列出图节点候选、缺失字段和拒绝原因；`Show selected candidate node only` 仅显示节点，不创建连接。
4. 在 `Import current_start or attachment JSON path` 选择已有证据文件，按 `Inspect imported connection`。
   可读完整 current_start、裸 attachment 或 `{attachment: ...}` 包装。只预览，不立即采用。
5. 核对橙色接入路段、连接到哪个节点，以及 evidence_id、source_kind、source_sha256。
   附件必须与当前照片、完整位姿、地图哈希、分量、query_id、照片哈希、采集/定位时间一致，路径首尾与当前位姿/确切节点匹配，PTS 严格递增且无超限缺口。
6. 人工检查来源内容，在 `Connection source file` 填对应原始证据文件；采用时校验其 SHA-256。
   按 `Adopt inspected evidence for OFFLINE preview` 才写入当前起点。检查后文件、起点、图或时间模式变了，必须重新检查。
7. 按 `Compute route preview from start + target`。此时仍重新检查时间、目标独立性、接入合法性和图连通性。

失败的重新导入先撤销旧接入和路线，不能悄悄退回旧附件。文件校验只证明内容一致，不认证轨迹声明；人工审核必须有实际依据。

**本轮不自动从单张照片生成接入轨迹。** 如果还没有能证明“当前照片位置到历史节点”的合法轨迹/接入证据，就停在定位与缺项解释，不会靠改 JSON、填 XYZ 或最近节点连线绕过。
导入/审核流程可用，不等于现有无序照片数据已经具备这类证据。精确格式、函数接口与拒绝码见 [契约](REVIEW_TIME_CONTRACT.md)。

若只做第一步历史图验证，可选择参考帧并按 `Use selected reference as MANUAL start`；这是明确的人工历史节点，不是现实当前机器人位置。选择操作仍按实际时间记录，超过默认30秒可重新明确选择。

## 5. 现实当前与离线回放

| 字段 | 含义 |
| --- | --- |
| captured_at / capture_time | 真实拍摄时间，永不为回放改写 |
| localized_at / localization_time | 实际定位计算完成时间 |
| run_at / planned_at | 本次真实规划运行时间 |
| replay_reference_time | 独立设置的历史回放参照时间 |

默认 `live_current` 按实际 UTC 检查30秒新鲜度，回放参照输入应留空。
选择 `offline_replay` 后填历史参照时间：照片不得晚于参照时间，参照时间不得晚于实际运行时间，参照与照片的间隔仍应在30秒预算内。
定位计算可以发生在多年后的今天；这不改变照片拍摄时间。模式或参照变化会清掉旧路线显示。

CLI 示例（使用界面保存的起点与图）：

```powershell
& .\runtime\python312\python.exe -m go1loc.cli plan-route `
  --graph results/my_review/route_graph.json `
  --start results/my_current/current_start.json `
  --target results/my_target/object_result.json `
  --time-mode offline_replay --replay-reference-time '2026-09-10T10:00:05+08:00' `
  --output results/my_replay/planning_result.json
```

示例日期应替换为实际采集记录，不是可直接套用的时间。命令自行读取实际运行时间，不提供伪造 `now` 的 CLI 开关。
回放与当前模式都 `execution_permitted=false`。终点是支持目标的参考观察相机位姿，不是物体中心；没有尺度时长度为 `map_units`。

## 6. 真实连续视频验证的输入阻塞

2026-10-13 用户明确答复“没有”可用真实视频/独立照片/标定。本轮没有下载代替数据、连接机器狗或将无序照片重编码成视频。

后续需要：

- 一段获准使用、无剪辑/跳切的真实连续运动视频；说明 Go1/手持/其他设备来源。
- 未参与建图的目标照片、独立当前照片，以及可信的拍摄时间和时区。
- 与分辨率/裁剪一致的相机内参和畸变参数；若暂缺，必须标明近似状态，不能声称有标定精度。
- 可供人工复核的连续历史路段；B 步另需与当前照片绑定的合法接入来源。

拿到数据后用全新目录依次执行：prepare-video → build-map/watch-build → 独立目标求解 → 路段审核 → 人工历史节点成功路线和禁用边 no_path → 独立当前照片定位/导入合法接入 → 成功路线及删附件/错配拒绝。
手持视频可先验证软件链，但不能据此宣称 Go1 相机/机身导航验收。原39图地图、独立查询和全部旧报告继续保留。
