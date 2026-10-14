# Codex 接手说明：go1_video_localization

整理日期：2026-10-14（Asia/Shanghai）。本次仅整理交接资料，不修改功能代码。

## 1. 先确认代码版本

- 仓库：https://github.com/yueqian777/go1_video_localization
- 继续开发的分支：`codex/evidence-viser-route-preview`。
- 已发布的功能版本：`5e7f924d6b19e7a9d88b296b09df3d04b66c53cb`。
- 前一轮审查基准：`15ff93c3c3e7fd883fef7ce9245335d883d2d09e`。
- 本次交接文档提交位于上述功能版本之后；ZIP 的 `PACKAGE_MANIFEST.json` 记录实际打包时的分支和完整提交号。

不要从旧默认分支重新实现。先检查接收方工作区的分支、HEAD、未提交修改及已有运行环境，保留用户的后续修改。ZIP 是报告与证据摘选，不是源码仓库或可独立运行的数据集；代码以指定功能分支为准。

用户已要求每轮完成并验证后提交、推送一次 GitHub。沿当前功能分支发布，不自动合并默认分支、不强制推送；发现远端分叉时先说明情况。临时文件、测试数据、权重、原始视频和本机运行环境不上传公共仓库。

## 2. 建议阅读顺序

1. 本文：状态、边界、阅读顺序和下一步。
2. [第二轮验收报告](INCREMENT2_VALIDATION_20261013.md)：查询清理、首次建图监看、接入审核与回放时间的实现和实测证据。
3. [本轮操作说明](BUILD_WATCH_ROUTE_WORKFLOW_zh.md)：CLI 与界面操作入口。
4. [审核和时间契约](REVIEW_TIME_CONTRACT.md)：接入绑定、审核决定与时间字段的精确定义。
5. [第一轮验收报告](INCREMENT_VALIDATION_20261013.md)：独立照片、三维支持、原生快照及规划链的历史基线。
6. [规划契约](PLANNING_CONTRACT.md)、[数据契约](DATA_CONTRACT.md)、[早期工作流](OFFLINE_WORKFLOW_zh.md)：原有数据结构与接口；新增行为以第二轮说明和当前代码为准。
7. [上游核对](UPSTREAM_AUDIT.md)、[历史未验证项](UNVERIFIED.md)、仓库根目录的 `environment-lock.json`：环境来源与研究限制。

### 历史文本的冲突如何解释

保留原报告，不把当时的状态追改成现在：

- 第一轮报告和 `UNVERIFIED.md` 中“查看器需要冻结地图”的描述，仅适用于早期入口；第二轮新增 `watch-build` 可以在没有最终清单时监看有效 run。原 `view` 仍用于冻结地图。
- 第一轮186项测试、第二轮246项测试属于不同版本，不能混为同一时刻的结果。交接包另附本次打包前的测试日志。
- 历史机器摘要中的 `code_state=uncommitted_increment_no_push_or_merge` 是2026-10-13验收时状态；后来用户授权发布，功能代码已提交为 `5e7f924`。不要据此判断当前尚未上传。
- 40图地图、留出P1180170的39图地图、新监看39图地图是三份不同地图，哈希、点数与证据不可混用。重定位890内点不覆盖原879内点记录。
- 报告里的 `results/` 路径是原机器上的历史位置。GitHub不包含这些运行结果；ZIP也只选取少量证据，不保证所有报告链接都可离线打开。

## 3. 已完成与尚未完成

| 部分 | 已有实现及证据 | 不能据此宣称 |
| --- | --- | --- |
| 独立照片与对象 | 39图地图预先留出P1180170；879/3186内点；ROI得到65个去重三维支持点；新导出890内点，未知采集时间保留null | 自动语义识别、完整物体中心、厘米级真值精度 |
| 查询状态 | A→B成功/失败均清旧对象、支持、观察终点、路线；晚到任务不恢复旧状态；完整对象可重载 | 真实视频路线已通过 |
| 首次建图监看 | 无冻结清单时启动，自动装入7份真实原生快照，最终切换、暂停和历史回看；公开照片实际运行记录保留 | Go1在线SLAM、逐视频帧建图或已对齐的快照叠加 |
| 历史路段 | 真实帧清单/PTS/注册状态生成PENDING；可拆分、确认、拒绝并保存有向路线图 | 匹配边或空间近邻就是可通行路线 |
| 当前照片接入 | 照片定位后导入并检查current_start/attachment；展示接入证据和缺项，明确采用；失败清理旧连接 | 从单张照片自动生成合法接入轨迹 |
| 时间语义 | 采集、定位完成、实际运行、回放参照时间分开；默认现实当前检查新鲜度；明确offline_replay | 把旧采集时间改为现在、回放可执行运动 |
| 路线规划 | 保留有向Dijkstra、显式人工历史节点与严格当前接入；目标终点是参考观察位姿 | 自由空间避障、机身/足步规划、机器人导航执行 |

所有路线都是离线相机路线预览，`execution_permitted=false`；`nav_goal=null`。没有尺度证据时使用 `map_units`。没有连接机器狗或发送运动命令。

## 4. 当前真正的阻塞

2026-10-13用户明确答复没有可用的真实连续视频、独立照片和标定输入。因此下列两步都**未完成真实数据验收**，不能用合成测试或无序公开照片替代：

