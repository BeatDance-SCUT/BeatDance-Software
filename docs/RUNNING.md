# 运行与验证

## 录视频时对时

原应用的“UDP 实时采集”和工作台的“采集”页均显示大字采集时间。先开始采集并确认收到数据，再用视频拍清批次、片段号和秒数，连续拍摄动作，结束前再拍一次屏幕。显示值取自最近已保存样本的 `time`，不是点击开始后的电脑秒表；无样本显示等待，断流或停止后不继续走时，设备重启后的新片段重新从零计时。

若视频第 V 秒拍到采集时间 D 秒，初步换算为 `数据时间 = 视频时间 + (D - V)`，每个启动片段分别对齐。页面约每 0.5 秒刷新，加上网络和渲染延迟，小数三位不表示毫秒同步精度。为检查逐次漏计／多计，请额外录下佩戴传感器的手明显晃动等共同事件，再用视频与波形校准；同步动作排除在正式计数区间外。

## 开发工作台（Infra v1）

在软件仓库根目录运行 `.\.venv\Scripts\python -m streamlit run infra_app.py`。
通过“采集／数据／标注／实验／对比”完成开发流程；首次使用可在“数据”创建合成示例。标注、数据集及实验结果保存在本地独立目录，默认不入 Git，需另行备份。完整流程与命令行见 [INFRA.md](INFRA.md)。

以下原有应用说明中的“12 项”是早期环境验证记录；当前回归以实际运行 `unittest` 的输出为准。

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

遇到“等待 UDP 数据”，检查硬件 `PC_IP`、端口、Wi-Fi 客户端隔离和 Windows 防火墙的 Python/UDP 入站权限。`app.py`、`infra_app.py` 的采集页和硬件仓库的 `imu_udp_plot.py` 三者不能同时占用同一 UDP 端口；端口被占用会显示错误。排查端口占用：

```powershell
netstat -ano | findstr :5005
```

## 不用硬件复现接收问题

没有硬件时，可以把已保存的采集按原始时间节奏重新发成本机 UDP 报文，反复验证接收与显示：

```powershell
# 默认取最新一次采集，按 10 倍速发到 127.0.0.1:5005
.\.venv\Scripts\python -m scripts.replay_capture --speed 10

# 指定批次与端口；--loops 3 表示连续发三遍
.\.venv\Scripts\python -m scripts.replay_capture --source data/recordings/2026-11-04/121723_291114_4da4bcc3 --port 5006 --loops 3
```

回放用的是与固件相同的 UDP v1 报文，因此界面里的“已写入 CSV”“检测波峰”“Δt–t”都应按原始采集的节奏变化。先启动“开始新采集”，再运行回放脚本。

接收端同时兼容两代固件报文：扁平格式（`ax/ay/az/gx/gy/gz` + `t_us`）与旧版嵌套格式（`acc_g/gyro_dps` + `t_ms`）。`seq` 按 uint32 校验，设备连续运行超过约 11 分钟也不会因序号超出 uint16 而整包被丢弃。

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

## 核对检出次数与人工计数

单组即时核对，不给人工计数也能看结构：

```powershell
.\.venv\Scripts\python -m scripts.truth_tools quick --capture 2026-11-04/125933_337845_8e4eafd3 --blocks 10 --plot data/results/count_check/a.png
```

输出里三个数要分开看：**全段峰**含起止静置的假峰，**检出**已剔除静置（人的计数只数跳的），**相位法**是对带通信号积 Hilbert 相位的独立估计。`--blocks` 每段计数可与视频分段对齐；`--sweep` 扫阈值与最小间隔看灵敏度。

注意相位法的定位：实测它相对人工计数系统性高估约 12%（手腕处一次跳跃会产生不止一个振荡周期），所以它只能用于发现「整段漏检」，不能当准确率基准，也不能拿两法接近就宣布算法准。

多组批量比对，把人工计数填进 `data/count_truth.csv` 的 `actual` 列：

```powershell
.\.venv\Scripts\python -m scripts.truth_tools score --truth-file data/count_truth.csv
```

会给出每组「实际 / 检出 / 相位法 / 差 / 误差」、平均绝对误差与偏差方向。

参数标定（有真值后再做，避免凭手感调参）：

```powershell
.\.venv\Scripts\python -m scripts.truth_tools calibrate --truth-file data/count_truth.csv
```

它会在突出度 × 最小峰间隔的网格上排名，同时打印当前默认参数的 MAE，差距小于 2 个百分点时建议不动默认值。2026-11-04 三组手腕单摇的结论：默认（0.7 m/s² / 0.40 s）MAE 5.5%、偏差 +1.7 次；改为 10.0 / 0.40 可到 3.9%，但那是三组拟合出来的，换人前不要当通用值。

注意采样率：`quick` 直接读 `imu_raw.csv`，不经过 infra 的 50 Hz 契约，所以 100 Hz 的真实采集也能用。

## 回归验证

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

测试通过本机 UDP socket 发送合成数据，覆盖 JSON→CSV→显示、独立批次、异常报文、序号缺口/回绕、完整分析和界面启停。测试文件写入 `.tmp/` 下的临时目录，不进入正式采集目录。

这类验证不替代 ESP32 实物网络联调，也不验证真实跳绳识别精度。波峰数量是算法输出，不能直接当作真实动作标注。
