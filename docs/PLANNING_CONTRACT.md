# 离线历史相机路线规划契约（v1）

本模块只生成 `route_preview_only`。每个结果（包括拒绝）都有
`execution_permitted=false`；没有网络、SSH、文件写入、系统时钟读取或运动命令。
只依赖已有 NumPy 和 Python 标准库。不修改原 `LocalizationResult`，不填充旧 `nav_goal`。

## 1. 公共接口与责任边界

```python
from go1loc.planning import build_route_graph, plan_route, camera_to_base_pose

graph = build_route_graph(
    manifest, reference_poses, frames_manifest,
    component_id=None,       # 多分量地图必须明确指定
    route_segments=None,     # 独立的连续性证据；也可在内存中的 frames_manifest 中提供
    max_gap_s=2.0,
)
result = plan_route(
    graph, start, target,
    now="2026-10-13T10:00:00+00:00",  # 此处只演示纯函数；生产包装层必须读当前系统时钟
    max_start_age_s=30.0,
    disabled_edge_ids=(),
    T_BC=None,
)
```

输入输出均为 JSON 兼容的字典/列表（变换函数也接受 NumPy 数组），输入不被修改。
构图配置错误、非冻结地图、清单哈希错误或不合法刚体矩阵抛出 `ValueError`。
地图正确但缺少连续视频证据时仍可返回节点，**边为空**，原因写入 `diagnostics`。
规划中的业务拒绝返回完整 `planning_result`，而非伪路径；无效 `now` 或时间预算抛出
`ValueError`。包装层应将异常显示为输入错误，不启用备用直线规划。

纯模块检查结构和证据绑定，不自行审查原视频，也不认证调用方的声明。
上层负责从已核验的冻结地图读取对应 `reference_poses.json`、确认视频来源、审查连续性、
核验实际 PnP 目标支持；不得凭“空间很近”自动填入 `verified_continuous=true`。
图哈希用于发现意外修改，不是签名或可信来源证明。

## 2. 冻结地图、坐标与尺度

- `manifest.frozen` 必须是布尔值 `true`。
- `map_hash` 使用现有 `manifest.manifest_sha256`，**不是**缩进后的清单文件字节哈希。
  重新计算方式与 `mapping.py` 一致：移除 `manifest_sha256`，
  `json.dumps(..., sort_keys=True, ensure_ascii=False)` 后对 UTF-8 字节取 SHA-256。
  另外拒绝 NaN/Infinity。不向冻结清单加入路线字段后再沿用旧哈希。
- `reference_poses` 仍是 `{参考帧名: {image_id, T_CW: 3x4}}`。
  根据 `manifest.components[].image_ids` 选择同一分量，求逆得到 `T_MC`。
  不合并未对齐分量，不按 `image_id` 排运动顺序。
- `T_AB` 表示 B → A，M 等同原系统的 W。
- 接受尺度组合：`arbitrary_monocular_scale + map_units`，或调用方已确认的 `metric + m`。
  本模块不测量或推断米制尺度。
- 图记录清单身份、参考位姿哈希、帧清单哈希、路段证据哈希及自身 `graph_sha256`。
  不直接修改图来禁用边；调用 `plan_route(..., disabled_edge_ids=[...])`。

米制外参转换：

```python
T_MB = camera_to_base_pose(T_MC, T_BC, scale_status="metric", units="m")
# 等价于 T_MC @ inverse(T_BC)
```

T_BC 的平移必须已采用米。对旋转的正交性、行列式 +1、齐次末行、有限数进行检查。
任意尺度地图传入米制 T_BC 会拒绝 `scale_mixing_not_permitted`，不能把它加到 SfM 坐标上。
不传外参时 `start_base_pose=null`；即使已换算起点机身位姿，**整条 path 仍标记
`route_kind=camera_route`**，因为这并未计算全程机身/足步轨迹。

## 3. 历史时间路线证据

兼容现有帧记录：

```json
{
  "probe": {"source_sha256": "视频的64位小写SHA256"},
  "timing_source": "best_effort_pts_not_exposure_time",
  "frames": [
    {"image_path": "images/frame_00000000.png", "frame_index": 0,
     "pts_time_s": 1.2, "kept": true, "quality": {"rejection_reasons": []}},
    {"image_path": "images/frame_00000015.png", "frame_index": 15,
     "pts_time_s": 1.7, "kept": true, "quality": {"rejection_reasons": []}}
  ]
}
```

