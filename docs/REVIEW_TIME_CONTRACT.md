# 路线审核、当前照片接入与回放时间契约

本轮只扩展现有纯 Python 规划模块，不改变视觉技术栈，不执行机器人运动。
所有校验报告、审核文档和规划结果均为 `execution_permitted=false`。
源码位置：`src/go1loc/planning.py`、`src/go1loc/route_review.py`。

## 时间语义

```python
plan_route(graph, start, target, *, now,
           max_start_age_s=30.0, disabled_edge_ids=(), T_BC=None,
           time_mode="live_current", replay_reference_time=None)
```

`now` 始终是本次真实运行的带时区 ISO 时间，由调用层读取系统时钟后传入；
纯函数不读取系统时钟。不能把它替换为历史拍摄时间来绕过新鲜度检查。

| 字段 | 含义 | 是否可随回放改写 |
|---|---|---|
| `captured_at` | 独立当前照片的真实采集时间 | 否 |
| `localized_at` | 此次照片定位计算完成时间 | 否 |
| `planned_at` / `run_at` | 本次规划的实际运行时间，即 `now` | 否 |
| `replay_reference_time` | 用户明确选择的历史回放参照时间 | 单独选择，不覆盖前三者 |
| `selected_at` | 用户本次人工选择历史图节点的操作时间 | 必须是实际选择时间 |

默认 `live_current` 不允许携带回放参照时间；照片仍须满足
`captured_at <= localized_at <= now`，且 `now - captured_at <= max_start_age_s`。

`offline_replay` 必须提供回放参照时间，满足：

```text
captured_at <= replay_reference_time <= now
captured_at <= localized_at <= now
replay_reference_time - captured_at <= max_start_age_s
```

定位是在现在离线计算的，因此 `localized_at` 可以晚于回放参照时间，不能把它误当曝光时间。
未知采集时间不能自动填现在，也不能用定位计算时间补位；应保留缺失并拒绝照片起点。
两种模式都原样保留起点采集/定位时间，输出 `time_mode`、`replay_reference_time`、
`planned_at` 和 `run_at`。回放不松绑地图版本、目标照片独立性、接入证据或几何要求。

人工节点是明确的历史图节点选择，不是当前照片定位：仍要求
`source=explicit_pose`、`manual=true`、确切 `node_id`；若给出 `T_MC`，必须与节点位姿数值一致。
其 `selected_at` 在两种模式均相对真实 `now` 检查新鲜度，不能写成历史照片时刻。
这也不宣称机器人现实中位于那个节点。路线始终是相机路线。

非法模式、回放参照缺失/无时区/晚于运行时间，以及非法时效预算会抛 `ValueError`。
数据业务拒绝仍返回 `status=rejected`，不生成备用路径。

## 当前照片接入：预览和导入

```python
validate_start_connection(graph, start, *, now,
                          time_mode="live_current", replay_reference_time=None,
                          target=None, max_start_age_s=30.0)

import_current_start(current, imported, graph, *, now,
                     time_mode="live_current", replay_reference_time=None,
                     max_start_age_s=30.0)
```

两者返回 JSON 兼容字典：

```python
{
    "valid": False, "node_id": None, "path": [], "evidence": None,
    "camera_pose": None, "missing": [], "reasons": [],
    "time_mode": "live_current", "replay_reference_time": None,
    "run_at": now, "execution_permitted": False,
}
```

导入结果另有 `start`：通过时为可用的新起点副本，失败时为 `None`。任何输入都不原地修改。
`missing` 是缺失字段路径，例如 `attachment.source_sha256`；`reasons` 包含拒绝代码。
业务拒绝不返回可画成合法连接的 `path`。成功时可展示接入 `node_id`、`path` 和
`evidence.evidence_id/source_kind/source_sha256`；不是显示“最近节点自动吸附”。

