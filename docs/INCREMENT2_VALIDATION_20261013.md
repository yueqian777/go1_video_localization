# 2026-10-13 第二轮增量验证与结果清单

## 结论

本轮的软件增量已实现并完成回归：查询依赖失效、首次建图独立监看、路段人工审核、当前照片接入导入/检查/明确采用，以及独立的离线回放时间语义。
**真实连续视频的 A/B 路线实测尚未完成：用户明确确认没有对应数据。** 以下公开照片建图、实际浏览器检查、合成模块测试分开记录，不把它们写成真实运动路线验收。

实施前核对工作区干净，开发分支为 `codex/evidence-viser-route-preview`，本轮基准提交为 `15ff93c3c3e7fd883fef7ce9245335d883d2d09e`。
实施与验收阶段未提交、未推送；验收后用户追加要求“每次完成都提交一次github”，本轮发布按该授权提交并推送当前功能分支，不自动合并默认分支。未检出旧默认分支、未回退、未连接机器狗、未发送运动命令、未安装/升级主视觉环境。

## 1. 实现与验收对应

| 要求 | 实现 | 核验 |
| --- | --- | --- |
| A→B 查询切换 | 对象、支持、观察终点、路线、旧查询画面统一失效；保留显式起点；晚到任务不回填旧目标 | 成功B、失败B、读文件失败、对象重载、旧计划/晚到worker回归；实际浏览器检查失败B清理 |
| 首次建图 | `run.json` 原子发布，`watch-build` 独立入口；阶段/events、READY快照、自动/手动/最终切换、失败保留 | 未有冻结清单时启动，真实自动显示7份中间状态；手动回看第一份 |
| 当前照片接入 | 原照片与处理图分别核验；结果整体哈希；图节点候选、接入路径/证据/缺项显示；检查后人工采用 | 合成导入/错配/换文件/错来源/时间/方向等回归；实际公开照片结果导入并明确显示缺采集时间与附件 |
| 历史路线审核 | 从冻结输入和原生参考位姿产生PENDING，时间/注册/质量/跳切屏障，拆分/确认/拒绝；重放决定保存 | 纯函数、worker I/O与GUI回调测试；无序照片实际生成零候选并保存零边图 |
| 回放时间 | `live_current` / `offline_replay`；采集、计算、实际运行、历史参照分开 | 不改采集时间；过期/未来/无时区拒绝；回放仍须合法接入且不可执行 |
| 真实视频路线A/B | 保留原规划器；没有替代数据或自动连线 | **数据阻塞**，不声称已通过 |

使用步骤和命令见 [操作说明](BUILD_WATCH_ROUTE_WORKFLOW_zh.md)，具体输入契约见 [审核与时间契约](REVIEW_TIME_CONTRACT.md)。

## 2. 首次建图真实监看记录

新目录：`results/build_watch_validation_20261013/`。
数据是已有 South-Building 39张公开无序照片，不含连续运动时间，不是Go1视频。
本次重建产生全新地图 `south-watch-39`，没有覆盖原 `south-independent-39`。

实际运行：

```powershell
& .\runtime\python312\python.exe -m go1loc.cli build-map `
  --images results/increment_validation_20261013_v2/reference_images `
  --output results/build_watch_validation_20261013/map `
  --map-id south-watch-39 --max-keypoints 2048 `
  --snapshot-frames-freq 5 --snapshot-max-count 8 `
  --sidecar results/build_watch_validation_20261013/build_runs

& .\runtime\python312\python.exe -m go1loc.cli watch-build `
  --run results/build_watch_validation_20261013/build_runs/2b0cdb2dc48d457f81aea04d0b0d715e `
  --sidecar results/build_watch_validation_20261013/viewer --port 8766
