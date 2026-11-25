# BeatDance 正式采集与算法工作台

## 启动与完整演示

在软件仓库根目录，使用现有环境：

```powershell
.\.venv\Scripts\python -m streamlit run infra_app.py
```

`infra_app.py` 是唯一正式入口，旧 `app.py` 已移除。采集统一在“采集”页进行；多个工作台会话或硬件诊断绘图工具不可同时占用同一 UDP 端口。工作台仅供单机本地开发；未提供账号、远程服务或多人编辑机制。

无需硬件的演示步骤：

1. 进入“数据”，点击“创建合成流程示例”。它会创建全新的六轴原始采集、人工构造标注和包含两个启动片段的数据集，不覆盖历史文件。
2. 在“标注”选择该采集和已有标注版本，修改动作时间、添加／删除行，或调整 valid／exclude 区间；点击“保存为新标注版本”。绿色竖线为人工动作，绿色背景为有效区间，红色背景为排除区间。
3. 返回“数据”，选定设备启动片段和时间窗，选择新标注版本，加入数据集草稿；可切换其他记录或片段继续加入。填写名称，保存数据集快照。重叠选择会拒绝保存。
4. 进入“实验”，选择数据集，使用默认参数运行。页面刷新只查看进度，不会重新启动任务。完成后点击“刷新任务状态”。
5. 调整 `weak_score` 再运行一次，核对两份实验配置与对比流程。
6. 在“对比”选择两个实验，查看配置与指标，选择失败事件定位信号窗口；下载 CSV、JSON、HTML 或实验 ZIP 包。
7. 离开工作台再重开，可重新浏览数据集、标注和实验历史。从旧实验加载配置可以创建新实验。

真实流程使用“采集”，或导入匹配的 `imu_raw.csv` 和 `session.json`。导入严格校验软件原始格式、单位对应字段、设备归属、序号与设备时间；仅接受已结束（`stopped`／`error`）的批次。独立硬件绘图 CSV 和旧分析 CSV 不能作为该格式导入。旧分析 CSV 在“数据”的专用区域查看。

合成示例及其标注不代表真实动作、实物网络联调或识别精度。仓库中的静态示例见 [infra_synthetic](../data/examples/infra_synthetic/README.md)。

## 模块与依赖方向

- `beatdance.counting`：实时和回放共享的 Loss Longer 入口。
- `beatdance.running_steps`、`beatdance.running_rhythm`：跑步落脚和右臂摆动检测，供 UDP 实时采集与停采回放共用。
- `beatdance.recordings`：按采集模式生成派生文件，为历史跑步记录提供事件重算。
- `infra.store`：采集校验／导入、标注版本、数据集快照、路径边界和原子 JSON 写入。
- `infra.evaluation`：有效区间计算、事件一对一匹配、失败案例与指标。
- `infra.experiments`：无界面实验执行、任务锁、取消、来源记录、报告与结果包。
- `infra.demo`：确定性合成六轴输入与人工构造标注。
- `infra.counting_lab`、`infra.counting_ui`：批量实验、参数扫描、差异复核和报告；复用 ExperimentJob。
- `infra.workbench`：七个 Streamlit 页面；仅调用公共接口、持有会话内接收器与任务对象。
- `infra.video_review`、`infra/video_review.html`：单采集模式生成离线分析，多算法模式读取保存产物；共用本地视频对齐、游标与复核记录。

页面和命令行复用同一个 `ExperimentJob`。采集页按模式逐包处理数据。算法实验台和采集视频复核采用 Loss Longer，跑步批次的汇总与事件 CSV 在“数据”页查看。

## 视频复核