调用方须先从现有哈希校验通过的当前照片定位结果创建 `current`，再读取外部文件为 `imported`。
Viser 的保存结果导入器还要求新版 `localization_result_sha256`，将位姿、质量与时间绑定到证据。
原图身份用原图哈希核验；标定后的 hloc 查询键按已绑定的处理图文件名核验，不能要求它等于原图文件名。
跨目标/当前照片的独立性不按 `query_id` 或绝对路径判断：不同原图可有相同basename/处理键；同一原图改名、复制或内部去畸变仍是共同原始来源。
规划要求已有 `query_sha256`、`photo_sha256`、`roi_binding.query_sha256`（按文档实际提供的字段）格式合法且一致；目标身份缺失、格式非法、别名冲突分别返回 `target_photo_evidence_missing`、`target_photo_identity_invalid`、`target_photo_identity_conflict`。当前别名冲突返回 `current_photo_identity_conflict`。原图相同仍返回 `target_photo_is_not_current_pose`。
省略 `target` 的接入预检只核验接入，不代表目标独立性已通过；真正规划仍检查目标。此改动不更改既有导出格式，不迁移旧地图。详见[照片身份修复记录](IDENTITY_FIX_VALIDATION_20261014_zh.md)。
导入格式有三种：完整 start、裸 attachment、仅包含 `attachment`（及可选 `schema_version`）的包装对象。
完整 start 必须与当前已核验结果逐项一致：

- `map_id/map_hash/component_id/query_id/photo_sha256`；
- `captured_at/localized_at`（严格按保存字段比较，不悄悄重写日期格式）；
- `source/pose_status/quality`；
- 完整 `T_MC`，包括旋转和平移；允许旧别名 `T_WC_query`，双别名不一致则拒绝。

附件本身也必须绑定上述身份和两种时间，满足原有连续性、来源哈希、节点、单位、路径、
逐点 PTS 和首末端点条件。**附件单独导入时还必须包含完整 `T_MC` 或旧位姿别名**，
避免只匹配平移而误用另一朝向的附件。完整 start 可通过其顶层完整位姿核验旧格式附件。
位姿等值沿用既有 `POSE_ATOL=1e-7` 的数值一致性公差，不是物理吸附半径。

导入不重新定位，不修正质量，也不替换照片数据。缺少附件、失败定位、旧附件错配、
轨迹方向/端点/时间不满足要求时都拒绝。非法附件不能通过人工选择另一个 `node_id` 修好。
成功导入后仍须在规划时重新检查时效和目标独立性；预览可传 `target` 提前做这项检查。

图/文档哈希只是内容一致性检查，不是签名。纯函数不认证视频内容或审核声明，
调用层必须核验实际来源；自动从独立照片生成合法接入轨迹仍不属于本次实现。
Viser 分为检查、明确采用两步；采用要求选择原始来源文件并核验 `source_sha256`，
期间输入变化需重新检查。图/时间/当前照片变化使旧预览失效，失败重导入撤销旧附件。

## 历史路段候选和审核

```python
review = generate_route_candidates(manifest, reference_poses, frames_manifest,
                                   max_gap_s=2.0)
review = review_route_candidate(review, candidate_id, decision,
                               first_name=None, last_name=None,
                               reviewer="审核人", evidence_note="检查的原始视频及连续性依据",
                               reviewed_at="带时区的实际审核时间")
```

以上均为纯函数，无文件读取或保存。生成器依赖父层已经核验的冻结地图/native 参考位姿、
真实视频 `frames_manifest`；若冻结地图嵌入了帧清单，传入清单必须与它完全一致。
必须有有效 `probe.source_sha256`。没有来源/有效时间序列时返回空候选及诊断，不用照片名造边。

返回 `go1loc.route_review.v1`，包含：

- `map_id/map_hash/source_sha256/source_kind/capture_device_kind/max_gap_s/input_sha256`；
- `source_path`：帧清单记载的原始来源路径（可能缺失，不猜测）；
- `candidates`：`candidate_id/component_id/status/verified_continuous/frame_names/pts_time_s/camera_path/parent_candidate_id`；
- `timeline`：所有抽样帧，含未注册、未保留、缺名帧及质量/断点原因；
- `diagnostics/route_segments/decisions/review_sha256/execution_permitted`。

