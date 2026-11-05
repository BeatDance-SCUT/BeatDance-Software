# 仓库文件导航

## Infra v1 新增位置

| 位置 | 职责 |
| --- | --- |
| `infra_app.py` | 独立 Streamlit 开发工作台入口，保留原 `app.py` |
| `beatdance/capture_clock.py` | 两个采集入口共用的视频对时时间显示 |
| `infra/store.py` | 原始采集校验、导入、标注版本与数据集快照 |
| `infra/evaluation.py` | 按有效区域的一对一匹配与指标 |
| `infra/experiments.py` | UI／CLI 共用的后台实验、互斥、取消、报告与导出 |
| `infra/workbench.py` | 采集、数据、标注、实验、对比、视频复核六个页面 |
| `infra/video_review.py`、`infra/video_review.html` | 视频复核页、对齐映射、视频驱动的波形游标与人工复核记录 |
| `tests/test_video_review.py`、`tests/video_review_browser.test.cjs` | 复核数据保真、页面及浏览器同步逻辑回归 |
| `infra/demo.py`、`data/examples/infra_synthetic/` | 明确标识的合成六轴和构造真值 |
| `data/annotations/`、`data/datasets/` | 本地不可变版本与快照，默认不入 Git |
| `results/experiments/` | 独立实验目录及进程级工作锁，默认不入 Git |
| `tests/test_infra.py` | 数据契约、评估、实验、命令行及页面回归 |
| `docs/INFRA.md` | 工作台操作和文件契约 |

`beatdance/signal_processing.py` 新增可选 `AlgorithmConfig`，默认保持既有算法结果；其余原入口和历史材料保留。以下文件树展示原有应用的基础结构。

## 目录职责

```text
BeatDance-Software/
├── app.py                         # Streamlit 启动入口与页面
├── README.md                      # 项目首页
├── requirements.txt               # Python 依赖
├── .gitignore                     # 本地数据与生成文件规则
├── beatdance/                     # 可复用业务模块
│   ├── __init__.py
│   ├── paths.py                    # 统一的仓库数据与结果路径
│   ├── imu_udp.py                  # UDP 校验、接收和原始 CSV 保存
│   ├── recordings.py               # CSV 读取和完整派生分析
│   └── signal_processing.py        # 滤波与波峰分析
├── scripts/                       # 用 python -m scripts.<名称> 运行
│   ├── __init__.py
│   ├── simulate_imu.py             # 模拟信号与示例分析生成
│   └── benchmark_algorithm.py      # 不同噪声下的算法基准
├── tests/
│   └── test_udp_integration.py     # UDP、CSV、算法和界面回归
├── data/
│   ├── README.md
│   ├── examples/analysis_result.csv
│   └── recordings/                # 按日期和批次归档真实采集
├── results/
│   ├── README.md
│   ├── algorithm_baseline/         # 原有基准 CSV 与结果图
│   ├── algorithm_runs/             # 新基准结果，按运行批次自动创建
│   └── simulations/                # 新模拟结果，按运行批次自动创建
└── docs/
    ├── RUNNING.md                  # 安装、启动、采集和验证
    ├── DATA_FORMAT.md              # 字段、单位和机器学习使用说明
    ├── REPOSITORY_LAYOUT.md        # 本文档
    └── reports/
        ├── README.md              # 原报告和截图的逐项索引
        ├── phase_1/               # 第一阶段中英文报告与两张截图
        └── phase_2/               # 第二阶段中英文报告与两张截图
```

`.venv/` 是本地 Python 环境；`.tmp/` 是测试和工具临时目录；`__pycache__/` 是 Python 自动生成缓存。它们不进入 Git。`.git/` 由 Git 自身管理。

## 2026-11-02 路径迁移

| 原位置 | 当前位置 |
| --- | --- |
| `imu_udp.py` | `beatdance/imu_udp.py` |
| `recordings.py` | `beatdance/recordings.py` |
| `signal_processing.py` | `beatdance/signal_processing.py` |
| `peak_detection.py` | `scripts/simulate_imu.py` |
| `test_algorithm.py` | `scripts/benchmark_algorithm.py` |
| `analysis_result.csv` | `data/examples/analysis_result.csv` |
| `test_results.csv` | `results/algorithm_baseline/test_results.csv` |
| `noise_test_results.png` | `results/algorithm_baseline/noise_test_results.png` |
| `个人工作（数据，代码ect）/` 中的第一阶段报告和 P1 截图 | `docs/reports/phase_1/`，文件名不变 |
| `个人工作（数据，代码ect）/` 中的第二阶段报告和 P2 截图 | `docs/reports/phase_2/`，文件名不变 |
| 新采集默认路径 `recordings/` | `data/recordings/` |

整理时没有发现已有的根目录 `recordings/` 采集数据，因此无需迁移旧采集。若之后从其他副本导入旧采集，把原有“日期/批次”层级放入 `data/recordings/` 即可由历史页面识别。

业务模块导入统一使用 `beatdance.*`；路径统一由 `beatdance/paths.py` 提供，不依赖启动时的工作目录。原有报告、截图、模拟 CSV 和基准文件仅迁移位置；脚本输出调整为独立结果批次，避免污染根目录或覆盖基准。
