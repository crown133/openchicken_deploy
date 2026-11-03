<p align="center">
  <img src="https://github.com/user-attachments/assets/c2f7c245-8217-46a1-8d1e-e0ba967cd969" alt="MicroDuck upstream robot" width="820">
</p>

<h1 align="center">OpenChicken Deploy</h1>

<p align="center"><em>基于 MicroDuck 的双足机器人部署与运控程序，使用强化学习策略驱动。</em></p>

本项目基于 [Pollen Robotics MicroDuck](https://github.com/pollen-robotics/microduck) 的
`daemon-v0.17.8`（`942e58e3aa6dbb28e7037203fff11866122c7567`）进行适配。
保留原版 Rust workspace、50 Hz 运控、ONNX 策略、Unix socket JSON-RPC 和服务划分，
面向 Radxa ZERO 3W 与 OpenChicken Z3W-A1 HAT。
图片来自上游，展示原版 MicroDuck；本仓库不是官方发布渠道。

策略训练参见 [microduck_rl](https://github.com/pollen-robotics/microduck_rl)。
代码按原版 [Apache-2.0](LICENSE) 许可证发布，原有版权说明保留。

## 新增与适配

- **14 个 XL330 + I²C 机身 IMU**：沿用 `DynamixelIo`，由运控程序拥有串口及传感器反馈，
  支持本机 LSM6DSV16X 机身 IMU；头部 BMI088 通过原有 tofd 链路提供状态。
- **统一手柄映射**：机器人直连蓝牙手柄和配套监控台 Xbox 采用相同动作组合键、
  原生 Start/Select 长按、四种摇杆模式和 0.1 死区。下面给出完整映射。
- **配置化技能**：沿用原生策略槽、技能新增/删除/加载和按键绑定，不增加硬件访问进程。
- **Radxa 启动模板**：`deploy/radxa/` 提供当前目录布局的 robotd、tofd、监控桥接、
  padd、configd 服务模板。新加入的蓝牙服务模板需要在目标机安装和验收，不会因编译而启用。
- **监控接入**：配套监控台通过既有 agent 转发原生 IPC，读取运控已有的关节、电压、IMU 和策略状态。
  监控台源码属于同级独立项目，不包含在本仓库；部署时需配套更新其 agent/protocol 文件。

本次手柄绑定名称使用 **IPC API 48**。升级时同时部署本次构建的 robotd、robotctl、padd/netpadd 等组件。
原有 `[pad] a/b/x/y/lb/rb` 单键配置不再表示新映射，使用下文 `lb_*` 键；不会把旧单键技能自动搬到新组合键。
新增映射已经纳入代码测试，真实蓝牙连接、声音、动作效果及关机仍需在 Radxa 验收。

## 按键与动作

| 按键 | 行为 |
| --- | --- |
| LB+A | 坐下 / 站起 `sit_toggle` |
| LB+十字键↓ | 地面拾取 `ground_pick` |
| LB+十字键← | 左踢 `kick_left` |
| LB+十字键→ | 右踢 `kick_right` |
| LB+十字键↑ | 单次翻滚 `roulade` |
| LB+B | 立即请求停止并松力 `robot.relax` |
| Start 短按 | 首次初始化回默认姿态，之后切换策略使能/关闭 |
| Start 长按 1.5 秒 | 关闭策略、回默认姿态、保持力矩，摇杆恢复行走模式 |
| Select 短按 | 无操作 |
| Select 按住 2–4 秒后释放 | 原生 `robot.rest`：休息、断矩及舵机重启流程 |
| Select 持续达到 4 秒 | 原生 `robot.shutdown`：休息后关机 |

动作组合键按下触发一次，按住不重复。LB+B 优先于其他操作；LB+十字键不切换模式，
释放 LB 不补发模式切换。LB 按住时行走速度归零、暂停摇杆姿态输入，当前模式保留。
长按 Start/Select 后的释放不再触发短按。普通 A/B/X/Y/RB 不触发技能。
原生 Home 的可选灯光功能仍保留，取决于机型；它不属于监控台 Xbox 的本次映射。

## 摇杆模式

不按 LB 时，十字键直接选择模式：

| 选择 | 模式 | 左杆上下 | 左杆左右 | 右杆上下 | 右杆左右 |
| --- | --- | --- | --- | --- | --- |
| ← | 行走，默认 | 前进/后退 | 横移 | 未使用 | 转向 |
| ↑ | 头部 | 头俯仰 | 头偏航 | 颈俯仰 | 头侧倾 |
| → | 头部＋行走 | 前进/后退 | 转向 | 头俯仰 | 头偏航 |
| ↓ | 机身姿态＋头部 | 机身升降 | 机身侧倾 | 头俯仰 | 头偏航 |

头部和机身姿态模式发送零行走速度；退出机身姿态模式取消姿态偏移，退出头部控制进入行走时头部回中。
默认行走范围：前后 ±0.4 m/s、横移 ±0.3 m/s、转向 ±1.0 rad/s。
死区为 **0.1**：绝对值小于死区时为零，死区外直接使用原摇杆比例，不重新拉伸。
原生轴方向、增益及幅度由 `[pad_axes]`、`[pad_drive]`、`[pad_head]`、`[pad_body]` 定义。
配套监控台实现相同默认值；自定义原生轴配置目前不会自动同步至浏览器。

LT/RT 均按按压深度控制嘴部，两者同时按时取最大开度。RT 越过 0.3 时触发一次 `chirp`；
LT 达到 0.3 时保持 `wheee`，释放到阈值以下停止。当前 14 舵机配置不包含嘴部舵机，
嘴部动作需要相应硬件，音频需要板端声音配置；指令支持不代表这些硬件已验收。

## 蓝牙配对与配置

`padd` 只读取 Linux 手柄输入并向 robotd 发送命令，不访问舵机。
配对由 `configd` 通过系统 BlueZ 完成。启用前需核对蓝牙适配器、输入设备权限、socket 路径和服务用户。
Radxa 模板使用 `openchicken1` 用户，其他机器应先修改模板路径与用户名。
configd 的 `--allow-user openchicken1` 授予该用户原生配置修改权限，不仅限于配对。

服务安装并启动后，将 Xbox 开机，按顶部 Sync 键直至快速闪烁，再在 Radxa 执行：

```sh
robotctl_bin=/home/openchicken1/openchicken_deploy/current/bin/robotctl
"$robotctl_bin" --config-socket /run/configd/configd.sock pad pair --timeout 30
"$robotctl_bin" --config-socket /run/configd/configd.sock pad status
```

需要解除配对时使用 `pad forget <MAC>`。配对后建立信任以支持重连，运行手柄驱动仍需 padd 服务。
`[netpad] enabled = true` 会选择 UDP 手柄入口；直连蓝牙应保持 false。
机器人直连输入与监控台输入分别是原生 IPC 客户端，操作时选择一个控制来源；本次没有新增跨客户端控制仲裁。

五个技能绑定可写入原生 robotd.toml，或通过 `robotctl pad bind` 修改，约一秒重读：

```toml
[pad]
lb_a = "sit_toggle"
lb_down = "ground_pick"
lb_left = "kick_left"
lb_right = "kick_right"
lb_up = "roulade"
```

```sh
"$robotctl_bin" --robot-socket /run/robotd/robotd.sock \
  --pad-config /home/openchicken1/openchicken_deploy/config/robotd.toml \
  pad bindings --json
# 修改动作时，将 bindings --json 换成：pad bind lb_up <技能名>
```

LB+B、Start、Select 不作为可重绑定技能。空字符串取消对应技能绑定。
策略管理沿用 `policy --file <robotd.toml> list/add/remove/load/reset`；当前本地 ONNX 的 add
不自动解析旁置 manifest，持续时间等需显式传入。add/remove 使用普通输出分支以请求重载，
再用 `policy list --json` 读取结果。现有策略接口并不支持任意观测维度或动作语义。

## 构建与验证

使用 Rust 1.94.1 和原版 workspace；Linux 构建需相应的 libudev、音视频开发库。

```sh
cargo test --locked -p pad-map -p robotd-params -p robotctl
cargo build --locked --release -p robotd -p robotctl -p padd -p netpadd -p configd
```

Radxa 为 `aarch64-unknown-linux-gnu`，需使用已配置的交叉编译工具链。
`build/radxa/` 提供构建/打包入口；依赖准备后编译使用离线模式，构建不连接机器人、不安装服务。
在包含本仓库的工作目录，可执行 `python openchicken_deploy/build/radxa/run.py --output artifacts/radxa-build-new`；
已具备构建镜像时可加 `--image-ready`，输出目录使用新名称。
完整上游安装流程在本适配中并非通用部署入口：`scripts/install.sh` 保留既有禁用逻辑，
实际部署使用 `deploy/radxa/` 模板及独立的模型、依赖和机器配置，不能直接套用上游发布钩子。

## 程序结构

| 组件 | 职责 |
| --- | --- |
| robotd / openchicken-control | 运控、观测、ONNX 推理、Dynamixel 和机身 IMU |
| robotctl | 原生命令行工具 |
| padd / netpadd / pad-map | 蓝牙/本机手柄、UDP 手柄及共享输入映射 |
| configd | 原生设备配置及蓝牙配对 |
| tofd | 头部传感器状态 |
| btd / mediad / updater | 保留原生 BLE、音视频及更新组件；是否启用由部署决定 |
| deploy / build | 配置模板与构建工具 |

本仓库上传源码、测试和公开配置模板。开发日记、实机测试记录、来源下载清单、私有身份文件、
签名材料、外部策略部署包、构建输出及原上游发布工作流不随仓库上传，保留在开发电脑。
