# BeatDance — IMU 跳绳运动监测软件

采集页面新增大字“采集时间”，显示最近已保存样本的 CSV `time`、批次号和启动片段。录视频时先拍清屏幕，再连续拍动作，末尾再拍一次，便于粗对齐；约 0.5 秒的页面刷新周期及网络、渲染延迟不适合直接作为逐次动作真值，需结合明显动作校准。断流或停止时读数保留在最后样本。

## 正式采集与算法工作台

“视频复核”页支持选择本地视频、输入视频时刻与屏幕采集时间进行单点对齐（可选首尾两点校正漂移），并排同步播放视频和带算法波峰的波形。支持慢放、按指定秒数步进、点击波形跳转、前后波峰定位、人工疑似漏计／多计记录及 JSON／CSV 导出恢复；可下载独立 HTML 离线复核。详见 [视频复核操作](docs/INFRA.md#视频复核)。

唯一正式入口 `infra_app.py` 提供七个页面：采集、数据、标注、实验、对比、视频复核、算法实验台：

```powershell
.\.venv\Scripts\python -m streamlit run infra_app.py
```

“数据”页可一键创建明确标记的合成六轴示例、标注和数据集，完成无硬件流程验证。支持标注版本、固定数据集引用、参数实验、事件匹配评估、失败片段定位及 HTML／CSV／JSON 导出。完整演示、CLI 和数据契约见 [Infra 使用说明](docs/INFRA.md)。旧 `app.py` 入口已移除；采集统一使用工作台的“采集”页。

算法实验台预置固定波峰、自适应幅度、自适应间隔、两者组合及迟滞状态机，支持批量运行、一维参数扫描、同数据事件差异与视频同步复核。选择已有标注数据集即可离线实验，无需连接硬件。操作与算法边界见 [计数实验台](docs/COUNTING_LAB.md)。视频复核页也可选择“算法实验对比”；本地视频不上传，切换实验／片段前请导出对齐记录。

采集与后续离线复核均在同一工作台完成。

软件接收 ESP32 的六轴 IMU UDP v1 JSON，**先保存 CSV，再从 CSV 读取并显示**。采集页显示实时六轴曲线；停止后可在数据页浏览历史记录、在视频复核页分析波峰，并在算法实验台进行离线对比。

## 快速启动

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m streamlit run infra_app.py
```

已有环境可直接执行最后一条。默认进入“采集”页，填写监听地址 `0.0.0.0`、UDP 端口 `5005`、匿名参与者、动作类型、佩戴位置与备注，点击“开始采集”；确认采集时间和样本数增加后再开始动作。结束点击“停止并保存”，在“数据”页查看历史记录。采集期间保持页面打开，切换页面会结束采集。

录视频时先拍清批次、片段与采集时间，并录下明显同步动作；同步动作排除在正式计数区间外。原始 CSV、批次信息与原视频需另行备份。旧分析 CSV 可在“数据”页的专用区域查看。

硬件与电脑需要连接可互通的同一 Wi-Fi。硬件固件中的 `PC_IP` 设为本机局域网 IPv4，`PC_PORT` 与软件端口一致（默认 `5005`）。完整步骤及排查见 [运行说明](docs/RUNNING.md)。

## 文件放在哪里

| 位置 | 内容 |
| --- | --- |
| `infra_app.py`、`infra/` | 唯一正式入口：采集、历史数据、标注、实验与视频复核 |
| `beatdance/counting.py` | 五种离线计数预置及统一检测接口 |
| `beatdance/` | UDP 接收、CSV 处理、信号分析和统一路径 |
| `scripts/` | 模拟信号与噪声基准脚本 |
| `tests/` | UDP、CSV、算法和界面回归 |
| `data/examples/` | 版本管理的模拟 CSV 示例 |
| `data/recordings/` | 按 UTC 日期和唯一批次保存采集数据 |
| `results/experiments/` | 每配置的独立实验、事件、评分与来源 |
| `results/counting_batches/`、`results/counting_reviews/` | 批次问题与配置、复盘结论及报告；本地备份并检查 Git 状态 |
| `results/algorithm_baseline/` | 原项目的算法结果与结果图 |
| `results/simulations/`、`results/algorithm_runs/` | 新运行生成的独立结果批次 |
| `docs/` | 运行说明、数据格式和文件导航 |
| `docs/reports/phase_1/`、`phase_2/` | 原有中英文阶段报告和截图 |

完整文件树和旧路径对照见 [仓库结构](docs/REPOSITORY_LAYOUT.md)，报告逐项索引见 [阶段材料](docs/reports/README.md)。

## 数据归档

每次开始新采集自动创建一个独立目录：

```text
data/recordings/YYYY-MM-DD/HHMMSS_微秒_随机编号/
├── imu_raw.csv                # 完整已接受原始六轴样本
├── session.json               # 参与者、动作标签、备注、单位和质量统计
└── analysis_magnitude.csv     # 停止后生成的完整合加速度分析
```

原始数据和派生分析分开保存。历史页还能生成 X/Y/Z 单轴完整分析。采集批次默认不进入 Git，需单独备份。字段、单位、标签和机器学习使用建议见 [数据格式](docs/DATA_FORMAT.md)。

## 开发命令

```powershell
# 自动回归
.\.venv\Scripts\python -m unittest discover -s tests -v

# 模拟信号，结果写入 results/simulations/ 下的新批次
.\.venv\Scripts\python -m scripts.simulate_imu

# 噪声基准，结果写入 results/algorithm_runs/ 下的新批次
.\.venv\Scripts\python -m scripts.benchmark_algorithm
```

脚本支持 `--no-show` 和 `--output-dir`，详见 [运行说明](docs/RUNNING.md)。

## 当前验证范围

已实现模拟信号分析、UDP v1 协议接入、CSV 持久化、实时显示和历史归档，并有本机 UDP 与界面回归测试。离线计数实验台及视频多算法复核已接入；2026-11-05 本次代码检查通过 49 项 Python 回归和 7 项浏览器逻辑测试，后续数量以实际运行输出为准。ESP32 实物网络联调和真实跳绳识别精度仍待验证；检测波峰不能直接当作真实跳绳次数或动作标注。
