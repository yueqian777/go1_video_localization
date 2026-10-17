# go1_video_localization

基于 Go1 运动视频建图的照片目标定位系统。项目运行在外部计算机，默认只处理本地文件，不连接机器狗，不修改现有 `go1_hri` 控制栈。

## 当前状态与阅读顺序（2026-10-17）

下文按日期保留的“缺少真实视频”等内容是早期实验状态，不是当前输入清单。

| 层次 | 当前状态 | 不代表什么 |
| --- | --- | --- |
| 已实现 | 统一采集/建图工作台、原生相机源、常驻hloc、自动历史拓扑、路口融合、证据绑定断点补充与接入缓冲 | 不等于自由空间导航 |
| 真实视频与找物 | camera6实采录像已建图，白球独立照片有几何支持；冻结地图58参考位姿、3549点 | 不等于球心/米制位置真值 |
| 真实静止观测 | 充电后独立新运行180次定位，157条方向；发现3秒时效撤回及一次前瞻点跳变 | 不等于连续稳定或运动成功率 |
| 旧记录回放 | 旧45条记录的接入逻辑回归为43条方向，旧数据保留 | 不是新的实机成功 |
| 本轮边界修复 | 反向行进与观察方向分开；先判断目标观察区；预热后取新帧；逐阶段时间日志 | 合成边界回归不是现场移动验收 |
| 新移动验证 | **未通过第二位置**：新运行182个结果中120条方向、4次定位后未接入；第二位置18张新帧复测均未通过位姿质量检查 | 不是120次移动成功；现场未观察方向正确性，转角/到达尚未完成 |
| 第二位置离线修复 | 查询参考图一致性回退已实现；保存的18张第二位置照片重新提取匹配后均通过定位；原182条记录重算也得到182条合格位姿 | 第二位置仍18/18未接入路线；不是新实机验证，不证明位置真值或连续导航成功 |
| 路线/耗时续查 | 原生图与路线坐标一致；第二位置距历史路线0.641～0.826 map_units，超过0.289接入范围。200帧加速重算位姿不变，第二位置最慢2.83秒、移动记录最慢3.54秒 | 没有强行吸附或补边；仍3条超过3秒，实时门限未放宽；不是新的实机通过 |
| Go1运动执行 | **未接通**，`LocalMotionAdapter`没有传输实现，始终影子引导 | 不可仅改`execution_permitted=true`启动机器人 |

建议依次阅读：

1. [第二位置路线与耗时续查](docs/SECOND_POSITION_ROUTE_LATENCY_20261017_zh.md)——最新几何诊断、加速实测和未解决边界。
2. [第二位置定位离线修复](docs/SECOND_POSITION_LOCALIZATION_FIX_20261017_zh.md)——上一轮参考图一致性修复。
3. [找物边界、操作与现场验证](docs/SHADOW_BOUNDARIES_20261017_zh.md)——上述离线修复之前的新实采记录，历史结论保留。
4. [统一工作台](docs/UNIFIED_WORKBENCH_20261017_zh.md)——日常入口。
5. [实时接入修复](docs/LIVE_ROUTE_ASSOCIATION_FIX_20261017_zh.md)——旧45条离线回归的来历。
6. 下方历史报告——按报告日期理解“当时尚未完成”，不要覆盖历史证据。

## 当前实现

