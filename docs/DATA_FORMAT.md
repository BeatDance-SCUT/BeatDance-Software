# 采集数据与机器学习使用说明

## Infra v1 扩展

原始 UDP v1 和 `imu_raw.csv` 格式保持兼容。新增独立人工标注版本、固定引用的数据集快照与实验结果，字段及评估口径见 [Infra 数据契约](INFRA.md#对象与文件契约)。人工动作按 `capture_key + segment + time` 解释；`valid` 区间显式启用评估，`exclude` 区间优先排除。

公共离线算法支持 `AlgorithmConfig`，默认参数与下文一致；自定义配置由每次实验单独记录。实验结果增加 `selection/capture_key/signal/segment/span` 分组，事件仍标记为候选波峰。时间偏差仅为离线候选与人工标注的比较，不代表声音延迟。原有 `save_analysis` 导出与当前视图间隔下载保持原格式和默认参数。

## 数据流和批次

数据流固定为：ESP32 UDP JSON → 协议校验/顺序检查 → `imu_raw.csv` → 从 CSV 读取 → 曲线与派生分析。

解析器同时接受 UDP v1 扁平报文（seq/t_us/ax/ay/az/gx/gy/gz）和旧嵌套报文（seq/t_ms/acc_g/gyro_dps）。当前仓库固件仍使用嵌套格式、20 ms 周期（50 Hz）；软件元信息固定写 target_sample_rate_hz=100，两者尚未统一。实际采样率应由设备时间核对，不从元信息推定。

每次点“开始新采集”，自动创建 `data/recordings/UTC日期/UTC时间_微秒_随机编号/`。批次的原始数据只追加，分析不修改原始数据，下一次采集使用新目录。

## `imu_raw.csv`：训练数据的原始来源

UTF-8、逗号分隔、有表头；每行一个已通过校验且被接受的原始样本。数值不做滤波、重采样、重力扣除或零偏校准；保留硬件发送精度。

| 字段 | 含义与单位 |
| --- | --- |
| `host_s` | PC 接收 Unix 秒，只用于诊断 |
| `seg` | 当前采集内推测的启动片段，从 0 起 |
| `time` | 当前片段首条已接受样本起累计的设备秒数 |
| `seq` | uint32 样本序号 |
| `t_us` | uint32 微秒时钟；旧嵌套报文由 `(t_ms × 1000) mod 2³²` 转换，约 71.6 分钟回绕 |
| `t_ms` | `t_us // 1000`，不是 Unix 时间，也不保证等于旧报文原始毫秒值 |
| `ax`、`ay`、`az` | XYZ 加速度，g，含重力 |
| `gx`、`gy`、`gz` | XYZ 角速度，°/s |

当前磁盘表头为 `host_s,seg,time,seq,t_us,t_ms,ax,ay,az,gx,gy,gz`，没有逐行 device、sender_ip、imu_id、temp_c、v、seq_gap。拒收包不写入 CSV。当前接收器按首个有效包的来源 IP 锁定，不是 device+IP；不能据此可靠区分同一 IP 后的多个设备。

旧 17 列 pc_time/device/.../ax_g/.../segment/time/seq_gap CSV 仍支持读取，不重写历史原件。normalize_frame 保留原列并补充分析层规范列：seg→segment、ax/ay/az→ax_g/ay_g/az_g、gx/gy/gz→gx_dps/gy_dps/gz_dps。它们不是当前磁盘表头；进入算法后加速度才乘 9.80665 转为 m/s²。受支持的无 time 扁平文件按展开的 t_us 差值构造时间。

重启识别采用规则：相对上一已接受包，序号与微秒时钟均反向、模差均不小于 2³¹，且已至少 2 秒没有接受新样本，则建立新片段。它是启发式规则，不能完全区分长时间迟到的旧包与真实重启；不满足规则的回退包会被丢弃。已知设备重启时，建议主动开始新采集。

序号缺口只表示遗漏的样本序号，不能直接当成纯网络丢包。UDP 不提供重传；发送前漏采且未递增序号的 IMU 读取失败也无法由缺口推断。

## `session.json`：标签、来源和质量

- `capture_id`、`started_at_utc`、`ended_at_utc`：批次标识和 UTC 起止时间。
- `labels.participant`：可选匿名参与者编号；`labels.activity`：整批动作标签；`labels.notes`：佩戴位置、实验条件等备注。
- `units`、`protocol_version`、`schema_version`：解释数据的单位和格式。
- `owner`：当前接收器锁定的来源 IP，历史元信息可能保存设备与 IP 组合；`counts`：已保存样本、无效包、其他设备包、重复/乱序包、序号缺口、推测重启的统计。
- `status`：`recording` / `stopped` / `error`；`error`：停止错误；`derived_files`：派生文件与处理参数。

标签在开始采集前填写，对整次采集有效；一批包含多种动作时，应拆分批次或另建逐段人工标注文件。空标签表示未标注，波峰结果不自动生成真实动作标签。

columns 记录实际表头；现有 units/sequence_policy 部分文字仍沿用旧字段及设备归属描述，应结合实际列与本文解释，不能据此补造数据。元信息在创建、设置标签和正常停止时写入；运行中的计数以界面为准。进程被强制终止可能留下 `recording` 状态和未结算计数，此时以原始 CSV 实际行和序号为准。每条原始行都 flush 到操作系统，但未逐条 fsync；突然断电仍可能丢失系统缓存中的数据。

## `analysis_*.csv`：可重新生成的派生数据

停止时自动生成完整 `analysis_magnitude.csv`，历史页也可生成 `analysis_x.csv`、`analysis_y.csv`、`analysis_z.csv`。自动生成的合加速度与实时界面选择的单轴无关；所选轴的完整文件可在历史页生成。

| 字段 | 含义 |
| --- | --- |
| `signal` | `Magnitude` / `X` / `Y` / `Z` |
| `segment` | 对应原始启动片段 |
| `time` | 该片段内的设备相对秒数 |
| `raw_acceleration` | 转换为 m/s² 后重采样的输入；合加速度使用三轴模长 |
| `filtered_acceleration` | 四阶 Butterworth 零相位低通输出，m/s² |
| `is_peak` | 算法检测波峰，0 / 1，不是真实跳绳标签 |
| `span` | 每个启动片段内的连续信号段；超过 0.2 秒的缺口处分段 |

每个连续段按设备时间间隔中位数建立等间距网格，线性插值后滤波；截止频率 `min(5 Hz, 0.4 × fs)`，波峰 prominence 为 0.7 m/s²，最小间距 `max(1, int(fs/2.5))` 点。不足 1 秒或不超过 15 点的片段仅展示输入，不滤波计峰。零相位滤波使用未来样本，适用于离线分析；实时显示是不断重算的窗口估计，边缘波峰可能变化。

不跨启动片段、也不跨大缺口计算波峰间隔。派生文件可能插入短缺口内的插值点，因此不能与原始 CSV 按行号对齐，需按 `segment + time` 对齐。实时显示只读尾部最多 30,000 行并显示选定时间窗口，停止/历史页生成的派生文件处理整份 CSV。

## `peak_intervals.csv`：当前视图的波峰间隔

分析区的 Δt–t 图使用 `beatdance.signal_processing.peak_intervals` 从分析数据派生间隔；可展开“间隔数据（当前视图）”下载 CSV。此下载不自动写入采集目录，原始 CSV 和分析 CSV 的格式不变。

| 字段 | 含义 |
| --- | --- |
| `signal`、`segment`、`span` | 输入存在这些列时保留，分别按信号、设备启动片段、连续段独立计算 |
| `time` | 后一个波峰的设备相对时间 t，秒 |
| `previous_peak_time` | 同组前一个波峰的设备相对时间，秒 |
| `delta_t` | `time - previous_peak_time`，即 Δt，秒 |

每组按时间排序，只保留正间隔；每组首个波峰没有间隔，重复时间产生的零间隔不输出。图中各组独立连线，虚线与指标卡使用相同的平均间隔。没有有效间隔时显示提示。

实时和历史回看导出的是当前所选窗口，窗口外的前一个波峰不参与计算。若需整批间隔，可先在历史页生成完整分析 CSV，再通过“分析 CSV”模式上传该文件。旧分析 CSV 缺少分组列时，按其提供的分组列处理；全部缺失时视作一个连续信号，无法据此识别重启或缺口。

间隔是检测波峰的派生特征，可用于观察节奏变化；漏检或多检也会改变间隔，不能当作已验证的真实跳绳间隔或训练标签。

## 训练准备

以 `imu_raw.csv` 为保留底稿，用 `session.json` 联结批次标签，训练前统一轴向、佩戴位置、单位和采样策略。可按 `(capture_id, segment)` 切窗，排除或标记缺口，保留质量筛选记录。按参与者或整批采集划分训练/验证/测试集，避免同次采集的相邻窗口泄漏到不同集合。

`data/recordings/` 下的采集批次默认被 Git 忽略，需单独备份；仅提交代码并不能保存训练数据。文件包含实验信息时，按项目的数据管理要求保存。

## 计数实验与视频复核产物

| 产物 | 内容 |
| --- | --- |
| 实验 config.json | algorithm_id、公共 algorithm 参数、detector_params、统一匹配容差 |
| analysis.csv / events.csv | 分组列 selection/capture_key/signal/segment/span；分析可带 threshold/high_threshold/low_threshold |
| inputs/<selection>/candidates.csv | time/span/accepted/reason，可选 refractory_seconds/decision_sample_time；A0 可为空 |
| cases.csv / metrics.json | 一对一匹配、误检漏检、逐片段与汇总评分；无评分依据为 not_evaluated |
| manifest.json 的 timings | 离线计算耗时，不是在线检测延迟 |
| results/counting_batches/<id>/ | batch.json 保存实验问题、用途、状态、子实验 ID；configs.json 保存配置列表 |
| results/counting_reviews/<id>/ | review.json 保存所选实验与结论；report.html 保存组会报告 |
| 浏览器导出的复核 JSON | 对齐点、视频基本信息及人工复核；多算法模式还用 review_context 绑定实验、配置与选区 |

threshold 是峰显著度门槛，不是绝对波形高度；decision_sample_time 也不证明在线时延。复核标记不自动成为评分 Annotation。独立复核 HTML 内嵌波形和标注、不含视频；恢复 JSON 后仍需重新选择本地视频。当前根 .gitignore 未单独列出 counting_batches/counting_reviews，提交前检查状态并另行备份。

详细参数与复现范围见 [COUNTING_LAB.md](COUNTING_LAB.md)。
