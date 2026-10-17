# Go1 网络相机模块离线验证与实机待验收清单

2026-10-15，本机完成只读代理源码、SSH 二进制接收、Viser 手动录像、PNG/FFV1 保存和现有建图接入。
全项目 301 项测试通过，其中新增采集测试 32 项；采集模块语句覆盖率 86.93%。
真实浏览器和公开样本回放均已验证，**尚未连接 Go1、部署代理或采集真实相机图像**。

## 基准与修改范围

开发前检查实际分支 `codex/evidence-viser-route-preview`，HEAD 为
`00fdaff151b15d8d25334235622379d2dcf46319`，仓库工作区干净；原 269 项基线测试通过。
本次在此基础上增量修改，没有回退分支、删除旧录屏结果或改写 hloc/LightGlue/PyCOLMAP。
未提交或推送 Git，没有连接设备或上传现场数据。

新模块为 `capture_wire/agent/transport/session/control/ui/import/synthetic.py`。
`Go1ReadonlyShmAdapter` 现在要求显式授权并返回尚未启动的 SSH source；UI 点击连接才启动。
现有 CLI 增加 `capture-ui`、`prepare-capture`、`export-capture`，地图/快照保留来源与时间类型。
规划系统只扩展来源枚举，连续性、人为核验、时间与执行限制不放宽。

## 软件测试证据

```powershell
& .\runtime\python312\python.exe tests/run_all.py
& .\runtime\python312\python.exe tools/run_capture_tests.py
```

最新结果：全套 301/301；采集专项 32/32，1010 条语句覆盖 878 条（86.93%）。
覆盖率是本机语句覆盖，不是板端兼容率、相机帧率、运动或建图成功率。
Ruff `E4,E7,E9,F,I` 基础检查和 Python compileall 通过。
代理两个文件通过 Python 3.6 语法解析；没有把这一结果称为 Nano 上实际执行通过。

测试覆盖：

- 分包、连续包、半包、EOF、长度/格式错误与 CRC 校验。
- 真实本机子进程二进制 stdout、100000 字节 stderr 持续排空与 8192 字节内存上限、超时关闭。
- 未授权不建立连接，SSH 严格主机验证/无 PTY/非交互认证，host 注入拒绝、远程路径引用。
- 远端共享段/认证/主机验证失败只显示固定错误代码，不回显任意 stderr 内容。
- 用独立内存缓冲区模拟 SHM，验证只读附加参数、双读不一致拒绝、所有者/大小/段身份改变拒绝、仅 detach。
- 生产者替换共享段时停止旧来源；采样期间每秒复核身份，不继续把旧附加段当正常流。
- 手动开始/停止、重复启动拒绝、相机/格式/会话混入拒绝、重连不自动录像。
- 有界队列溢出、磁盘写失败保留已完成 PNG、超时抓拍不使用旧图。
- BGR/RGB 颜色及源 PNG 独立于显示副本；无损视频回读像素与非等间隔时间。
- 导入哈希/路径防护、来源与读取顺序、读取缺口传播、无自动路线边。

未创建或写入任何真实 SysV 段，包括原厂 9001–9005。
本机没有可用 WSL Linux，因此真实 Linux SysV 系统调用、板上已有 Python 和 SSH 跳板仍须现场验证。

## 真实浏览器操作

实际启动 Viser 1.1.1 的 loopback 页面，未加载地图或视觉模型。
从 `disconnected` 手动连接显式合成源，点击开始、抓拍、停止、导出，随后断开并改选 camera2 再次连接/录制。

| 项目 | 实测 |
|---|---|
| 第一段 | camera1，236 张 464×400 PNG |
| 读取时间跨度 | 23.844 秒 |
| 已保存读取率 | 约 9.856 reads/s，不是曝光 FPS |
| 电脑队列丢弃/写入失败 | 0 / 0 |
| 抓拍 | 单独 source.png/source.json，非浏览器截图 |
| FFV1 | 236 帧，bgr0，抽查 0/118/235 帧逐像素一致 |
| FFV1 时间误差 | 最大约 5.36e-12 秒；格式仍按 1 ms 精度验收 |
| 第二段 | camera2，48 张 PNG；新 stream session_id，与第一段分开 |
| 断开后 | 隐藏预览，不自动续录 |

