# 新机器配置与边界

## 安装位置和入口

仓库可放在任意盘、含空格/中文的可写目录。`launch.cmd` 使用自身目录定位代码，窗口与后台必须来自同一构建；不会自动附着到端口上未知服务。

`setup-ui.cmd` 创建仓库内的 UI 虚拟环境，pip 缓存默认放仓库工作区。也可复用已有 pywebview 环境，设置 `SO101_UI_PYTHON` 后启动。不自动安装系统 WebView2、显卡或串口驱动。

配置页可选择任意明确工作区子目录，不能用整个磁盘或用户目录。更改工作区/端口后要求正常关闭并重启，新任务在重启前被阻止。不会自动搬运、删除旧工作区数据。`SO101_WORKSPACE` 可用于显式启动覆盖；日常建议用配置页，避免环境变量与保存的工作区发生冲突。

## 后端环境

已有兼容的 LeRobot 环境：在配置页选择其 `python.exe`，保存后检查模块。界面环境无 Torch 也可以打开；后端检查和工作进程使用所选解释器。

不要把旧机器的整份配置直接覆盖进新安装。便携版固定调用随仓库的 `runtime/shared_scripts`，以便前后端构建检查覆盖实际控制代码；可配置的是自己的解释器、资源、设备和工作区，不会混用旧目录的适配脚本。

从零配置时，需要先准备 Python 3.12 隔离环境、适合本机的 PyTorch/Torchvision/torchcodec 组合，再安装 LeRobot 的 feetech/dataset 扩展与 pynput。不要在系统 Python 里混装，也不要未经核对覆盖其他项目的 Torch。

当前适配器基于 LeRobot `7e241bd630a3719a56157a497ce5d08f244784f1`。本地增量补丁在 `runtime/patches/lerobot-local.patch`，涉及 Windows checkpoint 链接和录制键盘/保存接口；不是任意版本的通用补丁。

需要精确重建该源码依赖时，可在自己的工作区另建 checkout 后执行（替换路径，不修改别的环境）：

```powershell
$work = '你自己的工作区绝对路径'
$env:GIT_LFS_SKIP_SMUDGE = '1'
git clone https://github.com/huggingface/lerobot.git "$work\src\lerobot"
git -C "$work\src\lerobot" checkout 7e241bd630a3719a56157a497ce5d08f244784f1
git -C "$work\src\lerobot" apply --check "$PWD\runtime\patches\lerobot-local.patch"
git -C "$work\src\lerobot" apply "$PWD\runtime\patches\lerobot-local.patch"
# 用你已准备的隔离环境 Python；先核对 CUDA/Torch 等依赖。
& "$work\envs\lerobot\Scripts\python.exe" -m pip install -e "$work\src\lerobot[feetech,dataset]" pynput
```

这些命令仅是依赖配置说明，本平台不会自动执行它们。GPU 推理仍要求 CUDA；模块检查通过不证明 DLL、编解码器和显卡兼容，应在自己的机器验证。

## 绑定机械臂与相机

1. 在配置页查看串口枚举。该操作仅读取端口元数据，不打开串口。没有 pyserial 时会提示选择/安装后端环境。
2. 将正确设备设为主臂/从臂并保存。推荐端口 `auto`；指定端口不会绕过 USB 身份检查。
3. 分别选择对应主从臂的官方标定 JSON。文件可在工作区外，但必须是本设备的六关节标定；不会替你写校准寄存器。
4. 核对 Top/Wrist 索引与真实画面。两个角色不能用同一索引。
5. 从只读监控开始逐项验收。掉线、身份不唯一、标定与舵机不符时拒绝继续，不自动重标定或扩大限位。

## 任务起点和 ACT

采集无需 ACT 权重或固定任务起点；仍需首次低速主从同步和人工开始。输入语言指令不会让普通 ACT 变成语言条件策略。

任务起点可通过自己的标定坐标手填六个目标后另存，或选择已有且与当前标定绑定的文件。没有默认通用折叠姿态，保存也不代表路径已经无碰撞。ACT/复位开始前会再次校验。

当前 ACT 仍受 README 所列的模型形状、CUDA、帧率和时间上限限制。换任务后不得把原开发任务预算误认为自动成功检测。

## 与原开发机器并存

本公开版没有改动原开发机器的桌面入口、`state/settings.json` 或原共享脚本。公开版快捷方式使用独立的 `SO101 Data Platform (Portable)` 名称；如果已有同名快捷方式指向别处，会拒绝覆盖。

每个工作区只允许一个原生窗口实例。不同实例若占用同一服务端口会明确报错，不会连接旧后台；不同程序也不能同时占用同一串口/相机。

## 已测与未测

已做：合成标定/伪串口的配置契约、功能级校验、HTTP 接口、只读输入与受限输出路径、换工作区重启、临时非正式目录和前端 DOM 流程检查。

未做：全新 Windows 系统的完整安装、真实浏览器视觉验收、其他用户硬件的运动/传感器/采集吞吐验收。继续使用前需阅读根目录 SAFETY.md。
