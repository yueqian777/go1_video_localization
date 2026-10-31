# Go1：电脑可视化控制端＋机器狗接收端

当前 v0.3.4（2026-10-29）：在 v0.3.3 的停止原因和指令间隔诊断基础上，增加与现有 `RobotLink` 共用的导航任务租约、command_id、单轴动作接口和人工 STOP 撤销。导航不会另开 SDK/UDP 端口，也不会自动 TAKE 或 ARM。详见 [导航任务控制权](NAVIGATION_AUTHORITY.md)。

2026-10-18 修复版 v0.2：短时状态抖动先停止、不立即断开 TCP；持续 3 s 无反馈才断开；修复真实 SDK 释放后的 UDP 句柄残留；横移/转向独立选档，默认 0.4 m/s / 0.6 rad/s。使用前重启电脑 Python 服务并刷新网页，不能只刷新旧后台。修复依据和测试见 [修复记录](FIXES_20261018.md)。

这是新的独立 demo，不覆盖同级 `go1_keyboard`。旧版稳定快照已提交为 `071f3b2`。新需求的完整设计、协议和实机验收流程见 [方案.md](方案.md)。

当前已实现电脑界面、本机 Python 服务、C++ 网络接收端、稳定版运动策略、SDK 适配器和离线模拟测试。**2026-10-16 已部署到真实机器狗，ARM64 SDK 编译及热点连接验证通过，接收服务开机只监听、不自动接管。依照用户要求，本轮不接管、不解锁、不测试真实行走；运动及避障现场验收仍待完成。**

真机日常使用优先看 [网络控制使用教程](网络控制使用教程.md)，本次部署证据与旧目录恢复方式见 [部署记录](DEPLOYMENT_20261016.md)。

当前 22 项 Python 系统/状态测试、5 项启动入口测试、12 项前端状态测试通过；本次浏览器也复查了模拟控制、松键停止和离开页面解除解锁。此前 v0.2 的 ARM64 SDK 回环测试保留，本轮未重新连接真机。当前范围见 [状态修复记录](STATUS_FIX_20261018.md)，此前记录见 [v0.2 修复记录](FIXES_20261018.md)。

## 1. 现在就能试界面，不需要机器狗

Windows PowerShell：

```powershell
cd 'E:\桌面\other\Go1研究\Demo\go1_remote_control'
& 'D:\programs\py\python.exe' .\tools\run_demo.py
```

也可双击 `start_demo.cmd`（需要 Python 和 g++ 在 PATH）。程序会编译只支持模拟的接收端，随后自动打开 `http://127.0.0.1:8099`。模拟接收端只监听本机，界面始终显示 SIMULATION ONLY，不会连接 Go1，不使用 SSH，不需要 Unitree SDK。

界面操作：

1. 点击“连接接收端”，先看到网络在线，未自动接管。
2. 模拟时勾选现场确认框，点击“接管控制”；这一步会让模拟遥测出现。实机时必须真的已站立、已拔掉充电器。
3. 等健康静止就绪达到 500 ms，点击“A 解锁运动”。
4. 按住 W/S 前后、J/L 横移、Z/C 转向，也可按住屏幕方向按钮。松开即请求停止。
5. 空格/X 停止，R 清故障，O 静止时切换避障，Q 释放 SDK 控制（页面不关闭）。
6. 切换到其他窗口或隐藏页面，会停止并解除解锁；回来后重新 A。断网重连后先重新确认接管，再 A，不恢复旧命令。

不依赖终端的按键自动重复：浏览器利用 keydown/keyup 表达按住/松开，本机服务按 20 Hz 续发。机器狗端每条 MOVE 保留 600 ms TTL，按住时不断刷新；没有连续运动总时长上限。松键发零目标，失焦解除解锁，失联不会无限续走。

模拟主程序使用 Ctrl+C 退出。不要把模拟页面的 60% 电量、速度或姿态当作机器狗实测值；画面中的机器狗是 SVG 示意图，不是相机画面。

## 2. 之后使用真机

这台机器狗的首次安装与配对已完成，不需要再次生成密钥或重新部署。连接热点后，双击 `start_pc.cmd` 即可；若电脑服务已经运行，直接打开 `http://127.0.0.1:8099`，不要重复启动。

