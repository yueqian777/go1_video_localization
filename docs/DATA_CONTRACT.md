# 数据与输出合同

## Go1 输入边界

历史现场记录只作为适配说明：相机共享内存读出的图像为 464×400 BGR 鱼眼帧，五路不是深度图；跨板时钟和已有工厂点云不作为本项目 SfM 真值。当前默认捕获适配器是本地视频，`go1_readonly_shm` 明确拒绝运行，避免离线工具意外连接或修改机器狗。

## 标定

必须提供带来源、模型、原始 K、畸变参数、输出 K、输出尺寸和 `map_version` 的 JSON。`unknown`、`invented`、`placeholder` 来源会被拒绝。未经 Go1 标定的数据可以作为公开样例，但必须标成非 Go1 校准。

## 定位结果

结果至少包含 `query_id`、`map_id`、`component_id`、`mode`、`pose_status`、`object_status`、`T_WC_query`、`object_surface_position`、`object_extent`、支持帧/点、质量、单位、尺度状态、地图观测时间、失败原因和计时。任何未通过门禁的几何字段为 `null`，不使用最近参考位姿填充。

## 目标

目标位置表示可见表面点支撑，不等于物体体积中心、真实包围盒中心或机器人当前导航点。多个空间簇会输出 `ambiguous` 和候选簇，不能取簇间平均。
