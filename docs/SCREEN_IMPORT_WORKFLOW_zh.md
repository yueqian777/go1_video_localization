# Go1 单路录屏导入：小范围增量

适用：真实 Go1 画面经显示界面录制的 MP4。它不是原始相机码流，也不是合成测试。
保留 hloc / SuperPoint / LightGlue / PyCOLMAP；不连接 Go1，不包含运动执行。

## 1. 先检查原尺寸，再确认单路 ROI

在项目根目录运行（Windows 使用已有 D 盘 Python，无需重装）：

```powershell
$py = '.\runtime\python312\python.exe'
& $py -m go1loc.cli inspect-screen 'D:\你的素材\录屏.mp4' --output results/my-screen-inspection
```

打开输出 `native_frame.png`，按它的实际宽、高确认固定相机矩形，坐标原点左上。
这一步是原尺寸解码，不按聊天截图尺寸选点，不根据窗口排布推断物理 camera_id。
命令同时输出 `probe.json` 的源文件 SHA-256、视频尺寸、time_base 和时长。
`selection_required=true`；工具不会自动认定全幅或某个子窗口已获确认。

将 `configs/screen_652x648_example.json` 复制到本次本地结果目录后修改：

- `source_size_wh`：实测解码尺寸；导入时重新核验。
- `crop_xywh`：用户确认的固定单路区域，**原始录屏像素**坐标。
- `output_size_wh`：默认保持 ROI 尺寸；不必缩放。当前示例为 652×648，不适用于其他布局。
- `selected_window`：人工描述所选视图；`camera_label` 可记录用户称谓，不能代替已核实的 `camera_id`。
- `expected_source_sha256`：建议填入本次 probe 的实际哈希，避免误用其他录屏的选择配置。
- `exclude_rectangles`：原始录屏坐标下的十字线、图标、鼠标或固定遮挡区域。
- `margin_px`：在原始录屏坐标中扩张排除区；`border_px`：排除裁剪边框。
- `display_mode`：`unknown` / `raw_fisheye` / `already_rectified` / `display_reprojected`。
  除 unknown 外必须提供 `display_evidence`。不知道界面内部投影时，即使用户没额外编辑，也仍填 unknown。
- `calibration_status`：本轮仅支持 unknown，不伪造内参；录屏入口拒绝额外 `--calibration`。

优先在采集界面关闭叠加、避免鼠标进入和改变布局。掩膜不是去水印，也不会恢复被遮挡的场景。
动态鼠标、机身或其他遮挡无法可靠自动分割；须检查诊断帧，扩大固定排除区、分段重新配置或重录。

## 2. 导入前30秒，不重新编码视频

```powershell
& $py -m go1loc.cli prepare-video 'D:\你的素材\录屏.mp4' `
  --output results/my-screen-30s --screen-config results/my-screen-config.json
```

默认30秒、0.5秒采样、最多80帧。可用原有 `--config` 传入 FrameConfig JSON。
例如仅做匹配诊断，显式保留低清晰度候选：

```json
{"duration_s":30,"interval_s":0.5,"max_frames":60,"quality_filter":false}
```

关闭筛选**不代表质量通过**：逐帧指标和警告依然保留，kept 仅表示进入后续特征流程。
不建议为了获得更多注册帧直接降低阈值并宣称验收成功。
输出目录必须新建且为空，不覆盖旧结果。默认不产出派生视频，直接解码、裁剪为无损 PNG。
音频不读取、不保存、不分析。

输出目录：

| 文件 | 含义 |
|---|---|
| `screen_config.json` | 本次固定 ROI、显示模式、相机称谓、排除区 |
| `feature_mask.png` | 255可用／0排除；已计入边框和排除区扩张 |
| `raw/screen_*.png` | 被选关键帧的原尺寸解码图；非视频原件替代品 |
| `images/frame_*.png` | 供现有建图入口使用的裁剪 PNG；不涂黑 |
| `source_timeline.json` | 窗口内**每一解码帧**的原始 PTS、源索引、质量及派生关系 |
| `frames_manifest.json` | 原视频哈希→解码 PNG 哈希→裁剪 PNG 哈希、几何、时间和掩膜绑定 |
| `quality_report.json` | 场景区域质量统计、逐帧警告、待人工核实事项 |
| `diagnostics/valid_area_*.png` | 仅用于显示的红色排除区预览，不用于特征提取 |
| `decoder.log`、相邻 sidecar | 解码日志、运行事件 |

源索引是 ffmpeg 解码显示顺序，PTS 来自 ffprobe，不用 `index/fps` 伪造。
`screen_display` 只表示录屏显示时间；`captured_at` 和传感器采集时间保持 null，
`hardware_synchronized=false`。导入时间另存为 `imported_at`，不能替代拍摄时间。
裁剪和缩放用零基像素中心映射；输出保存正反3×3变换，可将 ROI 映射回原始录屏。
本轮不做额外非线性矫正，未知投影下算法估计的焦距仅是近似重建参数，不是实测标定。

## 3. 质量与真实特征掩膜

相似度、Laplacian、直方图、过曝/暗场指标只在有效场景区域计算，求导前再腐蚀有效区以避开 UI 边缘。
持续相似只记为“静止或疑似冻结”，**不自动判丢帧，不因此删帧**。
强跳变记为疑似场景/布局变化；模糊、低纹理、遮挡候选不能直接当作原因诊断。
这些是启发式提示，不能保证发现鼠标、机身遮挡或所有窗口布局变化；质量报告明确保留人工检查项。
缺口、疑似跳切及质量拒绝会传播到下一关键帧的 `discontinuity_before` / `interval_review`，
即使异常发生在两个选中关键帧之间也不会被漏过后直接连边。
不会删掉时间间隔、填补画面或自动输出 verified 路段。

外部特征实际处理顺序：

```text
未涂黑的 PNG → hloc SuperPoint → 按原尺寸关键点坐标过滤
→ 同步过滤 keypoints / descriptors / scores（及支持的 scales / oris）
→ LightGlue → COLMAP 导入
```

特征 HDF5 先完整验证和过滤到临时副本，再原子替换。保持属性，拒绝未知数组对齐或重复过滤。
索引映射单独存入 JSON，不向匹配器塞额外数组。不会在匹配生成后再删除关键点。
建图和查询共用这段过滤逻辑；不是只给 COLMAP 一个 mask 参数。
诊断图绿色为保留点、红色为移除点，`feature_filter.json` 有原索引映射及数量。

```powershell
& $py -m go1loc.cli diagnose-screen --import-dir results/my-screen-30s `
  --output results/my-screen-matching --pairs 4
```