所有会话 `source_kind=synthetic_test`。浏览器无 console error；有一条上游 THREE.Clock 弃用 warning。
自动化最初尝试用原生 select 操作 Viser 的 combobox，被正确拒绝；之后改为实际下拉选项完成换相机。
Windows 内嵌 Python 的脚本目录隔离问题已修正：合成子进程使用 `python -m go1loc.capture_synthetic` 启动。
演练结束关闭了本次专用浏览器及合成采集服务，没有清理其他服务或用户文件。

对第一段色条使用默认 `prepare-capture`，得到 48 个候选、0 个通过画质筛选。
结果完整保留；色条低纹理不能用于有效建图，没有为得到成功降低默认门槛。

## 新格式进入原有建图流程

另使用本机已有 South-Building 的 8 张公开照片建立明确的**合成回放夹具**：
仅夹具将照片缩到最长边 1000，然后走二进制帧编码/解码 → Recorder PNG → prepare-capture → 原有 SuperPoint/LightGlue/PyCOLMAP。
这不是现场录像，也不是连续相机轨迹；真实 SHM 代理不执行此缩放。
夹具显式 `quality_filter=False` 是为了验证连接流程，不能视为画质验收通过。

```powershell
& .\runtime\python312\python.exe tools/validate_capture_mapping.py --images data/public_sample/south_subset_40 --output results/my_new_capture_replay
```

该命令必须使用新的输出目录，并要求原有视觉依赖/公开照片已在本机准备好；不下载资料或访问机器人。
实测保存 8 帧、导入 8 帧、注册 8/8 图像、2164 个稀疏 3D 点。
冻结地图哈希：`cf51cae3d7266086422e667adf0979de2425b34ba2600607fe0e6e9c20d77117`。
最终又实际核验了模型哈希，并从这份地图运行 `build-route-graph`：**0 条边，execution_permitted=false**。
所有夹具相邻帧标记中断，来源仍为 synthetic_test，地图/快照时间仍标读取时间而不是曝光时间。

因此已验证“新采集格式能进入既有真实建图代码”；尚未证明 Go1 实拍清晰度、标定正确性、定位精度或导航连续性。

## 本机证据位置

`results/capture_development_20261015/` 默认不进入 Git：

- `tests_full.log`、`capture_tests.log`、`coverage.json`：测试和语句覆盖。
- `gui_sessions/`：两段合成录像、抓拍和 FFV1 校验记录。
- `output/playwright/recording.png`、`disconnected.png`：实际 UI 截图，仅作界面证据。
- `.playwright-cli/`：浏览器快照及 console 记录。
- `gui_prepared/`、`gui_import.log`：色条默认质量检查，保留 0 帧的真实结果。
- `public_replay/`、`public_replay.log`、`route_graph.log`：合成公开照片采集、导入、实际重建、快照与无自动路线边。
- `graphify-out/`：本机 AST 代码关系检查，未发送代码给外部语义模型。

源码包不包含这些图片、模型、Python 环境、凭据或原始现场影像。

## 实机尚未验收

- 当前五视窗真正的图像入口、现有 UDP 接收者和原厂服务资源基线。
- 当前这只 Go1 的 SSH 路由/主机指纹/身份认证、板上 Python 和代理退出行为。
- 五路当前 SHM UID/尺寸/布局、朝向、是否重投影，双读一致性的实际统计。
- 源 PNG 与旧查看器同场景对照，确认十字线来源及真实画质。
- 单路连续 1–2 分钟采集的网络吞吐、代理与 Pi 负载、原厂服务是否受影响。
- 实机开始/停止、抓拍、断网/重连和相机切换，不混入旧流或自动恢复录像。
- 匹配的相机标定，以及新 Go1 录像实际运行既有建图流程。

这些必须由用户明确触发首次连接、部署和现场录制后逐项完成。
操作顺序见 [Go1 网络相机预览与手动录像](GO1_NETWORK_CAPTURE_zh.md)。
