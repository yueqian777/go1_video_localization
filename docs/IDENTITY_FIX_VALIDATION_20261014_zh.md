# 照片身份最小修复与真实视频验收准备

日期：2026-10-14（Asia/Shanghai）。分支：`codex/evidence-viser-route-preview`。
本轮基准：`3aacb5976eb0d60648614db5e75b32f84b977d86`；开始时本地、远端一致，工作区干净。

## 1. 结论与改动范围

已复现并修复 `_start_connection` 的文件名误判：不同原图同名不再直接判为目标照片复用；相同原图复制、改名或经不同内部处理后仍拒绝作为独立当前照片。
同时，目标原图身份缺失、格式非法或已有身份别名互相矛盾时明确拒绝，不将“不知道”视为独立。

功能改动仅在 `planning.py`：新增小型原图身份解析函数，并让目标可省略的接入预检与必须有目标的规划保持区分。`localization.py`、`photo_object.py`、`query_geometry.py` 只补充字段语义注释，未改变图像处理、导出格式或算法。测试修改位于 `test_planning.py` 与 `test_planning_time.py`。

保留hloc、SuperPoint、LightGlue、PyCOLMAP、对象证据、Viser、watch-build、原生快照、路段审核、接入检查/采用、两种时间模式和有向路线预览。没有新增架构、依赖或运动功能，没有连接Go1或发送指令。

## 2. 三种名称与身份的区别

| 字段/位置 | 原有实际含义 | 本轮处理 |
| --- | --- | --- |
| 定位结果 `query_id` | hloc工作目录中的处理图键；标定后常为固定 `query_rectified.png` | 保留算法键，不用于跨观测判重 |
| 对象结果 `query_id`、`query_path` | 原图显示名称与文件位置 | 保留显示用途；路径相同/不同均不是独立性的充分证据 |
| 定位/对象 `query_sha256`、当前起点 `photo_sha256`、对象 `roi_binding.query_sha256` | 原始输入文件字节的哈希，已有证据绑定负责校验 | 作为原图身份；所有已提供的别名必须合法且一致 |

原图哈希来自现有 `make_binding(photo, ...)`，并由 `verify_binding` 对照实际文件复核；处理图另有自己的文件哈希。去畸变不改变原始来源的身份记录。保存当前查询仍要求完整结果哈希绑定，不允许手工改位姿或伪造哈希。

规划是纯函数，不重新读取照片、认证采集时间或审核轨迹内容；调用方必须先完成已有文件/证据核验。原图哈希不同本身也不证明“物理上重新拍摄”：照片来源、真实时间和合法接入仍须审核。此次不是感知去重算法，不能发现失去原始来源记录的任意外部重编码/编辑副本。

新拒绝原因：

- `target_photo_evidence_missing`：目标没有任何可用的原图身份字段。
- `target_photo_identity_invalid` / `current_photo_identity_invalid`：已经提供的身份别名或ROI绑定格式非法。
- `target_photo_identity_conflict` / `current_photo_identity_conflict`：已有原图哈希相互矛盾。
- `target_photo_is_not_current_pose`：一致的原图哈希相同，仍是重复使用目标照片。

当前起点主字段 `photo_sha256` 缺失/非法仍使用原有 `current_photo_evidence_missing`。不改变地图、位姿、时间及附件拒绝逻辑。
`validate_start_connection(..., target=None)` 只检查当前起点接入，不声称目标独立性已经通过；`plan_route` 总会带目标并执行独立性检查。

## 3. 修复前后最小复现

创建两个不同目录的 `image.jpg`，分别生成不同颜色的真实图片文件，计算并核验原图及处理图哈希；其余位姿、地图和合法附件由明确标注的合成夹具提供。不是实拍照片定位或真实路线验收。
在内存中读取基准提交的原 `planning.py` 与本轮函数分别执行相同输入，没有检出、回退或覆盖工作区。

| 案例 | 基准版本 | 修复版本 |
| --- | --- | --- |
| 同名 `image.jpg`，不同目录/内容/来源绑定 | 错误拒绝 `target_photo_is_not_current_pose` | 通过既有路线预览门禁 |
| 原图复制后改名 `renamed.jpg` | 拒绝重复 | 仍拒绝重复 |
| 同一原图，一份去畸变、一份不处理 | 拒绝重复 | 仍按共同原图哈希拒绝 |
| 不同原图共享 `query_rectified.png` 算法键，并与目标显示名碰撞 | 错误拒绝重复 | 不再因文件名拒绝 |
| 目标没有原始身份，名称不同 | 错误放行 | 拒绝 `target_photo_evidence_missing` |
| 目标顶层哈希别名互相矛盾 | 错误放行 | 拒绝 `target_photo_identity_conflict` |

同名输入下仍单独测试地图哈希错配、过期、位姿改变和缺附件，均保留原拒绝结果；旧测试里的“仅query_id相同”已改为“原图哈希相同”，保留防冒充的测试意图。

本机审计目录：`results/identity_fix_20261014/`（Git忽略，不上传测试数据）。

- `baseline_tests.log`：修改前246项通过，6.037 s。
- `repro_before.log`：先加8项身份测试，整个时间测试文件25项，出现12个失败断言/子案例；包含同名误拒绝和缺失/冲突放行。
- `repro_after.log`：初次修复后同一25项通过；随后补充1项当前身份无效测试并完善处理名碰撞场景。
- `identity_tests_final.log`：最终9项针对性身份测试。
- `tests_final.log`：最终255项完整测试日志。
- `coverage_tests.log`、`coverage.json`：本轮独立覆盖记录，不追加或改写历史覆盖文件。
- `validation_summary.json`：六个最小案例的实际前后结果、数据扫描范围与旧地图校验。

