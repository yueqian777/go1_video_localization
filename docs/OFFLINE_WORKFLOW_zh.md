# 离线定位、真实建图快照与历史路线预览

本轮在 `codex/evidence-viser-route-preview` 增量开发。没有机器人网络访问、运动命令、原厂服务变更或自动 GitHub 推送。环境与数据继续位于 D 盘，PyTorch / LightGlue / SuperPoint / PyCOLMAP 主链版本未升级。

## 1. 环境与启动

在本项目根目录运行，示例使用已有 D 盘 Python。每次运算使用新输出目录；冻结地图、已有查询和旧证据不覆盖。

```powershell
$py = '.\runtime\python312\python.exe'
$env:PIP_CACHE_DIR = Join-Path (Get-Location).Path '.cache\pip'
$env:TEMP = Join-Path (Get-Location).Path '.cache\tests'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
& $py -m pip install -r requirements-viewer.txt
& $py -m pip check
& $py -m go1loc.cli --help
```

可选界面固定 `viser==1.1.1`。新克隆仍需按 README 准备主视觉环境；本仓库不携带模型、运行时或样例数据。查接口时使用了 [Viser 官方 COLMAP 示例](https://viser.studio/main/examples/demos/colmap_visualizer/) 和 [PyCOLMAP 官方接口](https://colmap.github.io/pycolmap/pycolmap.html)，实际快照适配限定本机验证过的 **4.2.1**。

## 2. 视频与真实快照

```powershell
& $py -m go1loc.cli prepare-video .\data\walk.mp4 --output .\results\walk_frames --calibration .\configs\real_camera.json
& $py -m go1loc.cli build-map --images .\results\walk_frames\images --output .\results\walk_map --map-id walk-v1 --max-keypoints 2048 --snapshot-frames-freq 8 --snapshot-max-count 20
```

没有真实标定文件时不要编造 `real_camera.json`；可省略参数做未标定研究样例，但不能称为 Go1 几何验收。标定格式延续 `calibration.py`，视频与照片调用同一去畸变、有效区域裁剪函数，裁剪后内参会更新。

建图的 sidecar 默认是 `<map目录>.sidecar/<run_id>/`。包含 `events.jsonl` 和 `snapshots/snapshot-*/READY.json`，不写入冻结地图。快照保留完整五个原生模型文件并校验 SHA-256，原子发布后只读。UI 替换整个分量状态，不叠加旧点云。不同 build-attempt 以及快照与最终地图的对齐不作默认假设。

事件序号表示处理顺序；原视频 `pts_time_s` 是另一条时间轴，不能拿 image_id 作运动时间。保留/拒绝帧、阶段开始/完成、整批匹配进展、注册数、快照、失败与取消均有记录；后端没有逐对匹配回调，所以不显示伪百分比。默认事件上限 10,000 条/8 MiB，快照默认最多20份/512 MiB。记录的快照开销是校验与发布耗时，**不包含无法单独计时的原生快照写入耗时**。

两种展示模式：

- `final_map_replay`：最终模型回放；有真实 PTS 才能按视频时间播放。无序照片只浏览相机，不伪造时间。
- `build_snapshot`：真实中间重建记录。无 READY 快照则拒绝切换；不把最终云逐步显现冒充在线建图。此模式不叠加未对齐的最终查询/路线。

## 3. 查询与对象定位

```powershell
& $py -m go1loc.cli localize-photo --photo .\data\target.jpg --map .\results\walk_map --output .\results\target_query --calibration .\configs\real_camera.json --capture-time '2026-10-13T12:00:00+08:00'
& $py -m go1loc.cli locate-photo-object --photo .\data\target.jpg --map .\results\walk_map --query-dir .\results\target_query --roi 120 80 480 420 --output .\results\target_object
```

也可不传 `--query-dir`，由 `locate-photo-object` 实际完成照片定位再取 ROI。`--mask mask.png` 是与原照片同尺寸的二值/灰度掩膜，非零像素为目标；它与 ROI 取交集。ROI 顺序为原照片坐标 `X0 Y0 X1 Y1`，必须在图内。去畸变后逐关键点通过非线性映射回原图，不只变换矩形四角。

手机使用自己的标定；不要套用 Go1 内参。也可显式指定相机模型，例如 `--camera-model PINHOLE --camera-params fx fy cx cy`；这与 `--calibration` 二选一。未提供时会推断内参并记录 `inferred_or_approximate`，不能视为标定精度。采集时刻只有确实知道时才传 `--capture-time`，不会用文件修改时间或当前时间伪造采集时间。

`query_evidence.json` 包含按 hloc 实际索引拼接的对应关系：

- `query_keypoint_index`、`point3D_id`、参考 image_id/文件名/参考关键点索引及真实匹配分数；
- `uv_colmap` 为 hloc 加过0.5的 COLMAP 坐标；`uv` 为处理图零基像素中心，`uv_original` 为原图坐标；
- 对应级 PnP 内点、正深度和重投影误差；均值/中位数/P95及4×4分区覆盖率；
- 原始照片、处理图、标定、特征、匹配、地图与证据 JSON 的 SHA-256 关联。

失败查询同样保留诊断，但不能生成可用对象目标。默认内点率仍为0.25。一个查询点可能支持多个地图点，原始对应数和唯一点数分别记录，不能错用数组索引或把匹配分数称为成功概率。

对象只使用通过 PnP、正深度和误差筛选的支持，按 point3D_id 去重。自动聚类半径为 ROI 支持点最近邻距离中位数的2.5倍，随地图缩放而缩放；可用 `--cluster-radius` 以 **map_units** 显式设定。零簇为 `insufficient_evidence`，一簇为 `supported`，多簇为 `ambiguous`。ROI 背景可能同样是真实几何；请收紧选择或提供前景掩膜，程序不保证已识别物体语义。结果表示**目标可见表面/支持范围**，不是整个物体中心，也不证明物体现在仍在原位。

## 4. Viser 界面

```powershell
& $py -m go1loc.cli view --root .\results\walk_map --sidecar .\results\walk_ui --query-dir .\results\target_query --snapshots .\results\walk_map.sidecar\实际run_id\snapshots --port 8765
```

浏览器访问 `http://127.0.0.1:8765`。几何计算使用原模型，显示最多50,000点。加载、定位与对象求解通过独立进程执行，断开页面不会取消计算；请求、状态、输出存放于 UI sidecar。地图目录不能作为 UI 输出目录。旧 JSON 调试页仅通过 `view --legacy` 使用，也只写 sidecar。

界面操作顺序：

1. 选择 `final_map_replay` 并加载地图；参考帧下拉框显示真实PTS，无时间信息时明确提示不可用。
2. 选择目标照片，点击“打开原图并框选目标”，直接用鼠标左键拖出绿框；松手后框保留，四角方块可缩放、中心蓝块可移动。确认“已选中”及选框预览后计算。四个 ROI 数值是原照片像素，也可手动校正。填入适用于这台相机的标定/内参，需要时加同尺寸掩膜。
3. 点击 `Compute photo ROI -> 3D support`。可填写已有查询目录以复用**通过哈希及参数一致性校验**的证据；结果状态、点数与失败原因留在sidecar。
4. 点击 `Return to 3D map` 看查询相机与目标支持。`Load saved query/object` 可以重新载入已有证据。
5. 起点用 `Localize independent current photo` 或明确的 `MANUAL start`；载入已审核路线图后点击 `Compute route preview`。当前照片没有合法接入时仍拒绝，不会因定位成功自动吸附近邻。
6. `build_snapshot` 配合快照列表索引（从0开始，-1为最新）及加载按钮查看真实中间状态。此模式清除未对齐的最终查询和路线。
7. 截图按钮只保存3D画布，不含侧栏；结果保存按钮写sidecar。无合法路径时只保存拒绝结果，不生成伪路线。

当前查看器需要已有冻结地图；建图由外部CLI运行。它能读取真实中间状态，但不是实时Go1视频建图前端。将来增加在线采集前仍需单独验证数据来源、时间与安全边界。

## 5. 路线预览

```powershell
& $py -m go1loc.cli build-route-graph --map .\results\walk_map --segments .\results\verified_segments.json --output .\results\route_graph.json
& $py -m go1loc.cli plan-route --graph .\results\route_graph.json --start .\results\current_start.json --target .\results\target_object\object_result.json --output .\results\planning_result.json
```

具体 start / segments 格式见 [规划合同](PLANNING_CONTRACT.md)。连续性 sidecar 必须来自真正审核过的视频路段；不能只为了得到路线把 `verified_continuous` 改成 true。默认保留采集方向；时间缺口、跳切、未注册帧和跨分量均断边。当前图无自动回环连接或可逆假设。

`current_photo` 必须是独立当前照片，含实际采集/定位时间、地图身份、质量，以及有来源的路段接入证据。定位成功本身并不证明能直接走到最近图节点。缺少接入返回 `start_not_connected`。`explicit_pose` 只允许明确人工选定图节点，不把任意坐标悄悄吸附过去；标记 manual，不代表已实测当前狗位姿。CLI 使用当前 UTC 检查时效，默认最大30秒。

终点选确实支持该对象的参考相机观察位姿，而非物体中心。没有路径就返回空路径。未知尺度长度是 `map_units`；只有米制地图和已标定的米制 `T_BC` 才计算 `T_MB=T_MC@inverse(T_BC)`。相机历史路线仍不是机身/足步轨迹，也不是实时无障碍保证。旧 `nav_goal` 保持 null，所有规划结果 `execution_permitted=false`。

## 6. 后续 navigation_map 接口（当前不启用）

后续输入应包含 `schema_version, map_id, map_hash, component_id, source, origin, ground_up, resolution_m, scale_status, alignment_evidence, cells`，格子必须区分 free/occupied/unknown。当前没有带真实观测覆盖的占据图，也没有网格规划执行入口；不把稀疏云没有点的地方当 free。将来必须先验证尺度/对齐，再加入足迹与定位余量、障碍膨胀、禁止穿角、逐段碰撞及平滑复查、unknown禁行。

## 7. 重跑离线验证

```powershell
& .\tools\run_tests.ps1
& $py tools\run_coverage.py tests\run_all.py
& $py -m coverage report -m
& $py tools\demo_route_fixture.py --output .\results\new_synthetic_route
& $py tools\validate_public_increment.py --images .\data\public_sample\South-Building\South-Building\images --output .\results\new_public_validation
```

便携 Python 的标准库是 zip；coverage CLI 会移除原生扩展所在路径，故使用本项目的公共 API 包装器 `run_coverage.py`。输出里明确区分公开图像、合成测试与 Go1 现场。测试数据、运行结果、快照、截图均由 `.gitignore` 排除，不随源码上传。