- **统一工作台**：相机预览/录制与三维建图/定位共用一个网页入口。完整录像可直接送入建图，实时定位直接订阅同一采集服务的源帧，不重复开启 SSH。默认不连接、不录制、不控制运动。见[统一工作台说明](docs/UNIFIED_WORKBENCH_20261017_zh.md)。
- `go1loc capture-ui`：独立单路相机预览和手动 PNG 录像；仅在勾选确认并点击连接后使用 SSH 只读代理。默认不连接、不部署、不发运动命令。`--synthetic` 可离线演练。
- `go1loc export-capture` / `prepare-capture`：核验源 PNG，导出经过像素回读检查的 VFR FFV1，或直接生成已有 `build-map` 使用的有序帧清单。见[相机采集手册](docs/GO1_NETWORK_CAPTURE_zh.md)与[离线验证和实机待验收清单](docs/GO1_NETWORK_CAPTURE_VALIDATION_20261015_zh.md)。
- `go1loc prepare-video`：对真实视频做 SHA-256、ffprobe、基于真实 PTS 的有界解码、模糊/过曝/疑似冗余筛选；有认证标定时保存非线性去畸变映射和有效区域。
- `go1loc build-map`：依赖安装后调用固定版本 hloc + SuperPoint + LightGlue + pycolmap 建立真实稀疏地图。
- `go1loc build-map --max-keypoints N`：为显存受限设备设置 SuperPoint 特征点上限；默认仍为 4096，公开样例实测使用 2048。
- `go1loc localize-photo`：对独立查询照片做检索、局部匹配、2D-3D PnP 和严格质量门禁；不会在失败时回退最近参考位姿。
- `go1loc locate-object`：接收人工选择的 ROI 和实际 2D-3D 支撑，去重、空间聚类并对多簇输出歧义。
- `go1loc locate-photo-object`：照片＋地图＋原图 ROI/掩膜，实际计算并验证 PnP 支持，输出可见表面范围。
- `go1loc view`：可选 Viser 三维界面，仅监听 `127.0.0.1`；独立工作进程处理定位，结果写 sidecar。
- `go1loc build-map --snapshot-frames-freq 8`：记录真实 PyCOLMAP 中间快照；与最终地图回放严格区分。
- `go1loc watch-build --run ... --sidecar ...`：首次建图尚无冻结地图时观看阶段与 READY 快照，支持自动跟随、暂停/历史回看和完成切换。
- `view` 新增历史路段候选生成、人工拆分/确认/拒绝、当前照片接入证据检查与明确采用流程，不必编辑内部 JSON。
- `go1loc build-route-graph` / `plan-route`：只沿有连续时间证据的历史路线作有向 Dijkstra 预览，不连接机器狗。
- `plan-route --time-mode offline_replay --replay-reference-time ...`：明确分离真实采集/计算时间和回放参照时间；仍不可执行运动。
- `go1loc doctor`：只读检查本机工具、Python 包、GPU 和固定上游版本。
- `go1loc evaluate`：保留所有查询（包括失败和拒绝）并汇总状态。

## 开发环境

### GitHub 发布范围

本仓库发布当前研究原型的源码、配置、测试和文档，不包含 Python 运行环境、模型权重、第三方仓库副本、数据集、现场记录或本机生成结果。下文 `results/` 路径描述的是本机实测产物，不是随仓库分发的文件。

新克隆的仓库不会自带 `runtime/python312` 或 `.upstream`；现有 PowerShell 安装脚本要求先准备这些目录。可在项目所在盘创建独立 Python 3.12 环境，将 hloc 和 LightGlue 克隆到 `.upstream/`，按 `environment-lock.json` 中的提交号检出，再安装 `requirements-core.txt`、`requirements-pipeline.txt`、两个上游包和本项目。GPU 版 PyTorch 需要按硬件单独选择；不要把本机的 CUDA/驱动记录当成其他设备的安装要求。

第三方代码和权重分别遵循各自许可，本仓库不替它们重新授权：