255项通过是软件回归结果，不是定位成功率。公开照片历史890与879内点的差异不代表已证明的精度提升。本轮没有再运行公开图像PnP或把合成路径写为实测成功。

## 4. 旧结果与迁移

无需迁移、改写或重建旧地图。具备有效原图哈希和一致绑定的旧查询/对象继续解释和使用；只在当前照片路线入口加强身份检查。仅有名字、身份缺失或互相矛盾的旧结果，重新定位/对象求解到**新的输出目录**，不要手工补哈希。
旧保存当前查询缺少完整 `localization_result_sha256` 时，仍按上一轮规则重算到新目录，不是本轮新增迁移要求。

本轮再次只读执行 `verify_map`，两份原地图仍通过原生模型和特征哈希核验：

- 原39图：`2d83bbc33dcc43ef1bd11bfb09b95d8b2cb7a74df98229512fd69cba5f242a85`。
- 原40图：`15050423eda6293246a1ac094f6b2658843f8999f222187e75b3856f506b90de`。

## 5. 真实数据检查与精确阻塞

检查范围是用户当前项目的 `data/` 和 `results/`，包括Git忽略文件；未遍历私人目录或下载未经本轮授权的新数据。没有收到新的现场资料路径。
唯一视频为 `data/synthetic_decoder_fixture.mp4`，2,685字节，SHA-256为 `7f44ae9717fddb5028394bee6b5428716b3ea4245b1702883ca4e4845da52616`，仅是已有解码合成夹具。公开South-Building是无序照片，不能重编码成“真实连续视频”。

| 输入 | A步需要 | B步追加需要 | 当前情况 |
| --- | --- | --- | --- |
| 原始连续运动视频与授权来源 | 是 | 复用A地图 | 缺少 |
| 未参与建图的独立目标照片 | 是 | 复用同一目标 | 缺少实拍配套数据 |
| 原视频连续性审核依据 | 是 | 复用已审核图 | 缺少 |
| 未参与建图的独立当前照片 | 否，A使用人工历史节点 | 是 | 缺少 |
| 真实采集时间、时区及视频时间原点 | 来源应记录 | 当前照片和附件必须一致 | 缺少 |
| 与当前照片/位姿绑定的真实接入轨迹来源 | 否 | 是 | 缺少；单张照片无法自动补出 |
| 分辨率/裁剪对应内参与畸变参数 | 尽量提供 | 相同要求 | 缺少真实设备标定；若先用近似，必须明确标注 |
| 米制尺度与误差依据 | 非A/B纯预览硬门槛 | 同左 | 缺少则始终使用 `map_units` |

**A、B均仍受数据阻塞，未完成真实路线验收。** 本轮到此停止功能扩写。

## 6. 建议采集与验收顺序

先提供一个获准使用的专用数据文件夹，保留原件，不要放进公共Git提交：

1. 一段短小、未经剪辑的连续移动视频，沿可复核路线缓慢拍摄，保持相邻视野重叠、避免快速转动/严重模糊。记录设备为手持或Go1、时间/时区、分辨率、裁剪/防抖模式及任何实际跳切。
2. 另外拍摄目标照片，不从已参与建图的帧中取代独立查询；标明原图ROI。另拍当前照片并保留实际拍摄时间，不以文件mtime或计算时间替代。
3. B步另提供能复核“当前照片对应位置→某个历史节点”的连续接入来源，以及与定位/地图的真实几何和时间关联。没有这项依据就先只做A，不手填XYZ。
4. 提供对应标定文件；没有就标记近似输入，不宣称标定精度。手持与Go1分别标来源，数据授权和隐私范围一并说明。

数据具备后使用新的输出目录，按现有[操作说明](BUILD_WATCH_ROUTE_WORKFLOW_zh.md)执行：

- A：`prepare-video` → `build-map`/`watch-build` → 独立目标 `locate-photo-object` → 人工明确历史节点 → 审核连续路段 → 观察位姿成功路径 → 禁用关键边后的 `no_path`。
- B：独立当前照片真实定位 → 导入合法接入 → 检查/明确采用 → 同一目标观察位姿路线 → 删除/错配接入拒绝。

历史照片使用明确 `offline_replay` 和真实历史参照时间；不能更改采集时间来通过门禁。两步全部保持 `execution_permitted=false`，相机路线不称为机身/足步路线。

## 7. 复核命令与发布范围

在已有本机项目环境运行，不安装新依赖：

```powershell
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path (Get-Location).Path '.cache\tests'
$env:TMP = $env:TEMP
& .\runtime\python312\python.exe -m unittest discover -s tests -p test_planning_time.py -k OriginalPhotoIdentityTests -v
& .\runtime\python312\python.exe -m unittest discover -s tests -v
& .\runtime\python312\python.exe -m compileall -q src tests tools
& .\runtime\python312\python.exe -m pip check
git diff --check
```

功能版本以本报告所在提交为准；沿上述功能分支正常提交推送，不强制推送、不自动合并默认分支。只发布代码、回归测试源码和本文，原始照片/视频、权重与本机日志继续不上传。