示意字符串需替换为真实哈希。`image_path` 仅去掉开头 `images/` 来匹配参考帧名，
不进行可能冲突的 basename 合并。其他布局可明确提供每行的 `reference_name`。
允许输入记录乱序，按真实 `pts_time_s` 排序；缺失/重复 PTS 或重复名称会阻断构边。
PTS 不是曝光绝对时刻，节点另保留可选 `captured_at`，未知保持 null。

**PTS 和视频文件本身不证明连续行走。** 还必须独立提供经过审查的路段：

```python
segments = [{
    "segment_id": "reviewed-run-01",
    "map_id": manifest["map_id"],
    "map_hash": manifest["manifest_sha256"],
    "source_kind": "video",             # 测试必须写 synthetic_test
    "source_sha256": frames_manifest["probe"]["source_sha256"],
    "verified_continuous": True,
    "evidence_id": "review-record:run-01:segment-01",
    "frame_names": ["frame_00000000.png", "frame_00000015.png"],
    "enabled": True,
}]
graph = build_route_graph(manifest, reference_poses, frames_manifest,
                          route_segments=segments)
```

这是旁文件数据；无需、也不应修改冻结地图。`evidence_id` 应能追溯到连续拍摄、
无跳切、相机确实经过该路段的审查记录。`source_kind` 只允许 `video` 或
`synthetic_test`；后者必须在演示和报告中明显标注。

构边规则：

1. 只连接路段中按采集时间前进的相邻帧；不自动生成反向边或回环捷径。
2. 两端必须有同分量注册位姿、真实 PTS、`kept=true`，且无质量拒绝原因。
3. 两端在**全部已抽样帧记录**中必须相邻，时间间隔不得超过 `max_gap_s`。
   中间失配、未注册或被过滤的帧是断点，不能删除后把两端合并成长直线。
4. 目的帧 `discontinuity_before=true` 或来源帧 `discontinuity_after=true` 均断开。
5. 每对相邻相机生成一个有向边，保留端点折线和证据；中间相机不省略，
   全路线由这些折线拼成。这里没有稀疏点云空地推断、平滑或未验证的跨帧捷径。
6. `enabled=false` 的边保留供诊断但不参与搜索。没有有效边不会退化为照片名排序。

完整图可有多个断开的区域。Dijkstra 使用历史折线长度，不做欧氏最近点吸附。
仅因相机共视、局部匹配、空间接近或物体附近有点，均不会获得通行连接。
本版不添加反向/非时间回环接口；需要新的合法采集或后续专门连接证据协议。

## 4. 起点输入与包装层字段映射

每个 `start` 都必须携带准确的 `map_id/map_hash/component_id`，不能把目标照片位姿
或地图末帧当作当前位姿。全部绝对时间采用带时区的 ISO 8601。

### 人工选中精确图节点

```python
identity = {k: graph[k] for k in ("map_id", "map_hash", "component_id")}
start = {
    **identity, "source": "explicit_pose", "manual": True,
    "node_id": "frame_00000000.png",
    "selected_at": "2026-10-13T09:59:55+00:00",
}
```

这是**人工假设当前位置就是该节点**，不是实际定位成功；结果记录 `manual_exact_node`。
可以附加 `T_MC`，但必须与选中节点的完整位姿一致（仅允许数值误差，绝非距离吸附）。
任意指定位置若未提供受支持连接不会自动接入图。人工选择时间同样检查新鲜度。

### 独立当前照片

包装层将**当前照片定位结果**转换为下列 `start`，不改写原定位契约：

| 当前照片结果字段 | 规划 start 字段 | 说明 |
| --- | --- | --- |
| `capture_time` | `captured_at` | 实际采集时间，不用定位完成时间代填 |
| `localization_time` | `localized_at` | 定位完成时间，不用运行 CLI 的时间代填 |
| `query_sha256` | `photo_sha256` | 当前照片字节哈希 |
| `T_WC_query` | `T_MC` | 同一地图坐标，模块也接受原字段别名 |
| `query_id` | `query_id` | 独立当前照片身份 |
| `pose_status`、`quality` | 同名字段 | 必须 localized，保留实际质量记录 |
| `map_id/map_hash/component_id` | 同名字段 | 必须来自当前照片的定位结果，不能盲目盖成所选图身份 |