- [Hierarchical-Localization](https://github.com/cvg/Hierarchical-Localization)
- [LightGlue](https://github.com/cvg/LightGlue)
- [South-Building 公开样本来源](https://cvg-data.inf.ethz.ch/local-feature-evaluation-schoenberger2017/)

### 已验证的本机环境

所有新环境、wheel 缓存、模型权重和公开数据放在本项目 D 盘目录下。当前可复用的视觉运行时是 `runtime/python312`，不是系统 Python：

```powershell
& '.\runtime\python312\python.exe' -m pip check
& '.\runtime\python312\Scripts\go1loc.exe' doctor --project-root . --output results/doctor.json
& '.\runtime\python312\python.exe' tests/run_all.py
& powershell -ExecutionPolicy Bypass -File tools/install_pipeline.ps1
```

已验证的组合是 Python 3.12.10、PyTorch 2.9.0+cu130、torchvision 0.24.0+cu130、PyCOLMAP 4.2.1；`nvidia-smi` 识别为 RTX 5070 Ti Laptop GPU。CUDA 驱动和 Python 视觉包是两层不同东西，NetVLAD/LightGlue 权重仍需单独缓存。运行时会把 `TORCH_HOME` 默认指向项目下的 `.models/torch`，不应把新模型写入 C 盘用户缓存。安装脚本把 hloc/LightGlue 从 `.upstream/` 固定提交以避免 Windows pip 重复解析 Git 依赖。

首次验证可使用不超过 `exhaustive_limit` 的小地图，跳过 529 MB NetVLAD；超过该规模才启用 NetVLAD 检索。

公开 South-Building 实测已在 `results/public_south_map40_lg_k2048` 生成冻结地图：40/40 图像注册、10,394 个 3D 点、平均重投影误差 1.316 px。地图内查询 `P1180170.JPG` 通过定位质量门禁；未参与建图的 `P1180181.JPG` 和 `P1180182.JPG` 被严格拒绝，分别记录 0.2096 和 0.1381 的内点率。该结果只证明公开建筑样例上的软件链路，不等同于 Go1 相机或点云质量验收。

2026-10-13 增量版本的运行方法、数据关联、安全边界和实测证据见 [离线工作流](docs/OFFLINE_WORKFLOW_zh.md)、[验收记录](docs/INCREMENT_VALIDATION_20261013.md) 和 [规划合同](docs/PLANNING_CONTRACT.md)。原始地图和上述历史结果保留不变。

基于 `15ff93c` 的后续修订见 [首次建图与路线输入操作说明](docs/BUILD_WATCH_ROUTE_WORKFLOW_zh.md)、[本轮验证记录](docs/INCREMENT2_VALIDATION_20261013.md) 和 [审核/回放接口契约](docs/REVIEW_TIME_CONTRACT.md)。该修订修复查询切换后的旧对象/路线残留，已实测首次建图时自动加载7份真实快照；这里的建图来源仍是公开无序照片，不是连续运动视频。用户确认当前没有真实视频/独立照片/标定输入，所以真实连续视频 A/B 路线验收明确阻塞，不能以软件回归或合成夹具替代。

## 研究边界

接手审查的阅读顺序、版本关系、历史报告差异和数据阻塞见 [Codex交接说明（2026-10-14）](docs/CODEX_HANDOFF_20261014_zh.md)。

后续单点修订见 [照片身份修复与真实视频采集清单](docs/IDENTITY_FIX_VALIDATION_20261014_zh.md)：不再按相同basename/处理图名拒绝不同原始照片，同时拒绝原图身份缺失或冲突；真实连续视频A/B验收仍缺配套输入。

2026-10-14 用户提供真实 Go1 单路**屏幕录制**后，新增 [录屏导入操作说明](docs/SCREEN_IMPORT_WORKFLOW_zh.md) 与 [30秒试片验证报告](docs/SCREEN_IMPORT_VALIDATION_20261014_zh.md)。支持原尺寸选区、真实显示PTS、PNG派生/哈希、场景质量报告及实际 SuperPoint→LightGlue 特征掩膜。试片在用户接受清晰度限制后完成探索性稀疏重建（最终60/60帧、2675点）；不是原始码流或真实路线A/B验收，仍缺独立查询及人工连续性审核。

单目地图默认是任意尺度。没有外部测量误差与版本记录时，输出单位是 `map_units`，`nav_goal` 恒为 `null`。详细上游核对、数据合同和未验证项见 `docs/`。

目标选择仍是人工 ROI/掩膜，不是自动语义物体识别；输出不能称为完整物体中心。当前有历史相机路线预览，但没有自由空间可通行地图、实时避障或机器狗导航闭环。真实 Go1 视频、内参/相机机身外参和米制尺度尚未验收。公开图片定位通过软件门禁不等于已测得绝对定位精度。
