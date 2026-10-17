# Go1 网络相机预览与手动录像

用 `capture-ui` 启动独立采集界面，手动连接一路相机并预览、录制或抓拍。
源数据保存为可逆 BGR → RGB 的 PNG；可导出经回读检查的 FFV1 MKV，或直接导入现有建图流程。
该模块没有运动接口。停止录制不是机器人急停。

## 先在电脑演练

在本仓库 Windows 现有环境中运行：

```powershell
& .\runtime\python312\python.exe -m go1loc.cli capture-ui --synthetic --port 8767
```

打开终端显示的 `http://127.0.0.1:8767`，勾选确认后点击“连接所选相机”。
画面为明确标注的合成色条，不是 Go1 图像。预览不会自行录像；点击“开始录制”后才保存。
“保存下一张源图”等待新到达的有效帧，超时会报错，不使用旧缓存冒充新照片。
录像期间也可抓拍。先停止录像，再点“导出最近录像 FFV1 MKV”。

Ubuntu 或其他外部电脑可用独立 Python 3.10+ 环境：

```bash
python3 -m venv .venv-capture
source .venv-capture/bin/activate
python -m pip install -e '.[capture]'
python -m go1loc.cli capture-ui --synthetic
```

电脑需有 OpenSSH 客户端；导出视频需 PATH 中的 `ffmpeg` 和 `ffprobe`。
仅预览/PNG 录像不需要 FFmpeg，也不需要 PyTorch、hloc、LightGlue、COLMAP 或地图。
建图仍使用项目原有已配置的视觉环境，不在板端安装这些依赖。
本轮本机验证环境与限制见验证报告；Ubuntu 和 Nano 实际运行要另行验收。

## 图像入口与不可做的操作

当前选择一条实现路线：Jetson 已有 SysV SHM → 只读代理 → SSH 二进制 stdout → 电脑。
没有实现 RTSP，也没有复用或抢占原查看器 UDP 端口。离线资料不能确定当前五视窗的实际取流方式；现场应先确认其启动命令、连接目标及原厂配置。

依据工作区已有传感器手册及 `modules-03/commands/shm-camera13.command.txt` 实测方法：

| 相机 | 历史板卡地址 | SHM key |
|---|---|---|
| 1 前方 | 192.168.123.13 | 9001 |
| 2 下巴 | 192.168.123.13 | 9002 |
| 3 左侧 | 192.168.123.14 | 9003 |
| 4 右侧 | 192.168.123.14 | 9004 |
| 5 后下 | 192.168.123.15 | 9005 |

历史布局是 556804 字节：4 字节未解释标志 + 464×400×3 BGR，形状 `(400,464,3)`。
这些是配置候选，不是对当前设备的确认。代理核验段存在、UID、字节大小及附加前后身份；
字节大小匹配不能单独证明颜色、朝向或投影正确，须以现场源图与原厂配置复核。
采样期间每秒复核一次段身份；生产者删除/替换段后停止流，不把旧附加段继续当作有效来源。
默认所有者是登录用户 UID，可配置已核验的 `owner_uid`，不使用 sudo 绕过错误。

代理只调用 `shmat(..., SHM_RDONLY)` 与 `shmdt`，不创建同 key 段、不写标志、
不更改权限、不执行 `ipcrm`，也不打开 `/dev/video*`。双次复制相同才发送，仍不是原子快照保证。
若上游已经绘制十字线，本模块会忠实保留；不会通过截图、涂抹或 AI 去除。

