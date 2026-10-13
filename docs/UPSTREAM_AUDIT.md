# 上游实现审计

本项目没有重新发明 SfM 或局部匹配。已在 `.upstream/` 固定并读取：

- hloc：`c13273bd0ecc2917a35910fd843712a1c6243193`（当前主分支快照）
- LightGlue：`eb42fee2d71449efb0aa5c10549752b5d75384d8`
- hloc 的 `README.md`、`demo.ipynb`、`pipeline_SfM.ipynb`
- `extract_features.py`、`match_features.py`、`reconstruction.py`、`localize_sfm.py`
- LightGlue 的 `README.md`、`demo.ipynb`

固定的数据链为：真实视频解码/抽帧 →（有认证标定时）鱼眼校正 → hloc 特征和检索 → SuperPoint + LightGlue → pycolmap/COLMAP 稀疏模型 → 独立查询图像的 2D-3D PnP。原 hloc 在 PnP 无结果时的最近参考图回退在本项目中禁止使用。

COLMAP 坐标约定也被显式写进 `src/go1loc/geometry.py`：COLMAP 保存的是 world-to-camera，查询结果转换成 camera-to-world；默认单目尺度是不确定的，不写成米，不写入导航目标。