实际运行现有 SuperPoint 和 LightGlue，最多8组相邻帧对。
`match_diagnostics.json` 会检查索引有效和排除区域无关键点，并输出匹配图。
基础矩阵一致性只是未知投影下的辅助指标，不是 PnP 定位精度或路线成功率。

## 4. 小样例探索性建图与后续查询

检查质量后才运行；若未通过默认质量门槛，必须明确记录为探索性运行。

```powershell
& $py -m go1loc.cli build-map --images results/my-screen-30s/images `
  --output results/my-screen-map --map-id go1-screen-exploratory `
  --snapshot-frames-freq 8 --sidecar results/my-screen-build-run

# 在 sidecar 下找到本次 <run_id>/run.json 后另开终端；不需要最终 map_manifest：
& $py -m go1loc.cli watch-build --run results/my-screen-build-run/<run_id> `
  --sidecar results/my-screen-watch --port 8766
```

`watch-build`、原生 READY 快照、地图冻结行为沿用既有实现。
屏幕源明确记为 `go1_screen_recording`；路线审核允许这一新来源，但不能伪装为 raw video。
成功重建后仍需观看实际连续片段、确认/拆分/拒绝候选路段。匹配边和共视边不是可通行路线。
只做相机观察位姿的离线预览，无尺度时仍为 `map_units`，`execution_permitted=false`。

独立查询必须另录一次、另建导入目录，并针对其实际窗口和叠加重新核验掩膜：

```powershell
& $py -m go1loc.cli localize-photo --photo results/independent-query/images/frame_00000000.png `
  --map results/my-screen-map --output results/query-localization

# 查询 PNG 复制或改名后，显式指出它原来导入目录的清单：
& $py -m go1loc.cli locate-photo-object --photo 'D:\查询\目标.png' `
  --frames-manifest results/independent-query/frames_manifest.json `
  --map results/my-screen-map --roi 100 100 300 300 --output results/query-object
```

ROI 是**派生查询 PNG 的原尺寸像素**，不是显示缩略图像素。
独立来源由原录屏哈希及 PNG/PTS/掩膜绑定核验；同一录屏中的其他帧也不能冒充独立采集。
移动后的相同 PNG 若对应多个重复帧且无法确定 PTS，会明确拒绝，不猜时间。
当前小范围实现要求屏幕地图查询提供可核验的录屏派生清单，不会静默接受未知来源的无掩膜图片。
未提供独立录屏时，不能进行真实照片定位/目标路线验收。

未知传感器拍摄时间的录屏查询可做照片定位，但不能通过 `--capture-time` 填“现在”。
当前照片路线时效门禁继续拒绝缺时间证据；offline_replay 也不能制造缺失的采集时间。
第一阶段可使用人工明确的历史图节点作为起点，但目标照片仍须独立采集。

## 5. 发布边界

仅提交源码、测试、通用配置和无敏感信息的摘要。原录屏、派生 PNG、HDF5、模型、完整本地日志在 ignored 的 data/results 中。
本轮不改变旧地图和查询目录，不升级主视觉环境，不进行多相机融合、自由空间避障或机器人执行。