下面的配对命令仅供未来首次安装另一套环境参考；当前电脑已有 `config/pc.json` 和 `config/robot.token`，请保留，不要删除或重新配对。首次安装需把接收程序上传到机器狗；这是一次性过程，不是运行时用 SSH 传键盘。

电脑生成配对文件（不连接机器狗）：

```powershell
& 'D:\programs\py\python.exe' .\tools\create_pairing.py
```

会生成 `config/pc.json` 和 `config/robot.token`，不要提交 Git 或分享。机器人端需要相同 token；程序不使用 SSH 密码。之后连接机器狗热点、启动接收服务，在电脑执行：

```powershell
& 'D:\programs\py\python.exe' .\pc\server.py --config .\config\pc.json
```

或双击 `start_pc.cmd`。界面在电脑本机，网络目标默认 `192.168.12.1:8765`，之后控制流程同上。

## 3. 目录

```text
pc/server.py                 本机 HTTP 服务，只监听 127.0.0.1
pc/transport.py              本机 → 接收端 TCP、鉴权、浏览器租约
pc/navigation_authority.py  与 RobotLink 共用的导航任务租约和单轴命令边界
pc/web/                      HTML / CSS / JavaScript 可视化前端
robot/receiver.cpp           接收、鉴权、会话、独立看门狗与 SDK 控制
robot/control_policy.hpp     从 071f3b2 提取的稳定运动策略
robot/legacy/go1_highlevel.h  保留原 SDK 适配器、健康检查、端口锁和停止流程
robot/go1-remote.service      待实机验收后选用的 systemd 单元
tools/create_pairing.py      生成配对密钥，不连接机器狗
tools/run_demo.py            一键离线模拟与电脑界面
tests/test_system.py         模拟接收端＋电脑链路的集成测试
tools/check_monitor.py      真机仅认证/心跳/重连检查，绝不接管 SDK
tools/deploy_robot.py       本工作区的一次性 SSH 维护脚本，日常运行不依赖
CMakeLists.txt               机器狗 Linux 构建 / 模拟构建
方案.md                      架构、协议、安全策略和分阶段验收
网络控制使用教程.md           当前真机环境的日常启动、操作及故障处理
DEPLOYMENT_20261016.md       本次部署、测试边界和旧目录备份位置
```

## 4. 本机测试

先用 `run_demo.py` 编译过模拟接收端，再运行：

```powershell
& 'D:\programs\py\python.exe' .\tests\test_system.py
```

也可直接执行测试，缺少模拟程序时会自动编译。退出正在运行的模拟 demo 后再重新编译，避免 Windows 上 exe 被占用。

电脑应用运行只依赖 Python 3 标准库和浏览器；g++ 仅用于编译离线模拟端。真机接收端依赖 Linux、C++14、原有 Unitree Legged SDK v3.8.6。没有 ROS、Qt、Node、pip 包或云服务运行依赖。

## 5. 安全边界

- 模拟二进制拒绝 `--live`；模拟模式只能绑定 127.0.0.1。真机配置与模拟接收端模式不匹配时，电脑端拒绝继续连接。
- 默认网络服务不打开 SDK。点击接管才开始 SDK 零速度控制；不是只读遥测。未接管时电量等未知，不为了显示状态而偷偷发送控制帧。
- 前后最大 ±0.5 m/s；新版横移默认/最大 ±0.4 m/s，转向默认/最大 ±0.6 rad/s；可在接管前选更低档位。转向实际速度异常阈值为 0.9 rad/s，平面实际速度阈值仍为 0.8 m/s。最低 SOC 10%、姿态/关节/充电/手柄/新鲜度保护保留；无固定连续运动时限，但这些保护仍能中止动作。
- 不要同时运行旧 SSH 键盘 demo。两者共用 SDK 端口 8111 和同名锁文件，会拒绝冲突。
- token 仅用于私有局域网配对，TCP **未加密**；不应暴露到互联网或不可信共享 Wi-Fi。不得把此原型当作安全认证的急停系统。
- 导航还需要本机 `navigation_token`。它只允许已存在的 control_active + armed 会话提交带 command_id 的单轴 primitive；租约过期、状态不新鲜、重复 command_id 或浏览器 STOP 都会停止并撤销导航控制权。导航接口只调用 `RobotLink`，不创建第二个控制端口。
- 原厂避障效果和后向覆盖未验证；掉电、kill -9、控制器故障无法由本程序保证停止。现场手柄与人工看护不可省略。
