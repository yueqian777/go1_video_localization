# Go1自由空间导航升级与闭环接口（2026-10-29）

本轮在 `codex/evidence-viser-route-preview` 分支增加了一个保守的离线导航层。它把视频地图中的几何证据转换为 `occupied / free / unknown / uncertain` 四类2.5D栅格，在栅格上使用带朝向的 A*，再通过一个一次只发出一个运动原语的闭环状态机与现有 `go1_remote_control` 的 `RobotLink` 对接。

当前交付的性质是：**离线可测试、可生成新路线、可输出可停止的控制原语；没有执行真实机器狗运动。** 任何模拟结果都不等于实机自主导航验收。

## 结构变化

旧结构是：

```text
相机位姿 → 历史路线图 → 历史路段 Dijkstra → 相机路线预览
```

新结构是：

```text
地图几何 sidecar → 保守2.5D栅格 → 带朝向 A* → 局部运动原语
                                      ↓
                           新鲜视觉位姿 + RobotLink反馈
                                      ↓
                                  停止/重定位/重规划
```

历史轨迹仍保留，只能为狭窄的经过证据走廊增加支持，不能把周围无点区域自动变成自由空间。没有几何证据的格子保持 `unknown`；地面与障碍证据冲突时为 `uncertain`。普通规划拒绝未知和不确定格子。

## 新增文件

- `src/go1loc/free_space.py`：2.5D栅格、证据分类、侧车序列化和加载。
- `src/go1loc/grid_planner.py`：独立于历史路线图的带朝向 A*，支持前进、原地左右转和180°调头。
- `src/go1loc/navigation_closed_loop.py`：尺度/相机安装/上方向门禁，以及“观察—一个原语—停止反馈—重新观察”的闭环骨架。
- `src/go1loc/remote_control_adapter.py`：只调用现有 `RobotLink.submit()` 的适配器，不开第二个 SDK 端口。
- `src/go1loc/offline_scenarios.py`：A–K离线场景和逐项结果。
- `tools/build_free_space_sidecar.py`：从已有 `inspect_map` 的 `scene.json` 生成自由空间 sidecar。
- `tools/run_navigation_scenarios.py`：生成 JSON 报告和 JSONL 事件日志。

Viser 的“研究模式 → ①地图”中增加了自由空间 sidecar 加载入口。显示颜色为：绿色 free、红色 occupied、灰色 unknown、黄色 uncertain。载入前会检查 `map_id`、`map_hash`、`component_id` 和 `units`，且要求 `execution_permitted=false`。

## 离线用法

在导航项目根目录运行：

```powershell
$py = '.\runtime\python312\python.exe'
$env:PYTHONPATH = 'src'

# 从已有的 inspect_map 输出生成预览 sidecar。
# 不提供 --ground-height 时，观测格子不会被擅自判为 free。
& $py tools/build_free_space_sidecar.py `
  path\to\scene.json `
  results\free_space\free_space.json `
  --resolution 0.10

# 跑 A–K 并保存逐项日志。
& $py tools/run_navigation_scenarios.py

# 运行完整离线测试。
& $py tests/run_all.py
```

若已通过独立测量确定地图地面高度，可以显式传入 `--ground-height`。这只改变几何分类，仍不会把地图变成可执行运动任务；真实执行还需要米制尺度、相机到机身外参、上方向、地图到机身偏航和新鲜视觉定位全部通过门禁。

## A–K测试含义

| 场景 | 验证内容 | 允许的结论 |
|---|---|---|
| A | 历史 A→B→C 反向返回 | 能生成调头和反向栅格路径 |
| B | 历史右侧通过、左侧有几何通道 | 能生成历史之外的新路线 |
| C | 起点脱离历史轨迹但在 free 区 | 可以从任意可靠栅格起点规划 |
| D | 点云缺口 | unknown 不会被当作 free |
| E | 原通道被阻断 | 可选择其他已连通通道 |
| F | 朝向与目标相反 | 先原地转身再前进 |
| G | 视觉定位过期 | 停止续发有效运动 |
| H | 人工停止/接管 | 旧指令不会恢复 |
| I | 反馈/启动/TTL 延迟 | 不把延迟误判为到达 |
| J | 尺度缺失或外参门禁失败 | 拒绝真实执行授权 |
| K | 已在目标观察区 | 不先离开再绕回 |

## 当前精确阻塞项

1. 当前导航项目的地图单位仍可能是 `map_units`，没有本轮可核验的米制尺度。
2. 相机到 Go1 机身外参、地图上方向和地图到机身绝对偏航尚未形成现场标定证据。
3. `RobotLink` 的 `actual_yaw` 在控制仓库中是 yaw 速度反馈，不能直接当绝对航向。
4. 真实新鲜视觉位姿还没有接入这次闭环运行；已有定位耗时和丢帧边界不能被固定时间播放掩盖。
5. 当前适配器是显式授权入口，默认没有自动 TAKE、ARM、恢复或实机连接。
6. 原厂 `gait=4` 或 `avoidance_selected` 只能证明控制器选择了某种模式，不能证明现场障碍已被正确检测。
7. 现有稀疏点云不是稠密地板和墙体。若要形成可执行地图，还需对同一相机位姿运行 COLMAP `image_undistorter → patch_match_stereo → stereo_fusion`，并记录覆盖率、噪声和失败区域；低纹理处不能靠补点伪造。

## 下一次现场验收顺序

1. 先断开运动适配器，只采集一段新的相机画面和 RobotLink 状态，核对时间戳、图像延迟、网络状态和 `robot_fresh`。
2. 用已知距离和水平面完成尺度、上方向、相机外参及偏航零点标定，保存版本化标定文件。
3. 机器狗趴下或保持安全静止时，先测试定位更新、停止请求、手动接管和掉线；不发送非零 MOVE。
4. 由现场人员明确授权一次短、低速、可见的单原语测试；每次只允许一个分段，等待静止反馈和新视觉观测。
5. 依次验收停止、人工接管、视觉过期、网络超时、控制器故障和重新规划；任何一项失败都撤销导航授权。
6. 只有在上述证据完整后，才讨论连续路径跟踪。原厂避障必须作为独立能力记录，不能代替导航层的几何可通行性证明。

## 与现有功能的关系

现有建图、目标照片/ROI、PnP定位、历史路线审核、Viser地图回放、采集工作台和人工遥控文件保持不变。旧的 `plan_route` 仍是历史路线预览；新栅格规划和闭环适配器是独立入口，默认不会覆盖或自动启用旧流程。
