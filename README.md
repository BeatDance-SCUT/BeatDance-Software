# IMU-Based Jump Rope Monitoring System
# 基于 IMU 的跳绳监测系统

---

## 1. Project Overview | 项目介绍

This project aims to develop an IMU-based jump rope monitoring system. It uses motion sensor data to analyze movement patterns and estimate exercise frequency.

The software component is responsible for signal processing, peak detection, frequency estimation, and data visualization.

本项目旨在开发一套基于 IMU（惯性测量单元）的跳绳监测系统，通过采集运动传感器数据，分析运动模式并估计运动频率。

软件部分主要负责信号处理、波峰检测、频率估计以及数据可视化。

## 2. Main Features | 主要功能

- **Signal Processing | 信号处理：** Apply a low-pass Butterworth filter to reduce noise.
- **Peak Detection | 波峰检测：** Detect peaks in the filtered acceleration signal.
- **Frequency Estimation | 频率估计：** Estimate movement frequency based on the time intervals between adjacent peaks.
- **Data Visualization | 数据可视化：** Display raw and filtered signals, detected peaks, and statistical results.
- **Interactive Dashboard | 交互式仪表盘：** Use Streamlit to provide a web-based visualization interface.

## 3. Project Structure | 项目结构

| File / Folder 文件或文件夹 | Description 说明 |
|---|---|
| `app.py` | Streamlit 网页可视化界面 |
| `peak_detection.py` | 信号滤波与波峰检测 |
| `test_algorithm.py` | 算法性能测试 |
| `analysis_result.csv` | 信号处理结果数据 |
| `test_results.csv` | 算法测试结果 |
| `noise_test_results.png` | 噪声测试结果图 |
| `个人工作（数据，代码ect）/` | 个人工作及相关代码 |
| `项目记录（参考文献）/` | 项目记录与参考文献 |

## 4. Environment Setup | 环境配置

Install the required Python packages:

安装项目所需的 Python 库：

```bash
pip install numpy pandas scipy matplotlib streamlit
```

## 5. How to Run | 运行方法

Run the signal processing script:

运行信号处理程序：

```bash
python peak_detection.py
```

Start the web dashboard:

启动网页可视化界面：

```bash
streamlit run app.py
```

Then open the local URL displayed in the terminal.

随后在浏览器中打开终端显示的本地网址。

## 6. Current Development Status | 当前开发进度

- Completed initial signal processing using simulated IMU data.
  - 已完成基于模拟 IMU 数据的初步信号处理。
- Implemented low-pass filtering and peak detection.
  - 已实现低通滤波与波峰检测。
- Completed preliminary algorithm tests under different noise levels.
  - 已完成不同噪声水平下的初步算法测试。
- Developed an initial Streamlit visualization dashboard.
  - 已完成初版 Streamlit 网页可视化界面。
- Real-world IMU data validation and hardware integration are still in progress.
  - 真实 IMU 数据验证与硬件集成仍在进行中。

## 7. Notes | 注意事项

Current algorithm results are based on simulated data. Performance with real jump rope motion data has not yet been verified.

目前的算法测试结果基于模拟数据，尚未通过真实跳绳运动数据验证其实际性能。