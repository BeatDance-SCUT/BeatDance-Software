# 运行与验证

以下命令均在软件仓库根目录运行。已验证环境为 Python 3.14.7，依赖取当前最新稳定版，`requirements.txt` 只写下限、不设上限，回归测试 12 项全部通过。

## 安装和启动

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m streamlit run app.py
```

已安装依赖时，只需执行最后一条。打开终端显示的本地网址。

依赖始终跟随最新稳定版，需要更新时执行：

```powershell
.\.venv\Scripts\python -m pip install --upgrade -r requirements.txt
```

升级依赖后建议先跑一遍回归验证。上游发布破坏性大版本（例如 pandas 主版本变更）时，如出现异常再按需回退，不要直接修改本地已装版本了事。

默认“分析 CSV”读取 `data/examples/analysis_result.csv`，该文件是原项目的模拟分析数据，也可上传具有同样列的分析 CSV。

## UDP 实时采集

1. 电脑与 ESP32 连接可互通的同一 Wi-Fi，ESP32 使用 2.4 GHz。
2. 硬件固件 `BeatDance_IMU_UDP.ino` 的 `PC_IP` 设为本机当前局域网 IPv4，`PC_PORT` 默认 `5005`。软件监听地址 `0.0.0.0` 不能作为硬件的目的地址。
3. 在侧栏选择“UDP 实时采集”，默认监听 `0.0.0.0:5005`，填写参与者编号、动作标签和采集备注。
4. 点“开始新采集”。软件将有效且顺序正确的样本逐行写入 CSV 并 flush，再每 0.5 秒从 CSV 读取数据更新曲线。
5. 点“停止并保存”，释放端口，并生成整次采集的合加速度分析。进入“历史采集”可以按批次回看、生成单轴分析和下载文件。

数据保存在 `data/recordings/UTC日期/唯一批次/`，每次开始使用新目录。原始六轴数据和派生分析分别保存。详见 [数据格式](DATA_FORMAT.md)。

遇到“等待 UDP 数据”，检查硬件 `PC_IP`、端口、Wi-Fi 客户端隔离和 Windows 防火墙的 Python/UDP 入站权限。硬件仓库的 `imu_udp_plot.py` 与本软件不能同时占用同一 UDP 端口；端口被占用会显示错误。

切换到其他数据模式会停止采集并生成分析。关闭/刷新浏览器造成会话失联后，接收器约 60 秒释放端口；之前写入的 CSV 保留。失联停止或进程异常退出后，可从历史页补生成分析。机器休眠、进程退出或浏览器长期暂停后台刷新都会影响持续采集。

## 模拟和噪声基准

```powershell
.\.venv\Scripts\python -m scripts.simulate_imu
.\.venv\Scripts\python -m scripts.benchmark_algorithm
```

模拟结果自动写入 `results/simulations/唯一批次/`，噪声基准结果写入 `results/algorithm_runs/唯一批次/`。每次默认使用新目录，保留 `data/examples/` 的示例与 `results/algorithm_baseline/` 的原始结果。两条命令均支持：

```powershell
.\.venv\Scripts\python -m scripts.simulate_imu --no-show --output-dir .tmp/simulation
.\.venv\Scripts\python -m scripts.benchmark_algorithm --no-show --output-dir .tmp/benchmark
```

`--no-show` 仅保存 CSV 和图片，适合自动验证。`--output-dir` 显式指定输出目录；重复使用同一指定目录会更新其中的同名结果。

## 回归验证

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

测试通过本机 UDP socket 发送合成数据，覆盖 JSON→CSV→显示、独立批次、异常报文、序号缺口/回绕、完整分析和界面启停。测试文件写入 `.tmp/` 下的临时目录，不进入正式采集目录。

这类验证不替代 ESP32 实物网络联调，也不验证真实跳绳识别精度。波峰数量是算法输出，不能直接当作真实动作标注。
