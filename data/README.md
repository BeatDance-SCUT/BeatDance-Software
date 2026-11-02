# 数据目录

| 位置 | 用途 | Git 管理 |
| --- | --- | --- |
| `examples/analysis_result.csv` | 原项目的模拟分析示例，供默认仪表盘展示 | 跟踪 |
| `recordings/日期/批次/imu_raw.csv` | 一次采集的原始六轴样本 | 忽略，需单独备份 |
| `recordings/日期/批次/session.json` | 参与者、动作标签、备注、单位和质量统计 | 忽略，需单独备份 |
| `recordings/日期/批次/analysis_*.csv` | 可重新生成的派生分析 | 忽略，按需备份 |

采集批次由程序自动创建，不把模拟示例、测试数据和真实采集混放。`recordings/.gitkeep` 仅保留空目录结构。

机器学习以原始 CSV 和批次元信息作为底稿。完整字段、单位、标签和切分建议见 [数据格式](../docs/DATA_FORMAT.md)。