```

浏览器在21:47:17（UTC+8）连接时尚无冻结地图，日志 `watch_started.data.frozen_manifest_exists=false`。
界面先显示特征/匹配等实际阶段，之后依次自动显示以下完整模型：

| 时间（UTC+8） | 显示状态 | 相机数 | 3D点数 | 当时冻结清单存在 |
| --- | --- | ---: | ---: | --- |
| 21:48:08 | READY 1 | 7 | 2,481 | 否 |
| 21:48:10 | READY 2 | 12 | 4,173 | 否 |
| 21:48:14 | READY 3 | 17 | 5,644 | 否 |
| 21:48:18 | READY 4 | 22 | 6,512 | 否 |
| 21:48:22 | READY 5 | 27 | 7,329 | 否 |
| 21:48:25 | READY 6 | 32 | 8,625 | 否 |
| 21:48:32 | READY 7 | 37 | 9,685 | 否 |
| 21:48:39 | 最终冻结地图 | 39 | 10,222 | 是 |

这些来自原生增量重建快照，不是最终云抽样。21:49:19暂停自动跟随并手动加载历史第一份，界面回到7相机/2,481点。
快照标签明确为 `BUILD SNAPSHOT — final-map alignment unknown`；加载最终地图时整份替换，没有假定坐标对齐。

新最终清单哈希：`5a9431345c4f30b8800e43dfc2dc97d73795074a11b1d2725c1c4337f9a384a6`。
均值重投影误差约1.31136 px；这不是位姿真值误差，也不是尺度精度。
累计特征/匹配61.3333 s、至重建结束99.5575 s；不是与上次相同条件的性能对照。

记录与界面证据：

- `build.log`：该次实际建图输出；
- `build_runs/2b0cdb2dc48d457f81aea04d0b0d715e/run.json`：run身份、来源、完成状态；
- 同目录 `events.jsonl` 及 `snapshots/snapshot-000001` 至 `snapshot-000007`；
- `viewer/watch-b64e3f75ab0c45c49fac453b1e3cd301.jsonl`：启动无冻结清单、7次自动快照、最终切换和手动回看；
- `viewer/screenshot-59d0e3189f514898bef0a240c4ee2527.png`：建图尚未结束时保存的真实3D画布，含 BUILD SNAPSHOT 标签。

保存截图不含侧栏；阶段与计数另由 JSONL 和实际浏览器读取核对。失败保留、非法READY、未发布模型拒绝等异常由专门回归测试覆盖，本轮没有故意中断这次真实成功重建来冒充故障实测。

## 3. 实际浏览器路线输入与查询切换

在8767端口用原39图地图启动完整界面，sidecar为 `results/workflow_ui_validation_20261013/`。

1. 加载39相机/10,343点的原冻结地图，PTS显示为无序照片不可用。
2. `Generate PENDING candidates` 的实际worker读取原地图后返回 `source_kind=unordered_photos`、0候选，报告 `video_source_hash_required` / `video_provenance_missing` 等原因。
3. 点击保存，实际worker `b67c98b618a046eb914378f581015595` 输出0确认路段、0条有向边，不制造连续性。
4. 加载原完整对象结果，恢复支持点和支持参考相机；可见状态 `Object result: supported`。
5. 将查询切换到不存在的B目录，再点击规划，返回 `route_requires_explicit_start_and_object_evidence`。实际画面确认旧支持和目标相机消失。
6. 最终代码重启后导入本轮新导出的公开照片定位结果，采集时间仍空；`Inspect graph and current connection` 明确显示 `missing=[captured_at, attachment]`、`invalid_start_time`，没有绘制合法接入。

第5项的实际公开照片场景本来没有成功路线，不能声称此场景实测了“A的成功路线消失”；**A已有目标和路线→B切换成功/失败**由 `test_viewer_task_state.py` 的明确合成状态回归验证。
拆分/确认/拒绝与成功附件导入在GUI回调及worker I/O测试中使用合成夹具；没有把它们写成真实视频现场人工审核。

审核产物：

`results/workflow_ui_validation_20261013/jobs/b67c98b618a046eb914378f581015595/output/reviewed/`

其中 `route_review.json`、`route_segments.json`、`route_graph.json` 可直接复核。全部worker请求、状态、日志和输出也保留在各自job目录。
最终界面的三维画布另存为 `results/workflow_ui_validation_20261013/screenshot-a7e5bfc08eb14be88f41f3e623182789.png`，不含侧栏缺项文本。

## 4. 真实公开照片的新结果绑定核验

在原39图地图上，将独立照片 P1180170 再定位到全新 `results/workflow_validation_20261013/query_bound/`，仅检验新版结果整体哈希与当前照片导入器。

```powershell
& .\runtime\python312\python.exe -m go1loc.cli localize-photo `
  --photo data/public_sample/South-Building/South-Building/images/P1180170.JPG `
  --map results/increment_validation_20261013_v2/map `
  --output results/workflow_validation_20261013/query_bound
```

