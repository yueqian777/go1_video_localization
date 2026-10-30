# Go1 自由空间导航升级版深度修复记录

日期：2026-10-29

本轮修改覆盖离线地图、闭环状态机、目标工作台接入和 Go1 远程控制权限。全程没有连接机器狗，没有发送运动指令。

## 已修复的 P0 问题

1. `occupied` 在任何规划模式都是硬障碍。原来的 `allow_unknown=True` 会把 occupied 一并放行；现在只有 `unknown` 可以在 `EXPLORATION_CANDIDATE` 模式形成不可执行候选，候选不能进入闭环授权。
2. `FreeSpaceMap.world_to_cell()` 和 `cell_to_world()` 是唯一坐标入口，统一处理非零原点和非单位分辨率。闭环用世界坐标转换到 cell，已移除 `round(observation.x/y)`。
3. 每个 turn/forward primitive 获得唯一 `command_id`，动作发出后不递增 route index。只有匹配 command_id 的停止反馈、超过动作开始时间的新视觉 frame、目标朝向或目标 cell 都满足时才确认；超时进入 `REPLANNING`，不会自动接着发下一动作。
4. `PoseObservation` 保留 `frame_id`、采集 session、source sequence、source/receive/localization time、map version、localization quality 和目标可见性。生产配置要求 frame/session/sequence/map version；重复 frame 不能完成动作。
5. 最终目标必须由新的目标观察证据确认。到达几何 cell 但没有 `target_visible` 时只进入等待观察状态。

## 几何和地图诊断

`inflate_obstacles()` 要求调用方明确提供机器狗足底半径，将 occupied 膨胀为不可通行区域；不会从稀疏单目点云猜足底尺寸。`free_space_diagnostics()` 输出 free/occupied/unknown/uncertain 数量和比例、4 邻域连通分量、未知孔洞候选、尺度状态、预览/执行几何门槛。

`tools/build_free_space_sidecar.py` 增加了 `--origin-x`、`--origin-y` 和 `--footprint-radius`。sidecar 始终携带 `execution_permitted=false`。

## 目标工作台接入

`src/go1loc/free_space_workflow.py` 从已加载地图、当前定位或选中参考帧和 `supported` 目标结果自动选择目标支持相机，不要求用户手工输入网格坐标。目标点用于选择观察相机，终点仍是观察相机而不是物体中心。

Viser 主面板增加“生成自由空间路线预览（不控制机器狗）”。它读取已绑定的自由空间 sidecar，保存 `free_space_route-*.json` 并渲染青色网格路线。地图单位或不完整几何只能显示预览，不能进入执行闭环。

## 稠密 MVS

`src/go1loc/dense_mvs.py` 和 `tools/audit_real_navigation_evidence.py` 会先检查 COLMAP、图像目录和完整 sparse model。默认只生成命令计划；只有显式 `execute=True` 才运行 `image_undistorter`、`patch_match_stereo`、`stereo_fusion`，并且必须看到非空 `fused.ply` 才报告完成。

本机能力检查结果：`COLMAP_NOT_FOUND`，因此没有伪造稠密点云结果。

## 现有 Go1 数据复核

本轮使用已有 Go1 屏幕采集地图：

```text
results/playback_20261015/jobs/53842d7f428e436a9efc3690998ca5b5/output/scene.json
```

该地图为 `go1-screen-exploratory-30s-v2`，`units=map_units`，`scale_status=arbitrary_monocular_scale`。从 2403 个观测 cell 生成的 sidecar 全部为 `uncertain`，因此只能预览，不能证明自由空间，更不能执行。

目标绑定审计扫描了现有 15 个 `object_result.json`，没有找到同时匹配该地图 `map_id + map_hash` 的目标结果。因此当前真实数据链条的阻塞点是目标证据没有绑定到这张 Go1 地图，不是规划器擅自补了目标。

审计结果：

```text
results/navigation_upgrade_20261029/deep_fix_real_evidence_audit.json
```

## 远程控制闭环

`_audit/go1_remote_control` 增加 `pc/navigation_authority.py`：

- 导航和网页控制共用同一个 `RobotLink`，不增加第二个 SDK/UDP/TCP 控制源；
- 导航只接受已建立的 `control_active + armed + fresh` 会话，不自动 TAKE 或 ARM；
- 每个任务有 `navigation_token`、task lease 和唯一 command_id；
- 只允许六个单轴 primitive；
- lease 过期、状态不新鲜、重复 command_id 或网页 STOP/DISARM/disconnect/release 会停止并撤销导航所有权；
- `NavigationHttpClient` 是主仓库闭环到远程服务的接口。

具体接口和配置见远程仓库的 `NAVIGATION_AUTHORITY.md`。自动导航的任务租约、运动意图有效期、浏览器按键租约、网络新鲜度和接收端 MOVE TTL 已拆分；视觉短暂丢帧进入 `WAIT_RELOCALIZATION`，显式暂停进入 `PAUSED`，恢复先进入 `RESUMING`，必须以当前位姿重新生成路线后才会再次发出非零动作。

## 本轮集成修改与边界

- 连续同向前进格子按 `max_forward_cells` 合并，默认最多 3 格；转角、路径断点和障碍区域仍保留独立动作。A* 主干没有修改。
- `Telemetry` 明确区分 command id、执行中、实际停止、遥测时间、导航租约、人工接管、实际速度和 yaw 角速度。`predict_pose()` 只在不超过 0.5 秒的窗口内使用机身速度补齐短空缺，绝不把 `actual_yaw` 改写成绝对朝向，也不替代视觉全局修正。
- `NavigationHttpClient` 增加 keepalive/pause/resume；暂停不保留非零意图，恢复不会重放旧 command。真实控制链仍没有自动 TAKE、ARM 或第二套 SDK。
- 远程仓库的 `tests/test_cross_repo_navigation.py` 使用原生模拟接收端、PC 服务和本仓库HTTP客户端验证授权—运动反馈—暂停—恢复—新动作—停止链路，并验证动作截止后不会继续续发。

## 验证结果

主仓库：

```text
tools/run_tests.ps1
Ran 613 tests ... OK
```

新增自由空间和闭环回归共 37 项通过，A–K 离线场景全部通过，`motion_commands_sent=false`。控制仓库相关回归及跨仓库模拟集成共 12 项通过；MSYS2 下控制策略测试通过。

远程控制仓库在 MSYS2 g++ 模拟接收端下：

```text
tests/test_system.py
Ran 31 tests ... OK
```

这些结果是离线/模拟验证，不是 Go1 实机行走验收。真实执行仍需要米制尺度、相机到机身外参、足底尺寸、实时目标证据、远程控制任务租约和现场人工急停条件。
