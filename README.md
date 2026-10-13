# go1_video_localization

基于 Go1 运动视频建图的照片目标定位系统。项目运行在外部计算机，默认只处理本地文件，不连接机器狗，不修改现有 `go1_hri` 控制栈。

## 当前实现

- `go1loc prepare-video`：对真实视频做 SHA-256、ffprobe、基于真实 PTS 的有界解码、模糊/过曝/疑似冗余筛选；有认证标定时保存非线性去畸变映射和有效区域。
- `go1loc build-map`：依赖安装后调用固定版本 hloc + SuperPoint + LightGlue + pycolmap 建立真实稀疏地图。
- `go1loc build-map --max-keypoints N`：为显存受限设备设置 SuperPoint 特征点上限；默认仍为 4096，公开样例实测使用 2048。
- `go1loc localize-photo`：对独立查询照片做检索、局部匹配、2D-3D PnP 和严格质量门禁；不会在失败时回退最近参考位姿。
- `go1loc locate-object`：接收人工选择的 ROI 和实际 2D-3D 支撑，去重、空间聚类并对多簇输出歧义。
- `go1loc view`：仅监听 `127.0.0.1` 的本地结果查看器。
- `go1loc doctor`：只读检查本机工具、Python 包、GPU 和固定上游版本。
- `go1loc evaluate`：保留所有查询（包括失败和拒绝）并汇总状态。

## 开发环境

### GitHub 发布范围

本仓库发布当前研究原型的源码、配置、测试和文档，不包含 Python 运行环境、模型权重、第三方仓库副本、数据集、现场记录或本机生成结果。下文 `results/` 路径描述的是本机实测产物，不是随仓库分发的文件。

新克隆的仓库不会自带 `runtime/python312` 或 `.upstream`；现有 PowerShell 安装脚本要求先准备这些目录。可在项目所在盘创建独立 Python 3.12 环境，将 hloc 和 LightGlue 克隆到 `.upstream/`，按 `environment-lock.json` 中的提交号检出，再安装 `requirements-core.txt`、`requirements-pipeline.txt`、两个上游包和本项目。GPU 版 PyTorch 需要按硬件单独选择；不要把本机的 CUDA/驱动记录当成其他设备的安装要求。

第三方代码和权重分别遵循各自许可，本仓库不替它们重新授权：

- [Hierarchical-Localization](https://github.com/cvg/Hierarchical-Localization)
- [LightGlue](https://github.com/cvg/LightGlue)
- [South-Building 公开样本来源](https://cvg-data.inf.ethz.ch/local-feature-evaluation-schoenberger2017/)

### 已验证的本机环境

所有新环境、wheel 缓存、模型权重和公开数据放在本项目 D 盘目录下。当前可复用的视觉运行时是 `runtime/python312`，不是系统 Python：

```powershell
& '.\runtime\python312\python.exe' -m pip check
& '.\runtime\python312\Scripts\go1loc.exe' doctor --project-root . --output results/doctor.json
& '.\runtime\python312\python.exe' tests/run_all.py
& powershell -ExecutionPolicy Bypass -File tools/install_pipeline.ps1
```

已验证的组合是 Python 3.12.10、PyTorch 2.9.0+cu130、torchvision 0.24.0+cu130、PyCOLMAP 4.2.1；`nvidia-smi` 识别为 RTX 5070 Ti Laptop GPU。CUDA 驱动和 Python 视觉包是两层不同东西，NetVLAD/LightGlue 权重仍需单独缓存。运行时会把 `TORCH_HOME` 默认指向项目下的 `.models/torch`，不应把新模型写入 C 盘用户缓存。安装脚本把 hloc/LightGlue 从 `.upstream/` 固定提交以避免 Windows pip 重复解析 Git 依赖。

首次验证可使用不超过 `exhaustive_limit` 的小地图，跳过 529 MB NetVLAD；超过该规模才启用 NetVLAD 检索。

公开 South-Building 实测已在 `results/public_south_map40_lg_k2048` 生成冻结地图：40/40 图像注册、10,394 个 3D 点、平均重投影误差 1.316 px。地图内查询 `P1180170.JPG` 通过定位质量门禁；未参与建图的 `P1180181.JPG` 和 `P1180182.JPG` 被严格拒绝，分别记录 0.2096 和 0.1381 的内点率。该结果只证明公开建筑样例上的软件链路，不等同于 Go1 相机或点云质量验收。

`view` 页面支持本地图片上传和鼠标框选 ROI，并可下载 ROI JSON；这只是人工选择工具，不会自动猜测目标三维位置。

## 研究边界

单目地图默认是任意尺度。没有外部测量误差与版本记录时，输出单位是 `map_units`，`nav_goal` 恒为 `null`。详细上游核对、数据合同和未验证项见 `docs/`。

当前查看器不是实时三维建图界面；目标模块需要人工 ROI 和实际 2D-3D 对应关系，尚未串联自动物体识别。当前代码没有可通行地图、路径规划、实时避障或机器狗导航闭环。地图内查询成功不代表独立查询精度已通过验证；留出图失败原因仍需进一步分析，不能仅凭内点率认定是地图覆盖不足。