本次3186对应/890内点，内点率27.9347%，均值重投影2.25105 px，通过原有门限。
`capture_time=null`，实际 `localization_time=2026-10-13T14:07:08.292477+00:00`。
导入器核验完整结果哈希、原图/处理图、特征/匹配与地图身份；未知采集时间没有被计算时间代替。
该公开图片没有可信当前采集时间与连接轨迹，所以它不是B步的合格当前起点。没有为获得路线改写其时间或位姿。

旧查询仍为879内点，旧对象仍为65支持点，旧记录未覆盖。不同次PnP采样数略有不同，不据此宣称算法精度提升。

## 5. 测试、版本与修复边界

本轮起始基线186项通过。新增针对性的状态、监看、路段审核、接入导入、时间测试；不以覆盖率百分比为本轮目标。
最终新鲜执行 **246项全部通过，5.771 s**；`compileall`、`pip check`、`git diff --check` 通过。
发布前再次执行同一套246项测试，全部通过（4.879 s）；`compileall` 与 `pip check` 再次通过。
Git的LF/CRLF提示为行尾配置警告，不是空白错误。

```powershell
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path (Get-Location).Path '.cache\tests'
$env:TMP = $env:TEMP
& .\runtime\python312\python.exe -m unittest discover -s tests -v
& .\runtime\python312\python.exe -m compileall -q src tests tools
& .\runtime\python312\python.exe -m pip check
git diff --check
```

新增回归文件：`test_viewer_task_state.py`、`test_build_watch.py`、`test_build_viewer.py`、`test_planning_time.py`、`test_route_review.py`、`test_route_workflow.py`、`test_viewer_workflows.py`。
完整日志：`results/workflow_validation_20261013/tests_final.log`。

实施中有意先复现再修复的失败包括旧查询依赖残留、新界面操作缺失、校正图查询身份误拒绝、过期路段生成/保存覆盖新决定。
整套测试第一次集成另有2项失败，是新增输入目录隔离检查要求测试夹具提供 `image_dir`；补齐夹具后通过，未削弱运行时检查。
还修正原帧清单字节哈希与JSON语义哈希误比，以及程序更新“自动跟随”复选框延迟回调可能取消手动历史选择的问题。

当前实测版本：Python3.12.10、torch2.9.0+cu130、PyCOLMAP4.2.1、Viser1.1.1、RTX5070Ti Laptop GPU。
hloc/LightGlue保持 `environment-lock.json` 的原固定提交。没有新增完整导航栈。

保留性复核：

- 原39图清单哈希 `2d83bbc33dcc43ef1bd11bfb09b95d8b2cb7a74df98229512fd69cba5f242a85`；
- 原40图清单哈希 `15050423eda6293246a1ac094f6b2658843f8999f222187e75b3856f506b90de`；
- 两份均重新执行 `verify_map`，原生模型与特征文件哈希一致；原39图查询/对象记录保留。

## 6. 完整交付与未完成项

代码增量保留在原功能分支，按验收后用户授权发布；提交与远端状态以Git记录为准。新增主要模块：`build_watch.py`、`build_viewer.py`、`route_review.py`、`route_workflow.py`、`viewer_workflows.py`。
现有CLI、worker、建图/快照、定位证据导出、规划与Viser在原文件上增量修改，没有替代项目。

文档：本报告、[本轮操作说明](BUILD_WATCH_ROUTE_WORKFLOW_zh.md)、[接口契约](REVIEW_TIME_CONTRACT.md)。上一轮报告不覆盖。
机器可读摘要与逐文件大小/SHA-256清单位于：

- `results/workflow_validation_20261013/validation_summary.json`；
- `results/workflow_validation_20261013/artifact_inventory.json`。

清单覆盖本轮三个独立结果目录，共138个文件；这些日志、原生模型、图像和截图均继续在Git忽略目录中，不上传公共仓库。
本轮临时8766/8767查看服务在验收后已停止，文件保留，可用文档命令重开；没有设置自启动或后台定时任务。

真实视频验证A（真实地图+独立目标+人工历史节点+审核路径成功/断边no_path）与B（未参与建图的当前照片+合法接入+成功/删错附件拒绝）均列为**缺数据，未实测**。
下一次只需补齐真实连续视频、独立照片、采集时间/时区及可复核标定/来源与合法接入依据，再沿本轮流程跑新目录。
不得用重新编码的无序图片或手填XYZ代替。单目无尺度继续输出map_units，终点仍为观察位姿；机身/足步路线、自由空间避障、Nav2、Go1运动执行不在本轮范围。
