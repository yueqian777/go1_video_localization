# Go1 统一导航项目入口

现在两个项目已经合并到同一个视觉仓库中：

```text
go1_video_localization/
├─ src/go1loc/                         视觉定位、闭环和 NavigationRunner
├─ integrations/go1_remote_control/    PC 控制服务、模拟接收端和控制测试
├─ start_go1_navigation.cmd             默认 SHADOW 启动
├─ start_go1_simulation.cmd             离线模拟控制页面
└─ tools/run_unified_navigation_tests.ps1  一键编译并运行跨仓库模拟测试
```

## 最简单的验证方式

双击项目根目录的 `start_go1_simulation.cmd`。它会编译模拟接收端、启动只监听 `127.0.0.1` 的模拟服务，并打开 `http://127.0.0.1:8099`。模拟模式不连接 Go1、不使用 SSH、不调用 Unitree SDK。关闭模拟页面时，在控制台按 `Ctrl+C`。

## 运行自动验证

```powershell
cd "D:\文档\ChatGPT\共身智能\go1_video_localization"
& .\tools\run_unified_navigation_tests.ps1
```

它会编译模拟接收端，然后运行视觉闭环、Runner、导航权限和跨仓库 HTTP 测试。默认不会连接真实机器狗。

## 查看 SHADOW Runner

双击 `start_go1_navigation.cmd`，或执行：

```powershell
& .\tools\start_unified_navigation.ps1 -Mode SHADOW
```

SHADOW 模式只验证导航状态和动作生成，不发送控制请求。`LIVE_SUPERVISED` 需要操作者授权、新版接收端能力和有效标定证据，不会自动 TAKE 或 ARM。

## 真实 Go1

本统一入口没有自动部署和真实运动快捷方式。现场验证前仍需确认机器狗站稳、操作者在旁边、手柄可独立停止，并核验接收端反馈版本、米制尺度、重力、相机外参、地图朝向、位姿质量和时间同步。