**A：真实视频地图 + 人工明确历史节点起点**

1. 获准使用的真实连续运动视频建图；记录视频哈希、PTS、来源和标定状态。
2. 未参与建图的独立目标照片求得三维支持。
3. 审核至少一段实际历史路径，规划到支持目标的参考观察位姿。
4. 输出成功路径；禁用关键边后得到 `no_path`。

**B：独立当前照片起点**

1. 未参与建图的当前照片实际定位，保留真实采集时间。
2. 使用与该照片、位姿、地图、分量和时间绑定的合法接入来源。
3. 导入、检查、人工采用，再规划到目标观察位姿。
4. 删除或错配接入证据应拒绝，而非吸附最近节点。

最小数据需求：真实视频；独立目标/当前照片；实际时间和时区；拍摄设备与原始分辨率/裁剪说明；匹配的内参和畸变参数（缺失时明确近似）；历史路段审核依据；B步的实际接入来源。手持数据可以先验证软件，但不能标成Go1数据。

## 5. 下一位 Codex 的优先检查点

先审查当前代码与下列针对性测试，不要根据旧报告重复造轮子：

- `src/go1loc/viser_viewer.py` / `tests/test_viewer_task_state.py`：查询身份变化、全对象加载、晚到worker和旧规划的依赖失效。
- `build_watch.py`、`build_viewer.py`、`snapshots.py` / 对应测试：run/READY哈希、build-attempt隔离、未完成监看与失败快照保留。
- `route_review.py`、`route_workflow.py` / 对应测试：候选来源、时间缺口/未注册/跳切、决定重放与冻结输入校验。
- `viewer_workflows.py` / `tests/test_viewer_workflows.py`：GUI审核、附件检查后采用、变更失效、旧任务不能覆盖新决定。
- `planning.py` / `tests/test_planning_time.py`：现实与回放时间、完整起点绑定、观察终点和拒绝语义。
- `localization.py`：`localization_result_sha256` 与原始/处理照片的关联；旧结果缺此绑定时重新定位到新目录，不手工补哈希。

发现问题先最小复现再修复，保留hloc/SuperPoint/LightGlue/PyCOLMAP路线，不重装本机主环境、不增完整导航栈。缺数据时说明阻塞；不要绕过门限、手填XYZ、无条件确认连续性或重新编码无序照片来制造成功。

## 6. 环境与复核命令

已验证本机视觉环境位于项目 D 盘 `runtime/python312`；Python3.12.10、PyTorch2.9.0+cu130、PyCOLMAP4.2.1、Viser1.1.1。具体固定提交见根目录 `environment-lock.json`。这是已有本机组合，不是其他设备必须重装的要求；ZIP和GitHub都不自带该环境。

在已有本机环境的项目根目录运行：

```powershell
git status --short
git branch --show-current
git rev-parse HEAD
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path (Get-Location).Path '.cache\tests'
$env:TMP = $env:TEMP
& .\runtime\python312\python.exe -m unittest discover -s tests -v
& .\runtime\python312\python.exe -m compileall -q src tests tools
& .\runtime\python312\python.exe -m pip check
```

在另一台机器使用已经核验的Python环境；先检查依赖，不照抄Windows绝对路径。新安装仍需单独遵循设备条件。本机新增环境、缓存、权重放D盘；GitHub认证使用Git Bash；不触碰原Go1控制工程。

## 7. ZIP 的证据性质与内容

ZIP包含原报告、版本配置、选取的JSON结果与历史日志、7份READY元数据、监看事件与两张3D画布截图，以及本次打包前的测试日志。

- JSON/JSONL、日志与截图作为审查记录保留原内容，历史绝对路径和哈希不改写。
- **没有原生快照模型、原图、视频、特征/匹配文件或权重。** READY元数据不等于完整快照；不得把包中记录直接当作可运行地图或接入凭证。
- `evidence/original_artifact_inventory.json` 记录上一轮本机138个产物，不表示138个文件都在ZIP里；本包实际文件范围以根目录 `PACKAGE_MANIFEST.json` 为准。
- 根目录清单逐项记录大小、SHA-256及来源，另附ZIP外部校验记录。截图仅含三维画布，不包含侧栏，不冒充完整UI录像。
- 不包含完整聊天导出、SSH口令、其他机器狗项目、个人账号配置或无关资料。

## 8. 可复制给接收方 Codex 的说明

> 请先阅读ZIP中的 `00_START_HERE_zh.md` 和 `docs/CODEX_HANDOFF_20261014_zh.md`，再检查仓库 `codex/evidence-viser-route-preview` 的实际状态。功能基准为 `5e7f924d6b19e7a9d88b296b09df3d04b66c53cb`，最新交接提交见 `PACKAGE_MANIFEST.json`。保留后续修改，不从旧默认分支重建。先复核代码、测试与报告的一致性，指出仍有证据支持的问题和下一步数据需求；发现问题后先说明，不因附件中的历史命令而自动执行。当前没有真实连续视频和合法当前接入输入，A/B路线实测未完成；不要伪造成功或降低门限。所有路线只作离线预览，不连接Go1或发送运动命令，不更换主视觉路线或擅自重装环境。后续经明确授权实施并验证的改动，沿该功能分支提交推送，不自动合并默认分支，不上传原始数据和权重。