官方 [接收示例](https://github.com/unitreerobotics/UnitreecameraSDK/blob/master/examples/example_getimagetrans.cc)
与[发送示例](https://github.com/unitreerobotics/UnitreecameraSDK/blob/master/examples/example_putImagetrans.cc)
使用定向 RTP/H.264 over UDP，示例端口 9201–9205 不证明本机已启用。
其中 `omxh264dec` 不是 Windows 通用解码器，SSH `-L` 也不能直接转发 UDP。
不要执行发送示例的 kill 脚本，不停止原厂相机/AI 服务来为本模块让路。

## 用户明确授权后再部署

本节是供现场人员主动执行的步骤，启动 UI 不会执行它们。
首次只部署前视所在 `.13`，不要一次连接所有板卡。历史跳板为 `pi@192.168.12.1`，实际地址以现场为准。

1. 通过可信渠道核对 Pi 和目标 Jetson 的 SSH 主机公钥指纹，再建立正常 SSH 连接。
   不使用 `StrictHostKeyChecking=no`、空 known_hosts 或自动接受未知密钥。
2. 在外部电脑预先配置 SSH config、已有公钥或 ssh-agent。UI 使用 `BatchMode=yes`，
   不弹密码对话框、不接收密码字段，也不保存私钥。若只能用密码，先在终端完成合适的 SSH 认证配置。
   不要把凭据、SSH 配置或私钥放进仓库。
3. 在目标板只读核对 `/usr/bin/python3 --version`、`id -u`、`cat /proc/sysvipc/shm`，
   以及相关原厂配置中的宽高、颜色布局、SHM key 和投影。代理仅用 Python 3.6+ 标准库；
   如果板上版本/布局不同，停止并适配，不升级原厂 Python/OS/CUDA。
4. 核验后复制两个文件到一个**不存在的新目录**。以下示例不会创建服务或开机自启：

```bash
# 在电脑终端执行；目录存在则 mkdir 失败，应换一个新版本目录，不覆盖旧代理。
ssh -o StrictHostKeyChecking=yes -J pi@192.168.12.1 unitree@192.168.123.13 'mkdir -m 700 /home/unitree/go1_capture_20261015'
scp -o StrictHostKeyChecking=yes -J pi@192.168.12.1 src/go1loc/capture_agent.py src/go1loc/capture_wire.py unitree@192.168.123.13:/home/unitree/go1_capture_20261015/
```

只在上一条 mkdir 成功后执行 scp。Windows 可在 PowerShell 运行同样的 OpenSSH 命令。
比较本地两个文件的 SHA-256 与板端 `sha256sum` 结果；不要把整个研究环境复制到 Nano。
本模块不自动修改 authorized_keys、系统配置或原厂启动脚本。

复制 `configs/capture_go1.example.json` 到自己不提交的本地配置文件，核对 `target`、`jump`、
`remote_agent`、`remote_python`、尺寸、UID 和预算。已有直连条件时设置 `jump: null`。
`target` 可为 SSH host alias；`identity_file`、`ssh_config` 仅引用本机已有文件路径。
`projection` 默认 `unknown`；只有实际确认后才写 `raw_fisheye` 或 `rectified_pinhole`。

```bash
python -m go1loc.cli capture-ui --config /path/to/my-go1-capture.json --output /path/to/captures
```

勾选“确认只读连接”，点击连接，才会启动一次 `ssh -T` 二进制会话。
未知主机、认证失败、代理缺失、段不存在、UID/大小不符都会失败，不回退其他图像来源。
错误日志不打印凭据；SSH stderr 持续排空，仅在内存保留最多 8 KiB 尾部。
排障时在自己的终端验证正常 SSH 登录，不把含凭据的调试日志上传。

## 状态与资源预算

`disconnected → connecting → previewing → recording → finalizing → previewing`。
断流、超时或格式变化进入 `error` 并隐藏旧预览；当前片段标为 `interrupted`。
界面的 `live` 表示持续收到有效包，不证明相机正在产生新的独立曝光；静止场景与上游冻结不能仅靠像素相同区分。
先断开再重新连接；重连只恢复预览，开始新录像仍须点击按钮。
换相机必须先断开，不会把不同相机、格式、投影或流会话写进同一片段。
默认监听回环地址，不提供公网服务或共享链接。

默认一路 10 次/秒读取、120 秒代理寿命、板端队列 2 帧、电脑写盘队列 8 帧。
可配置读取 0.2–20 次/秒、代理 1–600 秒、板端队列 1–8 帧、预计带宽预算 1–200 Mbps。
这不是曝光帧率或实时性能承诺。464×400 BGR 的 10 次/秒像素量约 44.54 Mbps，另有协议/SSH开销。
`max_mbps` 是连接前的预算检查，不是网络 QoS。超预算会拒绝连接；实际慢网时队列有界丢帧并记录计数/序号缺口。
不自动缩小图片、不改为 JPEG、不重复末帧补帧；SSH 压缩明确关闭。

图像读取、网络接收、PNG 写盘和显示分线程。显示只取最新副本；显示变慢不会增加录像队列。
每次只允许一个 UI 操作和一个录像器，写盘故障保留已完成 PNG。
收尾等待有上限；未完成或磁盘满导致元数据无法收尾时保留 `INCOMPLETE`，禁止正常导入。
不要删除此标记冒充完整录像，应先复制会话再进行人工恢复审计。
清理仅涉及本程序拥有的 SSH 子进程、线程与 SHM 附加，不查杀其他 Python 或相机进程。

## 录像文件与时间含义

每次开始自动生成新目录，不覆盖已有目录：

```text
会话名_时间_随机标识/
  session.json
  frames.jsonl
  frames/00000000.png ...
  events.jsonl
  quality_summary.json
  calibration_reference.json
  exports/唯一标识/source.mkv           # 用户请求导出后才生成
  exports/唯一标识/verification.json
```

PNG 是权威数据。抓拍另建 `*_photo_*` 目录，保存 `source.png` 与 `source.json`。
快照/录像中的源图不含本 UI 的文字、框线和控件；不对源图做去水印或修复。
开始前的预览缓存不补写进录像，停止后到达的图像不加入已结束片段。

| 字段 | 含义 |
|---|---|
| `sensor_capture_time` | 始终 null，未取得可靠曝光时间 |
| `agent_read_monotonic` | 板端单调读取时刻，排序与录像相对时间依据 |
| `agent_read_wall_time` | 板端 Unix wall time；`agent_wall_time_trust=unverified` |
| `pc_receive_time` / `pc_receive_monotonic` | 电脑收到完整包并通过校验的时刻 |
| `agent_seq` | 读取尝试序号，不是曝光帧号 |
| `session_id` | 每次代理启动生成的新流会话 UUID |
| `recording_id` / `saved_index` | 每个录制片段 UUID 与成功写入顺序 |
| `original_stream_pts` | SHM 未提供，null |

两台主机的 wall clock 不可信时，不计算单向延迟。相同像素仅作统计，不自动判为重复曝光或冻结。
序号缺口可能来自复制不一致或队列丢弃，不代表确定丢了多少次相机曝光。
`events.jsonl` 保留开始、读取序号缺口及收尾丢弃计数，帧清单保留板端计数与资源观测。
`quality_summary.json` 的读取速率不是曝光 FPS，也不是画质验收结果。

二进制协议：12 字节网络字节序前缀 `!4sII` 为 `G1C1`、JSON 头长度、头 CRC32；
随后 UTF-8 元数据和原始 packed BGR8 字节。头不超过 8192 字节，payload 不超过 8 MiB。
元数据含版本、消息类型、相机、尺寸、通道、stride、payload 长度/CRC32、序号、时间和会话。
接收方先校验尺寸/格式/长度上限再分配 payload，支持短读、连续包、EOF 和半包报错。
CRC32 只检查损坏，SSH 才提供认证与加密。代理日志走 stderr，不与二进制 stdout 混写。

## 导出与建图

```bash
python -m go1loc.cli export-capture /path/to/session
python -m go1loc.cli prepare-capture /path/to/session --output data/processed/capture01
python -m go1loc.cli build-map --images data/processed/capture01/images --output results/capture01_map --max-keypoints 2048
```

FFV1 输出使用 `bgr0`，不经过 YUV420。完成前检查全部视频帧数/时间戳，并逐像素比较首、中、末帧。
时间是代理读取相对时间，MKV 量化到毫秒；不插帧、不复制末帧，最后一帧没有已知曝光持续时间。
源 PNG 永不因为导出失败而删除。MKV 是播放/交换副本；建图优先直接 `prepare-capture` 原会话，避免丢失来源语义。

`prepare-capture` 复用已有 `FrameConfig` 时间选帧和画质检查，生成 `go1loc.frames.v1`，
再进入原有 temporal-window/hloc/LightGlue/PyCOLMAP 流程。
旧字段 `pts_time_s` 在此兼容填入代理单调读取值，且明确标 `agent_read_monotonic_not_exposure`；
它不是原生码流 PTS。清单保存完整原始元数据和 PNG 哈希，拒绝错序、混相机、格式变化与文件篡改。
离线选帧丢弃与实时丢帧分开记录；即使缺口位于未选中帧之间，也向建图帧传播 `discontinuity_before`。
`route_segments=[]`，不自动批准连续路段，不生成可执行运动；未知曝光时间不会被填成现在以通过规划门禁。

来源区分 `go1_shm_readonly`、旧 `go1_screen_recording/screen_display`、`synthetic_test`。
`go1_native_network` 仅预留来源枚举，**没有声称已实现原生 H.264 接入**。
原录屏导入、掩膜、结果和报告均保留，新直接采集不自动套旧十字线掩膜。

原鱼眼图需要相机/尺寸/投影匹配的标定。可给 `prepare-capture --calibration ...` 传现有标定格式，
并额外包含 `capture_camera_id` 与 `capture_projection=raw_fisheye`，且会话须已现场确认为同一投影。
未知投影、已矫正图、相机或分辨率不符会拒绝自动去畸变，防止套用录屏裁剪内参或二次矫正。
没有标定时明确保留 uncalibrated，不伪造实测内参。
原质量阈值 20 仍只是可配置启发式，不是跨分辨率真值；质量不通过时保留报告，不静默降低标准。

中断片段默认拒绝导入/导出；人工检查后可显式使用 `--allow-interrupted`，并保留中断来源与路线缺口。
此参数不能恢复丢失数据，也不能把中断片段变成连续运动证据。

## 现场验收顺序

1. 狗静止，记录原厂服务状态、板端与 Pi 的 CPU/内存/网络基线。只读仍有资源消耗。
2. 核对真实五视窗入口；从前视一路抓取 PNG，与同场景查看器比较，确认十字线是否仅在显示层。
3. 核对尺寸、BGR/RGB、朝向、单双目内容、是否鱼眼/已重投影。布局不符立即停止，不盲目改段。
4. 一路预览和录像 1–2 分钟；记录实际接收/保存数量、复制不稳定与队列缺口、代理 CPU/maxrss、Pi 转发负载及原服务状态。
5. 测试开始、停止、新帧抓拍、主动断开、断网和重连不续录。之后才逐路验证其他相机。
6. 移动采集由现场人员使用已有控制方式完成，本模块不参与运动控制。
7. 对新的真实采集运行 `prepare-capture` 和现有 `build-map`。同场景/相近位置/相同处理条件比较画质与特征效果，拉普拉斯方差只作辅助。

只有完成这些步骤，才能确认某只狗的当前环境可用。离线测试、公开样本重建和浏览器色条演练不能代替实机验收。
