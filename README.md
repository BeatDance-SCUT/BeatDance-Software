# BeatDance — IMU 跳绳运动监测软件

采集页面新增大字“采集时间”，显示最近已保存样本的 CSV `time`、批次号和启动片段。录视频时先拍清屏幕，再连续拍动作，末尾再拍一次，便于粗对齐；约 0.5 秒的页面刷新周期及网络、渲染延迟不适合直接作为逐次动作真值，需结合明显动作校准。断流或停止时读数保留在最后样本。

## 正式采集与算法工作台

“视频复核”页支持选择本地视频、输入视频时刻与屏幕采集时间进行单点对齐（可选首尾两点校正漂移），并排同步播放视频和带计数事件的波形。支持慢放、按指定秒数步进、点击波形跳转、前后事件定位、人工疑似漏计／多计记录及 JSON／CSV 导出恢复；可下载独立 HTML 离线复核。详见 [视频复核操作](docs/INFRA.md#视频复核)。

唯一正式入口 `infra_app.py` 提供七个页面：采集、数据、标注、实验、对比、视频复核、算法实验台：

```powershell
.\.venv\Scripts\python -m streamlit run infra_app.py
```

“数据”页可一键创建明确标记的合成六轴示例、标注和数据集，完成无硬件流程验证。支持标注版本、固定数据集引用、参数实验、事件匹配评估、失败片段定位及 HTML／CSV／JSON 导出。完整演示、CLI 和数据契约见 [Infra 使用说明](docs/INFRA.md)。旧 `app.py` 入口已移除；采集统一使用工作台的“采集”页。

**Loss Longer（`loss_longer`）是当前唯一跳绳计数算法。** 实时采集、停止后分析、视频复核和实验共享设备时间逐包计数核心。算法实验台用于比较 Loss Longer 参数，支持一维扫描、事件配对与视频同步复核。详见 [算法说明](docs/LOSS_LONGER.md) 与 [计数实验台](docs/COUNTING_LAB.md)。保存的历史实验仍可浏览和对比。

采集与后续离线复核均在同一工作台完成。

软件接收 ESP32 的六轴 IMU UDP v1 JSON，先保存 CSV，再逐包更新 Loss Longer 计数。采集页显示实时六轴曲线及累计计数；停止后可在数据页浏览历史记录、在视频复核页复核计数事件，并在算法实验台进行离线对比。

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
| `beatdance/counting.py` | Loss Longer 计数工厂和回放接口 |
| `beatdance/` | UDP 接收、CSV 处理、信号分析和统一路径 |
| `scripts/` | UDP 回放、质量检查及真值工具 |
| `tests/` | UDP、CSV、算法和界面回归 |
| `data/examples/` | 版本管理的模拟 CSV 示例 |
| `data/recordings/` | 按 UTC 日期和唯一批次保存采集数据 |
| `results/experiments/` | 每配置的独立实验、事件、评分与来源 |
| `results/counting_batches/`、`results/counting_reviews/` | 批次问题与配置、复盘结论及报告；本地备份并检查 Git 状态 |
| `results/algorithm_baseline/` | 原项目的算法结果与结果图 |
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

# 本机 UDP 回放
.\.venv\Scripts\python -m scripts.replay_capture --speed 10
```

## 验证范围

当前已有三段开发录像逐包回放、本机 UDP 和界面流程验证。实物端到端延迟与独立留出录像准确率待验证。