```python
start = {
    "source": "current_photo",
    "map_id": current["map_id"], "map_hash": current["map_hash"],
    "component_id": current["component_id"],
    "query_id": current["query_id"], "photo_sha256": current["query_sha256"],
    "pose_status": current["pose_status"], "quality": current["quality"],
    "T_MC": current["T_WC_query"],
    "captured_at": current["capture_time"],
    "localized_at": current["localization_time"],
}
```

缺失采集时间不能用文件 mtime 或当前时间伪造，应交给模块拒绝或要求真实输入。
失败的定位即使仍带旧矩阵也拒绝。必须满足 `captured_at <= localized_at <= now`，
且采集距 `now` 不超过 `max_start_age_s`（默认 30 秒）。

**CLI/UI 包装层必须用当前系统 UTC 时间调用**
`plan_route(..., now=datetime.now(timezone.utc).isoformat())`。
不暴露用户可控的 `--now` 来绕过陈旧检查；不要从照片或示例配置读取 `now`。
纯函数保留显式时间输入仅用于确定性测试和标注为回放的离线实验。
系统时钟与采集时间的同步由调用方负责，不默默修正时间差。

目标结果可直接带 `query_path`、`query_dir`、`roi_binding` 和支持帧，完整保留在输出中。
模块按目标顶层 `query_id`、`query_sha256`、兼容字段 `photo_sha256`、
`roi_binding.query_sha256` 拒绝复用同一照片作当前位置。
当前 `query_id=photo.name` 时同名不同照片也会保守拒绝；应使用不同文件名，
或上层统一提供稳定且不冲突的查询 ID。不能删除身份字段来跳过该检查。

### 当前照片到图的接入证据

仅有成功 PnP，即使位置与节点完全重合，也不会自动建立通路。
可附带经上层审查的**定向连续相机轨迹**：

```python
start["attachment"] = {
    **{k: start[k] for k in ("map_id", "map_hash", "component_id", "query_id",
                            "photo_sha256", "captured_at", "localized_at")},
    "kind": "verified_temporal_connection", "verified_continuous": True,
    "source_kind": "video", "source_sha256": "实际接入视频的64位小写SHA256",
    "evidence_id": "review-record:current-to-node",
    "node_id": "frame_00000000.png", "units": graph["units"],
    "path": [[-1, 0, 0], [-1, 1, 0], [0, 0, 0]],  # 仅结构示意，不是现场数据
    "pts_time_s": [0, .25, .5],
}
```

`path` 必须已经在同一地图/分量/尺度下，对应真实连续轨迹采样，不允许自行连接直线。
至少两个有限三维点，逐点 PTS 数量相同、严格递增、间隔不超过 `graph.max_gap_s`。
首点必须等于本次当前照片的相机位置，末点必须等于明确节点位置；该等值检查不是
连接证据，只是用于排除错配。证据必须绑定本次照片、两种时间和地图身份。
上层要检查实际轨迹的方向、来源、坐标对齐和完整性，本模块不会从字符串读取视频。
拒绝时返回 `start_not_connected`，不显示未经支持的接入线。

## 5. 目标观察位置与 planning_result

上层真实 PnP/ROI 支持链传入：

```python
target = {
    **identity,
    "status": "supported",  # 也兼容 object_status；零簇/多簇均不能规划
    "supporting_frame_ids": ["frame_00000015.png"],
    "query_id": "target-photo.jpg", "query_sha256": "真实目标照片SHA256",
    # 原对象结果中的其他字段（支持点/表面位置/时刻/ROI绑定）原样保留
}
```

观察终点仅选实际支持帧在本分量中的相机位姿，不使用物体表面位置作步行终点。
先过滤不可达候选，再选总路线长度最短者。等长时采用可选
`observation_quality={frame_name: 实际支持数量或测得质量分数}`，较高者优先；
无质量记录保持 null，不捏造成功概率。再次同分按帧名确定性排序。

结果包含：

