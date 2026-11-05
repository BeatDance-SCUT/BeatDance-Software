# BeatDance — IMU 跳绳运动监测软件

采集页面新增大字“采集时间”，显示最近已保存样本的 CSV `time`、批次号和启动片段。录视频时先拍清屏幕，再连续拍动作，末尾再拍一次，便于粗对齐；约 0.5 秒的页面刷新周期及网络、渲染延迟不适合直接作为逐次动作真值，需结合明显动作校准。断流或停止时读数保留在最后样本。

## Infra v1 开发工作台

新增独立入口，覆盖采集、数据管理、人工标注、离线实验和结果对比：

```powershell
.\.venv\Scripts\python -m streamlit run infra_app.py
```

“数据”页可一键创建明确标记的合成六轴示例、标注和数据集，完成无硬件流程验证。支持标注版本、固定数据集引用、参数实验、事件匹配评估、失败片段定位及 HTML／CSV／JSON 导出。完整演示、CLI 和数据契约见 [Infra 使用说明](docs/INFRA.md)。原有 `app.py` 入口保持可用，两者不可同时占用同一 UDP 端口。

以下介绍原有监测应用。

软件接收 ESP32 的六轴 IMU UDP v1 JSON，**先保存 CSV，再从 CSV 读取并显示**。支持实时曲线、加速度滤波、波峰分析、Δt–t 间隔图、批次归档与历史下载。

## 快速启动

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m streamlit run app.py
```

已有环境可直接执行最后一条。默认显示模拟分析 CSV；在侧栏切换“UDP 实时采集”可开始硬件采集，切换“历史采集”可回看和下载。

三种模式的分析区均显示 Δt–t 图：横轴为后一个波峰的时间，纵轴为相邻波峰间隔（秒），虚线为当前视图的平均间隔。“间隔数据（当前视图）”可查看和下载对应 CSV。实时和历史回看按侧栏时间窗口显示；分析 CSV 模式显示上传文件的全部分析数据。

硬件与电脑需要连接可互通的同一 Wi-Fi。硬件固件中的 `PC_IP` 设为本机局域网 IPv4，`PC_PORT` 与软件端口一致（默认 `5005`）。完整步骤及排查见 [运行说明](docs/RUNNING.md)。

## 文件放在哪里

| 位置 | 内容 |
| --- | --- |
| `app.py` | 网页启动入口 |
| `beatdance/` | UDP 接收、CSV 处理、信号分析和统一路径 |
| `scripts/` | 模拟信号与噪声基准脚本 |
| `tests/` | UDP、CSV、算法和界面回归 |
| `data/examples/` | 版本管理的模拟 CSV 示例 |
| `data/recordings/` | 按 UTC 日期和唯一批次保存采集数据 |
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

已实现模拟信号分析、UDP v1 协议接入、CSV 持久化、实时显示和历史归档，并有本机 UDP 与界面回归测试。ESP32 实物网络联调和真实跳绳识别精度仍待验证；检测波峰不能直接当作真实跳绳次数或动作标注。