候选按实际 PTS 排列，保留全部抽样记录作为连续性屏障。未注册、质量不合格、缺名、
过大时间缺口、已标注跳切和分量变化均断组；共视、匹配、空间邻近不参与构边。
未标注的剪辑/跳切仍须审核原视频，平滑 PTS 本身不能证明没有剪辑。
只有一个有效帧的组保留诊断，不生成可确认的两点路段。

所有候选初始 `status=PENDING`、`verified_continuous=false`，`route_segments=[]`。
`source_kind=synthetic_test` 显式标签全程保留；实际视频使用 `video`，但该标签不是真实性认证。
`capture_device_kind` 未知保持 `unknown`，不得把手持、Go1、公开照片和合成测试混为一谈。
没有视频帧清单时标注 `unordered_photos` 并返回零候选及诊断，不标成已经验证的视频。
不修改冻结清单添加人工跳切；界面可通过下面的拆分/拒绝保存审核决定。

界面操作建议：

1. 生成候选，查看 PTS、帧注册状态、相机路径和原始视频；选择候选。
2. 填写审核人、依据说明；确认可连续的整个候选，或拒绝整个候选。
3. 若只认可子区间，选 `first_name/last_name` 后执行 **split**，再分别确认/拒绝子候选。
4. 保存 worker 从已核验输入重新生成候选、重放决定，之后生成新图及独立输出文件。

`decision` 仅为 `confirm/reject/split`。确认/拒绝只接受整个候选（端点省略即整个候选），
部分确认会报 `partial_decision_requires_split`。拆分得到不重叠的前段、所选段和后段；
所有有效子段仍 `PENDING`，单帧片段写入诊断。拆分边界之间不自动生成连接。
已确认/拒绝/拆分的候选不能再次直接决策；重新审核应从原输入新建审核会话。

审核说明和审核人均非空、最长 4000 字符；最多 100000 个帧/参考位姿、10000 项决定。
决定保存为可直接重放的参数，不附带不可重放的内部对象：

```python
for decision in saved_review["decisions"]:
    review = review_route_candidate(review, **decision)
graph = build_route_graph(manifest, reference_poses, frames_manifest,
                          component_id=selected_component,
                          route_segments=review["route_segments"],
                          max_gap_s=review["max_gap_s"])
```

父层应比较 `input_sha256`、地图/来源身份及 `max_gap_s`，然后重放，不直接信任导入的
`route_segments`。候选 ID 由输入摘要、分量、帧组和父候选确定；输入变更会拒绝旧 ID。
确认生成兼容旧构图函数的路段，附带 reviewer、evidence_note、reviewed_at 和输入摘要。
Viser 另以递增审核版本及 `review_sha256` 绑定生成/保存任务；过期完成仅保留磁盘文件，
不得覆盖新的候选或将旧决定生成的图设为当前图。

## 针对性验证与边界

```powershell
& .\runtime\python312\python.exe -m unittest discover -s tests -p 'test_planning*.py'
& .\runtime\python312\python.exe -m unittest discover -s tests -p test_route_review.py
```

新增测试位于 `tests/test_planning_time.py`、`tests/test_route_review.py`。
覆盖回放/现实时间分离、新鲜度边界、定位晚于回放的合法情况、照片/位姿/质量错配、
缺附件拒绝、时间缺口/未注册/质量/跳切屏障、显式批准、拆分无跨边、决定重放。
数据全部是明确标注的合成模块夹具，不是实际连续视频或机器狗验收。
用户已确认目前没有真实连续视频及独立照片等输入；不能以 South-Building 无序照片
重新编码视频或手工填 XYZ 替代真实路线验证。原有 39 图结果不在本模块改写范围。