本页含“Loss Longer 计数复核”和“算法实验对比”两种模式。后者直接使用保存的实验结果，也可从算法实验台进入；多轨道、差异跳转与对齐身份见 [视频同步对比](COUNTING_LAB.md#视频同步对比)。以下步骤描述采集复核。

1. 运行 `infra_app.py`，进入“视频复核”，选择已结束的跳绳采集和设备启动片段。波形和计数由 Loss Longer 回放生成。前后事件跳转使用估计时刻。
2. 在播放器内选择本地视频。文件仅通过浏览器的本地文件接口读取，不上传到 Python 服务，也不写入原始采集目录。推荐 H.264 MP4；其他格式能否播放取决于浏览器的解码支持。
3. 暂停到视频拍到屏幕时间的位置。例如视频 `8.40` 秒时，屏幕显示采集 `3.10` 秒，填写 V₁=`8.40`、D₁=`3.10`，点击“应用对齐”。自动使用 `数据时间 = 视频时间 - 5.30`。视频时刻可点击“取当前视频时刻”填写。
4. 若首尾有漂移，勾选两点校正，输入较晚的 V₂、D₂，使用 `D = aV + b`，其中 `a=(D₂-D₁)/(V₂-V₁)`、`b=D₁-aV₁`。只对所选启动片段生效，不能跨设备重启或视频剪辑断点建立一个统一映射。屏幕刷新／网络延迟不会自动消失，需用共同的明显动作核验输入点。
5. 视频的实际 `currentTime` 驱动波形游标。原生播放、暂停、拖动进度条、慢放会同步更新；点击波形或前后波峰按钮会反向定位视频。步进按指定秒数跳转，例如 30 fps 视频可填约 `0.033333` 秒；它不是浏览器保证的精确逐帧解码。数据缺口不连线，超出所选数据范围会提示。
6. 在可疑动作处暂停，选择“疑似漏计”“疑似多计”“真实动作”或“其他”，写备注并记录当前时刻。点击记录的“跳转”可回看。疑似错误由人工确认，未有人工真值时不会自动认定漏计、多计。每次修改对齐后，已有记录按原视频时刻重新计算采集时间。
7. 导出对齐与复核 JSON（包含采集 key、原始 CSV SHA-256、segment、算法参数、视频文件名／大小／修改时间、同步点和复核记录）；之后选回同一采集、参数与视频，可恢复记录。CSV 提供人工复核列表；它不是工作台 Annotation JSON，尚未自动进入评分标注。恢复时检查采集哈希、片段、参数和视频基本信息；视频信息不是内容哈希。

切换页面或修改上方采集／参数会重建播放器，请先导出记录。本页无后台自动保存。下载“独立复核页 HTML”可在浏览器离线打开，内含该片段分析数据，不包含视频或当前播放器内尚未导出的复核记录；重新选择本地视频并恢复 JSON 即可继续。分享 HTML 会分享其内嵌的采集波形，请按数据的实际用途管理。

同步逻辑测试：`node --test tests/video_review_browser.test.cjs`；Python／页面回归：`.\.venv\Scripts\python -m unittest discover -s tests -v`。合成数据或逻辑测试不代表真实视频对时精度已验证。

## 对象与文件契约

| 对象 | 位置 | 内容与规则 |
| --- | --- | --- |
| Capture | `data/recordings/日期/批次/` | 原有 `imu_raw.csv`、`session.json`；原始数据不经分析改写。导入复制到全新批次并记录来源。 |
| Annotation | `data/annotations/<id>.json` | schema 1、采集相对 key、原始 SHA-256、父版本、人工动作 `segment/time`、区间 `segment/start/end/kind`、备注。每次保存新版本。 |
| Dataset | `data/datasets/<id>.json` | schema 1、名称和片段列表；每项固定采集 key、segment、起止时间、原始／元信息哈希、可选标注 id 与哈希。 |
| Run | `results/experiments/<id>/` | 独立清单、配置、数据集与标注快照、来源信息、分析、事件、匹配案例、指标和 HTML。失败及取消保留已完成片段。 |

时间均使用当前设备启动片段的相对秒数；相同时间值在不同 segment 中属于不同事件。区间起止包含端点；动作点必须在该片段的原始时间范围内，重复时间点会被拒绝。标注支持任意多个人工动作以及 valid／exclude 区间；没有 valid 区间就不评估。采样缺口中的人工事件不会被当作已有可靠信号，未评分数量会记录。

数据集是固定引用的快照，原始 CSV、session 元信息或标注文件变化会使后续实验明确失败。新标注不会自动更新旧数据集；要评估新标注必须保存新数据集。首版通过目录扫描索引，未引入数据库，也未提供物理删除功能。

`analysis.csv` 和 `events.csv` 带有 `selection/capture_key/signal/segment/span`，避免跨批次、启动片段和缺口合并。`events.csv` 保存已发布计数及 `emitted_s`。整段逐包回放后按估计时刻裁剪到数据集选区。`cases.csv` 包含 matched／false_positive／false_negative、人工与候选时间及带符号偏差。

批次对象保存于 `results/counting_batches/<id>/`：batch.json 记录问题、用途、状态与子实验 ID，configs.json 保存配置列表。复盘保存于 `results/counting_reviews/<id>/`：review.json 记录所选实验与结论，report.html 为独立组会报告。各实验的 `inputs/<selection>/candidates.csv` 为候选接受／拒绝日志，`manifest.json` 的 timings 为离线耗时，不混入确定性评分。analysis.csv 可含动态阈值列；逐选区指标附带采集标签。

## 算法和评估

当前实验使用 [Loss Longer](LOSS_LONGER.md)。配置省略的参数采用算法默认值。每次实验记录完整的生效参数。

```json
{
  "algorithm_id": "loss_longer",
  "detector_params": {"weak_score": 0.38},
  "tolerance_seconds": 0.2
}
```

评分区间为：数据集选区 ∩ valid 区间 ∩ 连续信号支持区间 − exclude 区间。有效区间合并后统计，排除区间优先；不跨启动片段、采样缺口或排除区域匹配。分别报告评估秒数、排除秒数和未评分人工动作数。

匹配按时间排序，逐次取最早可行的一对人工／候选事件，每个事件只匹配一次，时间差绝对值不超过容差（默认 200 ms）。此策略最大化可匹配数量，不承诺最小化总时间偏差。

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

工作台默认使用仓库路径，与启动时当前目录无关。仅测试或独立工作区需要设置 `BEATDANCE_INFRA_ROOT` 来切换完整数据根目录。采集、标注、数据集和实验数据需另外备份。当前根 .gitignore 未单独列出 counting_batches/counting_reviews，不能假设批次与复盘会被自动忽略，提交前应检查 Git 状态。

## 离线计数算法实验台

新增“算法实验台”页面，预置五种计数方案，支持批量运行、一维参数扫描、同数据配对差异、同步波形与独立 HTML 报告。操作、算法边界和扩展接口见 [COUNTING_LAB.md](COUNTING_LAB.md)。原默认算法及评分规则保留。

算法实验台现已复用视频复核播放器：默认支持多算法与本地视频同步、差异跳转、对齐记录恢复及独立复核 HTML；视频复核页也提供“算法实验对比”入口。详见 COUNTING_LAB.md 的视频同步对比说明。
