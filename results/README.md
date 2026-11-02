# 算法结果

| 目录 | 内容 |
| --- | --- |
| `algorithm_baseline/` | 原仓库已有的 `test_results.csv` 与 `noise_test_results.png`，原样保留 |
| `algorithm_runs/唯一批次/` | 新运行噪声基准得到的 CSV 和图片，默认不进入 Git |
| `simulations/唯一批次/` | 新运行模拟信号得到的分析 CSV 和信号图片，默认不进入 Git |

从仓库根目录执行 `python -m scripts.benchmark_algorithm` 或 `python -m scripts.simulate_imu`，默认每次创建独立结果目录。需要展示或归档到 Git 的实验结果，可挑选后另建有明确说明的目录。

这里保存模拟和基准结果；ESP32 采集仍保存在 `data/recordings/`。原有基准结果并非实物跳绳识别精度证明。