- 地图身份、`graph_sha256`、`map_observation_time`、`planned_at`；
- `start_source`、`start_times`、实际 `start_quality`、`start_camera_pose`、`start_base_pose`；
- `target_object_result` 完整副本、`goal_candidates`、`goal_frame_id`、`goal_observation_pose`；
- `route_node_ids`、`route_edge_ids`（图内边）、`path`（含已验证接入折线）、`length`、`units`；
- `geometry_provenance`、`connectivity_provenance`（起点接入、逐边证据、Dijkstra）、
  `provenance_kinds`（包括测试来源）与警告；
- `status=route_preview_only` 或 `rejected`，独立布尔 `route_preview_only=true`，
  `failure_reason` 和固定 `execution_permitted=false`。

`goal_candidates[].route_length` 是图内距离，结果 `length` 还包括起点接入长度。
无路/起点无法接入时 `path=[]`、节点和边序列为空、`length=null`、目标位姿 null。
已在支持目标的人工节点可产生单点、零长度预览，不捏造额外移动。

主要拒绝原因：`map_id_mismatch`、`map_hash_mismatch`、`component_id_mismatch`、
`target_not_supported`、`goal_not_supported`、`start_localization_failed`、`stale_start`、
`invalid_start_time`、`start_not_connected`、`target_photo_is_not_current_pose`、
`scale_mixing_not_permitted`、`unknown_disabled_edge`、`invalid_graph`、`no_path`。

历史路线只反映过去的相机经过位置；不保证现在无障碍、机身足迹可通过或物体仍在那里。
没有导航占据地图就不开展自由空间规划；不从稀疏点云缺点区域推断 free。
本版 `live_camera` 禁用，不执行机器人连接。

## 6. 测试与明确标注的合成演示配方

在项目根目录运行（全部离线）：

```powershell
& .\runtime\python312\python.exe -m unittest discover -s tests -p test_planning.py -v
$env:COVERAGE_FILE = Join-Path (Get-Location).Path 'results/.coverage-planning'
& .\runtime\python312\python.exe tools/run_coverage.py tests/test_planning.py
& .\runtime\python312\python.exe -m coverage report -m
```

便携 Python 将标准库打包为 zip/pyc；使用公共Coverage API包装器保留原生扩展搜索路径，
不使用 `coverage run -m unittest` 或直接 `coverage run`。包装器当前统计行覆盖，不冒称分支覆盖。

可运行 `tools/demo_route_fixture.py --output results/new_route_fixture` 保存明确标注来源的演示，
也可从测试夹具生成**只在模块层有效的 synthetic_test**样例：

```python
from copy import deepcopy
from pathlib import Path
import sys
# 便携 Python 的隔离模式不自动添加工作目录；请先在项目根目录运行。
sys.path.insert(0, str(Path.cwd()))
from tests.test_planning import fixture, inputs, NOW
from go1loc.planning import build_route_graph, plan_route

manifest, references, frames = fixture()
graph = build_route_graph(manifest, references, frames)
start, target = inputs(graph)
success = plan_route(graph, start, target, now=NOW)
disconnected_target = deepcopy(target)
disconnected_target["supporting_frame_ids"] = ["wall.png"]
no_path = plan_route(graph, start, disconnected_target, now=NOW)
# success.path = [[0,0,0], [0,2,0], [2,2,0]], length=4 map_units
# no_path.failure_reason = "no_path", path=[]
payload = {"fixture_kind": "synthetic_test_not_go1_acceptance",
           "success": success, "disconnected": no_path}
```

父层如需保存，输出到 Git 忽略的 `results/` 旁目录，不写冻结地图、不上传测试数据。
上面的 `NOW` **仅为合成夹具时间**，不能传给真实 CLI 规避陈旧定位。
测试覆盖 T_CW 求逆、米制外参次序、尺度混用、无时间来源、时间断裂、跳切、缺失注册、
隔墙近邻、单向/禁用边、断图、多观察终点、目标不足/歧义、起点新鲜度、接入绑定及不修改输入。
South-Building 无运动序列连续性依据时只可显示点云/相机，不能作为 Go1 路线验收。
真实 Go1 视频、现场连续路段审查和当前障碍状态均需后续单独验证。
