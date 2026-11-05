# Infra v1 本地开发工作台

## 启动与完整演示

在软件仓库根目录，使用现有环境：

```powershell
.\.venv\Scripts\python -m streamlit run infra_app.py
```

原有 `app.py` 继续保留。两个入口和硬件绘图工具不可同时占用同一 UDP 端口。工作台仅供单机本地开发；未提供账号、远程服务或多人编辑机制。

无需硬件的演示步骤：

1. 进入“数据”，点击“创建合成流程示例”。它会创建全新的六轴原始采集、人工构造标注和包含两个启动片段的数据集，不覆盖历史文件。
2. 在“标注”选择该采集和已有标注版本，修改动作时间、添加／删除行，或调整 valid／exclude 区间；点击“保存为新标注版本”。绿色竖线为人工动作，绿色背景为有效区间，红色背景为排除区间。
3. 返回“数据”，选定设备启动片段和时间窗，选择新标注版本，加入数据集草稿；可切换其他记录或片段继续加入。填写名称，保存数据集快照。重叠选择会拒绝保存。
4. 进入“实验”，选择数据集，使用默认参数运行。页面刷新只查看进度，不会重新启动任务。完成后点击“刷新任务状态”。
5. 将峰显著度改为 `10` 再运行一次，演示漏检案例；这只是刻意构造的软件功能检查。
6. 在“对比”选择两个实验，查看配置与指标，选择失败事件定位信号窗口；下载 CSV、JSON、HTML 或实验 ZIP 包。
7. 离开工作台再重开，可重新浏览数据集、标注和实验历史。从旧实验加载配置可以创建新实验。

真实流程使用“采集”，或导入匹配的 `imu_raw.csv` 和 `session.json`。导入严格校验软件原始格式、单位对应字段、设备归属、序号与设备时间；仅接受已结束（`stopped`／`error`）的批次。独立硬件绘图 CSV 和旧分析 CSV 不能作为该格式导入。旧分析 CSV 在“数据”的专用区域查看。

合成示例及其标注不代表真实动作、实物网络联调或识别精度。仓库中的静态示例见 [infra_synthetic](../data/examples/infra_synthetic/README.md)。

## 模块与依赖方向

- `beatdance.signal_processing`：公共离线算法和 `AlgorithmConfig`；不依赖页面或 Infra。
- `infra.store`：采集校验／导入、标注版本、数据集快照、路径边界和原子 JSON 写入。
- `infra.evaluation`：有效区间计算、事件一对一匹配、失败案例与指标。
- `infra.experiments`：无界面实验执行、任务锁、取消、来源记录、报告与结果包。
- `infra.demo`：确定性合成六轴输入与人工构造标注。
- `infra.workbench`：五个 Streamlit 页面；仅调用公共接口、持有会话内接收器与任务对象。

页面和命令行复用同一个 `ExperimentJob`。首版接入离线零相位滤波算法，未实现产品在线检测、声音反馈、训练或视频同步。没有新增第三方依赖。

## 对象与文件契约

| 对象 | 位置 | 内容与规则 |
| --- | --- | --- |
| Capture | `data/recordings/日期/批次/` | 原有 `imu_raw.csv`、`session.json`；原始数据不经分析改写。导入复制到全新批次并记录来源。 |
| Annotation | `data/annotations/<id>.json` | schema 1、采集相对 key、原始 SHA-256、父版本、人工动作 `segment/time`、区间 `segment/start/end/kind`、备注。每次保存新版本。 |
| Dataset | `data/datasets/<id>.json` | schema 1、名称和片段列表；每项固定采集 key、segment、起止时间、原始／元信息哈希、可选标注 id 与哈希。 |
| Run | `results/experiments/<id>/` | 独立清单、配置、数据集与标注快照、来源信息、分析、事件、匹配案例、指标和 HTML。失败及取消保留已完成片段。 |

时间均使用当前设备启动片段的相对秒数；相同时间值在不同 segment 中属于不同事件。区间起止包含端点；动作点必须在该片段的原始时间范围内，重复时间点会被拒绝。标注支持任意多个人工动作以及 valid／exclude 区间；没有 valid 区间就不评估。采样缺口中的人工事件不会被当作已有可靠信号，未评分数量会记录。

数据集是固定引用的快照，原始 CSV、session 元信息或标注文件变化会使后续实验明确失败。新标注不会自动更新旧数据集；要评估新标注必须保存新数据集。首版通过目录扫描索引，未引入数据库，也未提供物理删除功能。

