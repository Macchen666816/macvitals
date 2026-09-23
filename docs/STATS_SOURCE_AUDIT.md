# Stats 源码审计

审计日期：2026-09-22
参考项目：Stats（[exelban/stats](https://github.com/exelban/stats)）
本地参考目录：`upstream-reference/stats`
审计提交：`a9bf99866eac97d62e8952c058961f6c5e51bc67`（2026-09-21，`fix: fixed the Network reader polling airportd when the module is disabled (#3629)`）

名称说明：本次审计发生在项目首次公开发布前，当时项目暂名 PerformanceTable；正式发行名称现统一为 MacVitals。审计对象、结论和证据没有因改名而改变。

## 结论先行

Stats 的核心采集策略不是“所有数据都交给 powermetrics”。它按指标选择更贴近系统的来源：

| 指标 | Stats 主要来源 | 结论 |
|---|---|---|
| CPU 总体 / 逐逻辑 CPU 利用率 | Mach `host_statistics`、`host_processor_info` | 累计 ticks 相邻差分；首帧建立基线 |
| CPU 频率 | IOReport `CPU Stats` performance states | 按 residency 和频率表加权，不解析 powermetrics 文本 |
| GPU 利用率、渲染器、tiler、FPS | IOKit `IOAccelerator`、IOReport DCP channels | 多种字段按能力读取，缺字段保留缺失 |
| ANE 功耗 / 利用率 | IOReport `Energy Model` | 能量差分除以时间，再按平台上限归一化 |
| 内存 | Mach `host_info` / `host_statistics64`、`sysctl` | 页计数、内核压力等级、swap 结构体 |
| 温度、电压、功率、风扇 | AppleSMC（含 privileged helper） | 原始键先映射为测点，再分组和聚合 |
| 磁盘容量 / I/O | Disk Arbitration、statfs、IOKit、SMART | 识别设备身份，避免卷和驱动层重复计算 |
| 网络 | `getifaddrs`、`sysctl`、CoreWLAN、接口计数 | 按接口累计计数差分，切换接口时重建基线 |
| 进程 CPU / 内存 | `/bin/ps`、`top` | 使用系统工具快照；进程排序与模块分开 |

因此，MacVitals 目前的 powermetrics 管线应视为“特权功率/频率补充源”，而不是所有数据的唯一来源。Stats 的架构支持我们继续保留当前稳定的 powermetrics 解析，同时把未来的原生 API 迁移作为独立、可验证的工作项。

## 审计范围与许可边界

本次阅读覆盖：

- `Modules/CPU/readers.swift`
- `Modules/GPU/reader.swift`
- `Modules/RAM/readers.swift`
- `Modules/Disk/readers.swift`
- `Modules/Net/readers.swift`
- `Modules/Sensors/readers.swift`、`Modules/Sensors/values.swift`
- `SMC/smc.swift`、`SMC/Helper/main.swift`
- `Kit/module/reader.swift`、`Kit/plugins/Repeater.swift`、`Kit/plugins/DB.swift`
- `Kit/plugins/SystemStats.swift`、`Modules/Remote/main.swift`
- `README.md` 与 `LICENSE`

上游是 MIT License。若将上游代码或实质性代码片段复制进 MacVitals，必须保留上游版权声明和 MIT 许可文本，并保留免责声明；“无任何限制”并不准确。本项目当前只记录接口、算法和设计取舍，没有复制 Stats 源码，也不构成直接派生分支。

`upstream-reference/` 是本地审计材料，不属于 MacVitals 发布内容。未来初始化本项目 Git 仓库时，应将该目录加入 `.gitignore`，或在发布前移除它；若确实采用上游代码，则应另行保留许可证并明确派生关系。

## 采集管线

### CPU 利用率

`LoadReader` 调用 `host_processor_info(mach_host_self(), PROCESSOR_CPU_LOAD_INFO, ...)` 取得每个逻辑 CPU 的 user/system/nice/idle 累计 ticks，相邻样本用无符号差分计算每核利用率。整机比例来自 `host_statistics(..., HOST_CPU_LOAD_INFO, ...)` 的四类累计 ticks 差分：

```text
busy = Δuser + Δsystem + Δnice
total = busy + Δidle
利用率 = busy / total
```

首帧没有上一份计数，逐核结果为空或等待下一帧；无效总量不会被当作真实 100% 或 0%。计数回绕使用无符号减法。Stats 可选择显示逻辑线程，也可按超线程兄弟合并；异构核心簇的分组来自 SystemKit 的运行时拓扑，而非芯片名称硬编码。

需要注意：源码中 `prevCpuInfo` 的 Mach 内存由 `vm_deallocate` 释放，当前实现依赖连续采样生命周期；MacVitals 采用自己的快照模型，不能直接复制其指针管理代码。

### CPU 频率

`Modules/CPU/readers.swift` 查询以下 IOReport channel：

- `CPU Stats / CPU Complex Performance States`
- `CPU Stats / CPU Core Performance States`

它读取各性能状态的 residency，并用 SystemKit 提供的频率表做加权平均，再按 E/P/S cluster 输出。没有活动 residency 时使用最低频率边界，通道缺失则保持不可用。该口径是“采样窗口内工作状态的平均频率”，不是瞬时 PLL 频率，也不是整段时间一直运行在该频率。

这与 MacVitals 当前从 powermetrics 的 `HW active frequency` 读取不同。IOReport 值得作为未来原生频率后端研究，但它依赖私有/半公开接口、系统版本和频率表映射，不能未经多机型验证就替换已验证的 powermetrics 管线。

### GPU、FPS 与 ANE

`Modules/GPU/reader.swift` 从 `IOAccelerator` 的 `PerformanceStatistics` 读取设备利用率、Renderer/Tiler 利用率、温度、风扇百分比、核心/显存时钟；GPU 温度缺失时对 AMD/Intel 尝试已知 SMC 键。Apple GPU 还从 IOReport DCP channels 计算 FPS：读取 `DCP*` 的 `swap` 累计计数，相邻样本差除以 `CFAbsoluteTime` 间隔；首帧、时间倒退或计数回退返回不可用。

ANE 从 IOReport `Energy Model` 中汇总以 `ANE` 开头的 channel。代码按单位把 mJ/uJ/nJ/pJ 归一化为焦耳，再用能量差除以真实时间得到功率；按 M1-M5 平台配置上限转换为 0-1 的利用率近似值。这里的“ANE 利用率”是功耗归一化指标，不是神经网络吞吐率。

### 内存

`UsageReader` 使用：

- `host_info(... HOST_BASIC_INFO ...)` 获取物理总内存；
- `host_statistics64(... HOST_VM_INFO64 ...)` 获取 active、speculative、inactive、wired、compressed、purgeable、external 等页计数；
- `sysctlbyname("kern.memorystatus_vm_pressure_level")` 获取内核压力等级；
- `sysctlbyname("vm.swapusage")` 获取 swap 总量、已用和可用。

页数乘 `vm_page_size` 后计算 used/free，并将 purgeable/external 作为 cache 相关组成。这个口径仍是工具自己的分类估算，不等同于 Activity Monitor 的 footprint。重要的可借鉴点是：页大小、压力和 swap 都通过结构体/API 读取，而不是解析命令文本或根据剩余百分比猜测压力。

### 温度、传感器与风扇

Stats 通过 `AppleSMC` IOKit 连接读取 SMC keys；`SMC/Helper` 以 launchd privileged helper 和 XPC 提供更完整的读写能力，甚至支持风扇控制。这比 MacVitals 当前只读、面向观察的 Python 接口更重，也带来更高权限和安装维护成本。

`SensorsReader` 先按平台筛选 `SensorsList`，再读取已知键，处理带 `%` 的序列键，最后才按设置决定是否加入未知键。温度为 0 或超过 110°C、异常电流等会被过滤。传感器随后按 CPU/GPU/system/sensor 等组分类，并计算 Average/Hottest 等聚合值。

Stats README 明确说明：CPU/GPU 测点是 thermal zones，不等于物理核心；“CPU Efficient Core 1” 不代表某一个物理核心；每代 SoC 的键都会变化。这个命名原则与 MacVitals 当前的保守策略一致：展示“CPU 热区/测点”或经过验证的友好名称，不把原始代号直接扔给用户，也不从测点编号推断核心身份。

### 磁盘与网络

磁盘容量使用 Disk Arbitration 识别挂载卷，再用 `statfs` 计算总量和可用量；APFS 额外估计 purgeable space。磁盘活动从 IOKit 驱动层的 `Statistics` 读取 Bytes (Read/Write)，并按设备 BSD 名称、路径、文件系统确认身份变化。进程 I/O 用 `proc_pid_rusage` 的累计字节数差分。

网络接口速率从 `getifaddrs`/`NET_RT_IFLIST2` 的累计收发字节差分得到。接口变化会清空基线；不可达时重置带宽；计数回退取 0；还会按链路速率对超过物理可能值的单次跳变丢弃。这个“基线 + 计数回退 + 物理上限”组合，是 MacVitals 网络速率防尖峰逻辑的有价值参考。

### 进程

CPU 页面使用 `/bin/ps -Aceo pid,pcpu,comm -r`，内存页面使用 `/usr/bin/top -l 1 -o mem ...`。Stats 将进程读取作为独立 Reader，不把一次快照误当成长期平均值；RAM 页面可按负责进程合并子进程。它仍依赖系统命令文本格式，因此解析规则需要随 macOS 版本验证。MacVitals 已采用累计 CPU 时间差分以明确采样窗口口径，并支持 CPU/RSS 双排序，不能直接退回单次 `ps %cpu` 的模糊口径。

## 采样调度、缓存与缺失语义

`Reader<T>` 使用受串行队列保护的最新值，后台 `Repeater` 由 `DispatchSourceTimer` 驱动，默认有 200ms leeway，并提供 start/pause/stop/reset。弹出窗口 reader 在需要时异步读取，避免阻塞主线程。`callback` 才会更新最新值、写入有限频率的历史缓存，并把可序列化的值发送给 Remote。

`DB` 使用 Application Support 下的 LevelDB（失败时临时目录），普通 key 约 30 秒节流写入，历史 key 有 TTL。它适合菜单栏历史趋势，但不意味着每次采样都落盘。MacVitals 当前是实时 SSH 仪表，不应为了对齐 Stats 而引入数据库；若将来增加历史，应把采集快照、展示和持久化解耦。

Stats 多数 Reader 在失败时不触发新值，或保留明确的可选字段；这比用 0 表示“命令不支持、权限不足、没有设备”可靠。MacVitals 应继续区分未知、等待首帧、不可用和真实零值。

## Remote 的真实网络路径

README 的 “External API” 只说明更新检查和 public IP，不能代表 Remote 模块。源码确认 Remote 在用户登录并启用后包含三类能力：

1. **监控：** 通过 `api.system-stats.com` 注册设备和账户，以设备 UUID 标识；各 Reader 的 `callback` 经过 `SystemStats.shared.send` 编码后发布到 `wss://broker.system-stats.com:8084/mqtt` 的 `stats/<uuid>/metrics/<key>` topic。
2. **控制：** MQTT 订阅 `stats/<uuid>/control/+`，可处理音量、静音、睡眠、重启客户端和禁用控制等命令，并回传 control acknowledgement。
3. **更新：** 远程 update 命令触发客户端检查更新；OAuth 流程使用 `oauth.system-stats.com`，access/refresh token 存入 Keychain。

远程机器列表、主机状态和历史数据由 `Modules/Remote/main.swift` 从 API 获取，界面链接到 `app.system-stats.com`。因此用户此前对“Stats 远程监控需要经过服务器”的判断是正确的，但需加上条件：这是登录并启用 Remote 后的能力，不是普通本地监视的必经路径。它与 MacVitals 的定位差异很清楚：MacVitals 默认 SSH 直连被监测 Mac，不需要账户、第三方服务器、MQTT broker 或远程控制权限。

## 与 MacVitals 的比较

### 与活动监视器的对照边界

Stats 的设置和源码都不能证明它与活动监视器使用相同统计口径。对照时必须先比较定义：

- CPU 百分比可能共享系统计数来源，但刷新窗口、聚合方式和进程归一化仍可能不同。
- RAM 的 wired、compressed、cached、App memory、footprint 等分类不是同一组互斥字段；数值接近也不代表定义相同。
- GPU active residency、频率和功率分别描述活跃时间、工作频率和系统估算功率，不应直接当作活动监视器的 GPU 百分比或整机功耗。
- 温度取决于 SMC/IOKit 测点映射，平均值、最高值和热压力不是同一指标。
- 网络和磁盘速率是接口/设备累计计数差分，不能直接解释为某个应用的净流量或文件读写。

因此，Stats 只用于发现采集思路、实现第二套观测和定位明显异常；MacVitals 的每个字段仍以自身记录的来源、统计窗口和缺失语义为准。

| 方面 | Stats | MacVitals 当前决策 |
|---|---|---|
| 产品形态 | 菜单栏 GUI、后台 helper、可选云端 Remote | SSH 优先的终端分块仪表盘 |
| CPU 利用率 | Mach ticks 差分 | Mach/系统累计计数差分，首帧等待 |
| 频率 / 功率 | IOReport、IOAccelerator、SMC | powermetrics 特权流，完整帧解析 |
| 内存 | Mach/sysctl 结构体 | 系统 API/命令补充，明确估算口径 |
| 温度 / 风扇 | SMC + privileged helper，可控制风扇 | 只读 SMC 等接口，按已验证测点命名 |
| 网络 | 原生接口计数、VPN/链路上限防尖峰 | 保留接口基线、路由/VPN 切换重建逻辑 |
| 远程 | 账户、API、OAuth、MQTT、控制/更新 | SSH 连接路径，无默认服务端依赖 |
| 历史 | LevelDB + 菜单栏趋势 | 当前只做实时观察，不贸然持久化 |

## 可借鉴与不建议照搬

优先借鉴：

1. 为 CPU/内存/网络等指标选择原生 API，减少不必要的命令文本解析。
2. 把每个指标做成独立 reader，明确首帧、基线、超时和缺失值状态。
3. 使用运行时硬件拓扑和平台映射，不根据芯片名称或探头编号猜物理含义。
4. 继续采用温度热区/测点命名，平均值和最高值分别标示，不把传感器原始代号当用户界面。
5. 为网络和磁盘计数增加设备/接口身份变化与计数回退保护。

不建议直接照搬：

- 不为终端实时观察引入 Stats 的 GUI、LevelDB、菜单栏生命周期和后台 helper。
- 不直接复制 IOReport 私有接口而跳过多机型、多系统版本验证。
- 不引入 SMC 写入、风扇控制或远程控制能力；这会扩大权限和安全边界。
- 不把 Stats Remote 的云端账户、MQTT 中转作为 MacVitals 的默认依赖。
- 不把上游仓库整体合并成 fork；这会带来产品目标、Swift/AppKit 架构和发布许可边界的混杂。

## 后续优先级

1. **保持现有 powermetrics 后端稳定。** 继续用真实完整帧、超时和明确状态保证特权指标可信。
2. **建立 IOReport 研究分支/探针。** 已增加 `python3 -m macvitals --ioreport-probe`，只验证 CPU performance states、Energy Model 和 DCP swap 的两次采样差分；先与同一时间窗口的 powermetrics 对照，不立即替换生产管线。Stats 与活动监视器都只作为辅助参照。
   管线验证还增加了管理员专用 `sudo python3 -m macvitals --compare-probes`：实测中 `powermetrics` 完整帧窗口约 0.523 秒，IOReport 窗口约 0.516 秒，均成功取得两次样本。CPU state active residency 与 `powermetrics` 集群 active frequency 只具有相关性，不能直接互换；Energy Model 的 CPU Energy 差分可换算为窗口平均功率，但仍不是活动监视器的同名功率字段。
3. **扩大传感器验证矩阵。** 记录机型、macOS 版本、SMC key、友好名称和实测证据，区分“代码支持”和“真机确认”。
4. **继续保持 SSH 直连。** 项目明确不增加历史记录、日志数据库、导出或告警；远程能力只服务于随用随开的实时观察。
5. **发布前隔离参考材料。** 将 `upstream-reference/` 作为审计目录排除出发行包，并在项目许可证策略确定后再决定是否引用 MIT 代码。

## 证据与推断边界

以上“来源、函数、网络端点、许可要求”均由本地 checkout 的源码、README 或 LICENSE 直接确认。关于 IOReport 在未来 macOS/芯片上的稳定性、频率表完整性、SMC 键的物理含义和不同指标之间的数值一致性，是工程推断或待验证事项，不应写成 Apple 官方保证。Stats 的采集实现值得参考，但其版本和平台支持会变化；本审计对应上述提交号。