`analysis.csv` 和 `events.csv` 带有 `selection/capture_key/signal/segment/span`，避免跨批次、启动片段和缺口合并。`events.csv` 只包含候选波峰，不是已确认的产品动作。输入整个原始启动片段，按缺口分段分析，再裁剪为数据集选区；选区外的样本可能作为离线滤波上下文。`cases.csv` 包含 matched／false_positive／false_negative、人工与候选时间及带符号偏差。

## 算法和评估

`AlgorithmConfig` 的默认值与现有分析兼容：

```json
{
  "algorithm_id": "offline-butterworth-peaks-v1",
  "algorithm": {
    "signal": "Magnitude",
    "cutoff_hz": 5.0,
    "prominence": 0.7,
    "min_peak_interval": 0.4,
    "max_gap": 0.2
  },
  "tolerance_seconds": 0.1
}
```

截止频率仍限制为 `min(cutoff_hz, 0.4*fs)`，四阶 Butterworth、零相位滤波、连续段设备间隔中位数重采样及线性插值保持原逻辑。小于 1 秒或不超过 15 点时不滤波计峰。间距转换为采样点时向下取整并至少为 1。

评分区间为：数据集选区 ∩ valid 区间 ∩ 连续信号支持区间 − exclude 区间。有效区间合并后统计，排除区间优先；不跨启动片段、采样缺口或排除区域匹配。分别报告评估秒数、排除秒数和未评分人工动作数。

匹配按时间排序，逐次取最早可行的一对人工／候选事件，每个事件只匹配一次，时间差绝对值不超过容差（默认 100 ms）。此策略最大化可匹配数量，不承诺最小化总时间偏差。

指标包含：匹配数、误检数、漏检数、计数绝对误差、计数误差率、精确率、召回率、平均有符号／绝对时间偏差。分母为零时 JSON 中为 `null`；人工动作数为零时单列误检。汇总还报告各选区绝对误差之和，避免不同片段的多计少计抵消。

没有标注或没有有效区间显示 `not_evaluated`（未评估）；只有部分选区有评分显示 `partial`。不同数据集的实验可以浏览，但界面提示不可直接比较汇总指标。离线时间偏差不等于在线确认延迟或声音延迟。

## 任务、恢复、复现与备份

操作系统文件锁保证本机同一数据根目录内只有一个实验执行，包括其他浏览器会话及 CLI。后台线程不调用 Streamlit。取消请求在当前片段完成后生效；较长片段可能等待更久。终态是 completed／failed／cancelled；进程异常终止后，下次启动实验将残留 queued／running 记录标记为 interrupted，不删除产物。

每次运行记录 Git 提交号、当前已跟踪差异、未跟踪 Python 源文件快照、Python 和依赖版本、输入 SHA-256。缺少 Git 来源时保留 `provenance_error`，不得将其视为完全可复现。未跟踪非 Python 资源不自动打包；代码、依赖、原始输入与清单应一并备份。

实验 ZIP 包包含运行目录内的配置、代码差异、标注快照和结果，**不包含原始采集**。原始 CSV 和 session 可在“数据”分别导出；即使原始采集失去访问或变化，也可查看保存的实验产物。报告经过 HTML 转义。

采集切页时停止并保存；接收器保留页面失联约 60 秒自动释放端口的行为。停止后若派生分析失败，提示原因并保留原始 CSV。实时查看有界读取、显示抽样不改变原始样本。每行 flush 不代表 fsync 或断电零丢失。

## 命令行和验证

```powershell
# 创建新的合成原始采集、标注和数据集，输出各对象 id
.\.venv\Scripts\python -m infra.demo

# 将上面的 JSON 配置保存为配置文件，使用已有数据集 id 运行
.\.venv\Scripts\python -m infra.experiments run --dataset DATASET_ID --config config.json

# 临时数据根目录可放在 --root，位于 run 子命令之前
.\.venv\Scripts\python -m infra.experiments --root .tmp/demo run --dataset DATASET_ID

.\.venv\Scripts\python -m unittest discover -s tests -v
```

工作台默认使用仓库路径，与启动时当前目录无关。仅测试或独立工作区需要设置 `BEATDANCE_INFRA_ROOT` 来切换完整数据根目录。CSV／JSON 数据、标注、数据集和实验目录默认不进 Git，需要另外备份。
