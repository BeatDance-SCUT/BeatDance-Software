"""Real-data validation helpers: capture QC, truth-to-annotation conversion, reviewer agreement.

The infra workbench can only accept action timestamps typed into a table by hand.
A 60 s jump-rope round holds one to two hundred jumps, so truth has to come from a
recording and be converted into the annotation JSON the workbench imports.

Clock convention: the device clock (`time` in `imu_raw.csv`) starts at zero for each
boot segment, while a phone video starts at an arbitrary wall-clock instant. Every
truth timestamp is therefore expressed in video/audio seconds and shifted by one
offset::

    device_time = truth_time + offset

Measure the offset from one deliberate, unambiguous event visible in both streams
(a hand clap or a stomp): `offset = device_time_of_event - truth_time_of_event`.
Use `--sync VIDEO DEVICE` to pass the pair directly, or `--offset` once it is known.

Commands::

    python -m scripts.truth_tools qc --capture 2026-11-03/20261103T101500_abcdef1234
    python -m scripts.truth_tools annotate --capture KEY --truth jumps.csv --sync 12.35 3.10
    python -m scripts.truth_tools compare --a reviewer_a.csv --b reviewer_b.csv

Nothing here writes into `data/recordings`; the only output is an annotation JSON
file that the workbench "导入标注 JSON" button accepts.
"""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

import numpy as np

from infra.store import Store, number
from beatdance.paths import PROJECT_ROOT
from beatdance.signal_processing import STANDARD_GRAVITY, AlgorithmConfig, analyze_acceleration

TARGET_FS = 50.0
FS_TOLERANCE = 0.05
MAX_GAP_SECONDS = 0.2
MAX_GAP_RATIO = 0.01
TIME_HEADERS = ("time_s", "time", "t", "seconds", "sec", "device_time")


def read_times(path):
    """Read a truth sheet: one timestamp column, header optional, # starts a comment."""
    rows = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        column = None
        for line_number, row in enumerate(reader, 1):
            cells = [c.strip() for c in row]
            if not cells or not any(cells) or cells[0].startswith("#"):
                continue
            if column is None:
                lowered = [c.lower() for c in cells]
                hit = next((i for i, c in enumerate(lowered) if c in TIME_HEADERS), None)
                if hit is not None:
                    column = hit
                    continue
                column = 0
            raw = cells[column] if column < len(cells) else ""
            try:
                value = number(raw, f"line {line_number}")
            except (TypeError, ValueError):
                raise ValueError(f"{path}:{line_number}: not a numeric timestamp: {raw!r}") from None
            rows.append(value)
    if not rows:
        raise ValueError(f"{path}: no timestamps found")
    return rows


def segment_bounds(frame):
    return {int(s): (float(g.time.min()), float(g.time.max())) for s, g in frame.groupby("segment")}


def capture_summary(frame, meta):
    segments = []
    for seg, group in frame.groupby("segment"):
        times = group.time.to_numpy()
        deltas = [b - a for a, b in zip(times, times[1:])]
        gaps = [d for d in deltas if d > MAX_GAP_SECONDS]
        duration = float(times[-1] - times[0]) if len(times) > 1 else 0.0
        segments.append({
            "segment": int(seg),
            "rows": int(len(times)),
            "duration_seconds": duration,
            "fs_median": 1.0 / statistics.median(deltas) if deltas else None,
            "gap_count": len(gaps),
            "gap_seconds": sum(gaps),
            "gap_ratio": sum(gaps) / duration if duration else None,
        })
    duration = sum(s["duration_seconds"] for s in segments)
    rows = sum(s["rows"] for s in segments)
    # Rate from the per-segment median interval: a dropout shrinks the row count,
    # so dividing rows by duration would penalise the same gap twice.
    rated = [(s["fs_median"], s["rows"]) for s in segments if s["fs_median"]]
    fs = (sum(rate * weight for rate, weight in rated) / sum(w for _, w in rated)) if rated else None
    gap_seconds = sum(s["gap_seconds"] for s in segments)
    gap_ratio = gap_seconds / duration if duration > 0 else None
    checks = {
        "single_segment": len(segments) == 1,
        "sample_rate": fs is not None and abs(fs - TARGET_FS) / TARGET_FS <= FS_TOLERANCE,
        "gap_ratio": gap_ratio is not None and gap_ratio <= MAX_GAP_RATIO,
        "duration": duration >= 10.0,
    }
    return {
        "capture_id": meta.get("capture_id"),
        "status": meta.get("status"),
        "synthetic": bool(meta.get("synthetic")),
        "rows": rows,
        "duration_seconds": duration,
        "effective_fs": fs,
        "gap_seconds": gap_seconds,
        "gap_ratio": gap_ratio,
        "segments": segments,
        "checks": checks,
        "verdict": "PASS" if all(checks.values()) else "REJECT",
    }


def command_qc(args):
    store = Store(args.root)
    frame, meta, hashes = store.capture(args.capture)
    summary = capture_summary(frame, meta)
    summary["raw_sha256"] = hashes["raw_sha256"]
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(f"采集 {args.capture}")
    print(f"  状态      {summary['status']}        {'合成数据（不可作为精度证据）' if summary['synthetic'] else '真实数据'}")
    print(f"  样本数    {summary['rows']}")
    print(f"  总时长    {summary['duration_seconds']:.2f} s")
    fs = summary["effective_fs"]
    print(f"  实际采样率 {fs:.2f} Hz" if fs else "  实际采样率 未知")
    print(f"  缺口      {summary['gap_seconds']:.3f} s "
          f"({(summary['gap_ratio'] or 0) * 100:.2f}%)")
    for seg in summary["segments"]:
        print(f"    片段 {seg['segment']}: {seg['rows']} 行, {seg['duration_seconds']:.2f} s, "
              f"{seg['gap_count']} 处缺口")
    print("  判定项")
    labels = {"single_segment": "单一启动片段", "sample_rate": f"采样率 {TARGET_FS:.0f}±5%",
              "gap_ratio": f"缺口占比 ≤{MAX_GAP_RATIO * 100:.0f}%", "duration": "时长 ≥10 s"}
    for name, ok in summary["checks"].items():
        print(f"    {'通过' if ok else '不通过'}  {labels[name]}")
    print(f"  结论 {summary['verdict']}")
    if summary["verdict"] == "REJECT":
        print("  该轮不满足采集质量门禁，建议重采而不是调算法参数。")


def build_annotation(frame, hashes, capture_key, times, args):
    bounds = segment_bounds(frame)
    segment = args.segment
    if segment not in bounds:
        raise ValueError(f"segment {segment} not in capture; available: {sorted(bounds)}")
    low, high = bounds[segment]
    device_times = []
    for value in times:
        shifted = value + args.offset
        if low <= shifted <= high:
            device_times.append(round(shifted, 6))
    device_times = sorted(set(device_times))
    if not device_times:
        raise ValueError("no truth event falls inside the capture segment; check --offset")
    if args.valid:
        intervals = []
        for text in args.valid:
            start_text, _, end_text = text.partition(":")
            start, end = number(start_text, "valid start"), number(end_text, "valid end")
            if not low <= start < end <= high:
                raise ValueError(f"valid range {text} outside segment bounds {low:.3f}..{high:.3f}")
            intervals.append({"segment": segment, "start": start, "end": end, "kind": "valid"})
    else:
        intervals = [{"segment": segment, "start": low, "end": high, "kind": "valid"}]
    return {
        "schema_version": 1,
        "capture_key": capture_key,
        "raw_sha256": hashes["raw_sha256"],
        "events": [{"segment": segment, "time": t} for t in device_times],
        "intervals": intervals,
        "notes": args.notes,
        "truth_source": {
            "kind": args.source,
            "offset_seconds": args.offset,
            "events_before_filter": len(times),
            "events_dropped": len(times) - len(device_times),
        },
    }


def command_annotate(args):
    if args.sync and args.offset is not None:
        raise ValueError("use either --sync or --offset, not both")
    if args.sync:
        video, device = (number(v, "sync") for v in args.sync)
        args.offset = device - video
    if args.offset is None:
        raise ValueError("--sync VIDEO DEVICE or --offset is required to align clocks")
    store = Store(args.root)
    frame, _, hashes = store.capture(args.capture)
    value = build_annotation(frame, hashes, args.capture, read_times(args.truth), args)
    output = Path(args.out) if args.out else Path(f"{args.capture.replace('/', '_')}_annotation.json")
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"偏移       {args.offset:+.3f} s（device = truth + offset）")
    print(f"真值事件   {len(value['events'])} 个（原始 {value['truth_source']['events_before_filter']}，"
          f"落在片段外剔除 {value['truth_source']['events_dropped']}）")
    span = value["events"][-1]["time"] - value["events"][0]["time"]
    if span > 0:
        print(f"平均间隔   {span / (len(value['events']) - 1):.3f} s "
              f"≈ {60 * (len(value['events']) - 1) / span:.1f} 次/分")
    print(f"评分区间   {[(round(i['start'], 2), round(i['end'], 2)) for i in value['intervals']]}")
    print(f"已写入     {output}")
    print("下一步：工作台「标注」页 → 导入标注 JSON → 保存为新标注版本。")


def save_plot(path, times, acceleration, analysis, peaks, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, (top, bottom) = plt.subplots(2, 1, figsize=(11, 6), sharex=True,
                                         gridspec_kw={"height_ratios": [2, 1]})
    top.plot(analysis.time, analysis.raw_acceleration, color="#9aa0a6", linewidth=0.8, label="raw")
    top.plot(analysis.time, analysis.filtered_acceleration, color="#1a73e8", linewidth=1.2, label="filtered")
    top.plot(peaks.time, peaks.filtered_acceleration, "v", color="#d93025", markersize=6, label="peak")
    top.set_ylabel("m/s²")
    top.set_title(title)
    top.legend(loc="upper right", fontsize=8)
    if len(peaks) > 1:
        lag = np.diff(peaks.time)
        bottom.plot(peaks.time[1:], lag, "o-", color="#188038", markersize=3, linewidth=0.8)
    bottom.set_xlabel("Device-relative time (s)")
    bottom.set_ylabel("Δt (s)")
    bottom.grid(alpha=0.3)
    figure.tight_layout()
    figure.savefig(path, dpi=130)
    plt.close(figure)


def capture_dir(root, key):
    """Resolve a capture key such as 2026-11-04/125933_337845_8e4eafd3."""
    recordings = (Path(root) / "data" / "recordings").resolve()
    path = (recordings / key).resolve()
    if not path.is_relative_to(recordings) or any(part in (".", "..") for part in Path(key).parts):
        raise ValueError("capture path escapes recordings")
    if not (path / "imu_raw.csv").is_file():
        raise ValueError(f"找不到采集 {key}：应为 data/recordings 下的 日期/批次 目录")
    return path


def load_capture(root, key):
    """Read a capture with the beatdance loader, tolerating both CSV schemas."""
    from beatdance.recordings import load_raw_csv

    path = capture_dir(root, key)
    frame = load_raw_csv(path / "imu_raw.csv")
    metadata_path = path / "session.json"
    meta = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    return path, frame, meta


def magnitude(segment):
    return np.sqrt(segment.ax_g ** 2 + segment.ay_g ** 2 + segment.az_g ** 2).to_numpy() * STANDARD_GRAVITY


def build_config(prominence=None, min_interval=None, cutoff=None):
    """Overlay only the parameters the caller actually set on the defaults."""
    overrides = {}
    if prominence is not None:
        overrides["prominence"] = prominence
    if min_interval is not None:
        overrides["min_peak_interval"] = min_interval
    if cutoff is not None:
        overrides["cutoff_hz"] = cutoff
    return AlgorithmConfig(**overrides)


def detect(times, acceleration, config):
    """Run one configuration and return the analysis frame plus its peaks."""
    analysis = analyze_acceleration(times, acceleration, config=config)
    return analysis, analysis[analysis.is_peak == 1]


def active_peaks(analysis, peaks, *, window=0.5, min_std=1.0):
    """Drop peaks that sit in a near-static stretch (before start / after stop).

    A human count only covers the jumping, so rest-period ripples must not be
    counted. Each peak keeps only if the raw signal around it actually varies.
    """
    if peaks.empty or analysis.empty:
        return peaks
    times = analysis.time.to_numpy()
    values = analysis.raw_acceleration.to_numpy()
    step = float(np.median(np.diff(times))) if len(times) > 1 else 0.01
    half = max(1, int(round(window / step / 2)))
    keep = []
    for moment in peaks.time.to_numpy():
        centre = int(np.searchsorted(times, moment))
        low, high = max(0, centre - half), min(len(values), centre + half + 1)
        keep.append(bool(np.std(values[low:high]) >= min_std))
    return peaks[np.asarray(keep, dtype=bool)]


def phase_cycles(times, acceleration, *, low_hz=1.2, high_hz=4.5, min_std=1.0):
    """Independent count: integrate the Hilbert phase of a band-passed signal.

    Peak picking can miss or double a cycle; this method only counts how many
    full oscillations the waveform completed while the person was moving, so
    agreement between the two is evidence the count is not a detection artifact.
    """
    from scipy.signal import butter, hilbert, sosfiltfilt

    times = np.asarray(times, dtype=float)
    values = np.asarray(acceleration, dtype=float)
    if times.size < 50:
        raise ValueError("too few samples for a phase estimate")
    step = float(np.median(np.diff(times)))
    fs = 1.0 / step
    if high_hz >= fs / 2:
        raise ValueError("band-pass upper edge must stay below Nyquist")
    sos = butter(2, [low_hz, high_hz], btype="bandpass", fs=fs, output="sos")
    band = sosfiltfilt(sos, values)
    phase = np.unwrap(np.angle(hilbert(band)))

    half = max(1, int(round(0.5 / step / 2)))
    window = np.lib.stride_tricks.sliding_window_view(values, 2 * half + 1)
    spread = window.std(axis=1)
    moving = np.zeros(len(values), dtype=bool)
    moving[half:half + len(spread)] = spread >= min_std

    if not moving.any():
        return 0, 0.0
    indices = np.flatnonzero(moving)
    # Only the longest continuous moving stretch, so separate rests do not join.
    breaks = np.flatnonzero(np.diff(indices) > 1) + 1
    runs = np.split(indices, breaks)
    run = max(runs, key=len)
    if len(run) < 2:
        return 0, 0.0
    cycles = (phase[run[-1]] - phase[run[0]]) / (2 * np.pi)
    return int(round(cycles)), float(times[run[-1]] - times[run[0]])


def clean_note(text):
    return " ".join(str(text or "").split())


def command_quick(args):
    """Fast post-round check: detected count and rate against a human count."""
    _, frame, meta = load_capture(args.root, args.capture)
    segment = frame[frame.segment == args.segment]
    if segment.empty:
        raise ValueError(f"segment {args.segment} not in capture")
    low, high = float(segment.time.min()), float(segment.time.max())
    if args.window:
        start_text, _, end_text = args.window.partition(":")
        low, high = max(low, number(start_text, "window start")), min(high, number(end_text, "window end"))
        if low >= high:
            raise ValueError("window is empty after clipping to the segment")
    segment = segment[(segment.time >= low) & (segment.time <= high)]
    times = segment.time.to_numpy()
    acceleration = magnitude(segment)

    print(f"采集     {args.capture}")
    labels = meta.get("labels") or {}
    if any(labels.values()):
        print(f"标签     参与者 {labels.get('participant') or '-'} · "
              f"动作 {labels.get('activity') or '-'} · 备注 {clean_note(labels.get('notes')) or '-'}")
    print(f"窗口     {low:.2f} – {high:.2f} s（{high - low:.1f} s，{len(segment)} 样本）")
    if args.truth_count:
        print(f"人工计数 {args.truth_count} 次")

    if args.sweep:
        grid = [AlgorithmConfig(prominence=p, min_peak_interval=interval)
                for p in (0.7, 2.0, 5.0, 10.0) for interval in (0.40, 0.30, 0.25)]
    else:
        grid = [build_config(args.prominence, args.min_interval, args.cutoff)]
    results = []
    for config in grid:
        analysis, peaks = detect(times, acceleration, config)
        lag = np.diff(peaks.time.to_numpy())
        results.append((config, analysis, peaks, lag))

    print()
    if args.sweep:
        print(f"{'阈值':>6} {'最小间隔':>8} {'全段峰':>6} {'剔除静置':>8} {'跳动内速率':>10} {'与人工差':>10}")
    for config, analysis, peaks, _ in results:
        jumping = active_peaks(analysis, peaks)
        count = len(jumping)
        active = float(jumping.time.max() - jumping.time.min()) if count > 1 else 0.0
        bpm = 60.0 * (count - 1) / active if active > 0 else float("nan")
        if args.sweep:
            diff = f"{count - args.truth_count:+d}" if args.truth_count else "—"
            rate_text = f"{bpm:.1f}/分" if bpm == bpm else "—"
            print(f"{config.prominence:>6.1f} {config.min_peak_interval:>8.2f} {len(peaks):>6} "
                  f"{count:>8} {rate_text:>10} {diff:>10}")
        elif args.truth_count:
            print(f"全段峰   {len(peaks)} 次（含起止静置的假峰）")
            print(f"检出     {count} 次  实际 {args.truth_count} 次  "
                  f"差 {count - args.truth_count:+d} 次  误差 "
                  f"{abs(count - args.truth_count) / args.truth_count * 100:.1f}%")
        else:
            print(f"全段峰   {len(peaks)} 次（含起止静置的假峰）")
            print(f"检出     {count} 次（未给 --truth-count，无法算误差）")
    print()

    config, analysis, peaks, _ = results[0]
    jumping = active_peaks(analysis, peaks)
    count = len(jumping)
    lag = np.diff(jumping.time.to_numpy())
    try:
        cycling, cycle_span = phase_cycles(times, acceleration)
    except (ValueError, np.linalg.LinAlgError):
        cycling, cycle_span = 0, 0.0
    if cycling:
        print(f"相位法   {cycling} 个周期（连续运动 {cycle_span:.1f} s），"
              f"与峰值法差 {count - cycling:+d} 次")
    if count > 1:
        active = float(jumping.time.max() - jumping.time.min())
        print(f"有效跳动 {active:.1f} s（首个峰到末个峰），速率 {60.0 * (count - 1) / active:.1f} 次/分")
    if len(lag):
        print(f"间隔     中位数 {np.median(lag) * 1000:.0f} ms，"
              f"最短 {lag.min() * 1000:.0f} ms，最长 {lag.max() * 1000:.0f} ms，"
              f"CV {np.std(lag) / np.mean(lag):.2f}")
    if len(lag) and lag.min() < 0.6 * np.median(lag):
        print(f"提示     最短间隔 {lag.min() * 1000:.0f} ms 不足中位数的 60%，"
              "可能是同一次跳跃的落地冲击与甩绳被拆成两峰。")
    if len(lag) and lag.max() > 2.0:
        print("提示     存在 >2 s 的间隔，检查中途停顿或漏检成串。")

    if args.blocks and count:
        print()
        print(f"分块计数（每 {args.blocks:g} 秒），用于和视频分段对齐")
        edges = np.arange(low, high + args.blocks, args.blocks)
        per_block = np.histogram(jumping.time.to_numpy(), bins=edges)[0]
        for index, value in enumerate(per_block):
            if value:
                print(f"  {edges[index]:6.1f} – {min(edges[index + 1], high):6.1f} s  {value:>3} 次")

    if args.plot:
        save_plot(args.plot, times, acceleration, analysis, jumping,
                  f"{args.capture}  segment {args.segment}  {low:.1f}-{high:.1f}s")
        print(f"图已保存 {args.plot}")


def read_truth_counts(path):
    """Read a truth sheet: capture key plus the human count for that round."""
    rows = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: empty truth file")
        names = {name.strip().lower(): name for name in reader.fieldnames}
        if "capture" not in names:
            raise ValueError(f"{path}: 缺少 capture 列")
        count_key = next((names[k] for k in ("actual", "truth", "count", "实际") if k in names), None)
        if count_key is None:
            raise ValueError(f"{path}: 缺少 actual 列（人工计数）")
        for line, row in enumerate(reader, 2):
            key = (row.get(names["capture"]) or "").strip()
            raw = (row.get(count_key) or "").strip()
            if not key or key.startswith("#") or not raw:
                continue
            try:
                value = int(round(float(raw)))
            except ValueError:
                raise ValueError(f"{path}:{line}: 计数不是数字: {raw!r}") from None
            if value <= 0:
                raise ValueError(f"{path}:{line}: 计数必须为正: {raw!r}")
            rows.append((key, value))
    if not rows:
        raise ValueError(f"{path}: 没有可评估的行（actual 列还空着？）")
    return rows


def command_score(args):
    """Batch compare detected counts against human counts for many captures."""
    config = build_config(args.prominence, args.min_interval, args.cutoff)
    records = []
    for key, actual in read_truth_counts(args.truth_file):
        _, frame, meta = load_capture(args.root, key)
        segment = frame[frame.segment == frame.segment.iloc[-1]]
        analysis, peaks = detect(segment.time.to_numpy(), magnitude(segment), config)
        if not args.keep_rest:
            peaks = active_peaks(analysis, peaks)
        detected = len(peaks)
        active = float(peaks.time.max() - peaks.time.min()) if detected > 1 else 0.0
        try:
            cycling, _ = phase_cycles(segment.time.to_numpy(), magnitude(segment))
        except (ValueError, np.linalg.LinAlgError):
            cycling = None
        records.append({
            "capture": key,
            "activity": clean_note((meta.get("labels") or {}).get("activity")),
            "actual": actual,
            "detected": detected,
            "phase": cycling,
            "diff": detected - actual,
            "error": abs(detected - actual) / actual,
            "active_seconds": round(active, 2),
            "detected_rate": round(60.0 * (detected - 1) / active, 1) if active > 0 else None,
            "truth_rate": round(60.0 * (actual - 1) / active, 1) if active > 0 else None,
        })

    if args.json:
        print(json.dumps({"config": config.to_dict(), "rounds": records}, ensure_ascii=False, indent=2))
        return

    print(f"参数     阈值 {config.prominence:g} m/s² · 最小间隔 {config.min_peak_interval:g} s · "
          f"低通 {config.cutoff_hz:g} Hz")
    print()
    print(f"{'采集':<34}{'实际':>5}{'检出':>5}{'相位法':>7}{'差':>5}{'误差':>8}{'有效秒':>8}{'检出速率':>10}")
    for r in records:
        phase_text = f"{r['phase']}" if r["phase"] else "—"
        print(f"{r['capture'][:34]:<34}{r['actual']:>5}{r['detected']:>5}{phase_text:>7}{r['diff']:>+5d}"
              f"{r['error'] * 100:>7.1f}%{r['active_seconds']:>8.1f}"
              f"{(r['detected_rate'] or 0):>9.1f}/分")
    print()
    mean_error = statistics.mean(r["error"] for r in records)
    bias = statistics.mean(r["diff"] for r in records)
    agree = [abs(r["detected"] - r["phase"]) for r in records if r["phase"]]
    print(f"平均绝对误差 {mean_error * 100:.1f}%   平均偏差 {bias:+.1f} 次   组数 {len(records)}")
    if agree:
        mean_agree = statistics.mean(agree)
        phase_bias = statistics.mean(
            (r["phase"] - r["actual"]) / r["actual"] for r in records if r["phase"])
        print(f"两法差异     峰值法与相位法平均差 {mean_agree:.1f} 次；"
              f"相位法相对真值偏差 {phase_bias * 100:+.1f}%")
        if abs(phase_bias) <= 0.05:
            print("             相位法与真值接近，可作为第二参考。")
        else:
            print("             相位法自身有系统偏差（一次跳跃对应不止一个振荡周期），"
                  "只能用来发现整段漏检，不能当准确率基准。")
    if bias <= -3:
        print(f"结论     平均绝对误差 {mean_error * 100:.1f}%，系统性少计，"
              "优先怀疑最小间隔限制或阈值过高，而不是随机误差。")
    elif bias >= 3:
        print(f"结论     平均绝对误差 {mean_error * 100:.1f}%，系统性多计，"
              "检查是否把同一次跳跃的落地冲击与甩绳拆成了两个峰。")
    else:
        print(f"结论     平均绝对误差 {mean_error * 100:.1f}%，平均偏差 {bias:+.1f} 次，"
              "无系统方向，误差来自单次漏检/多检。")
    if len(records) < 5:
        print(f"备注     {len(records)} 组样本只能支撑初步结论；"
              "换人、换佩戴位置或改动作前应补采并重跑 calibrate。")


def command_calibrate(args):
    """Grid-search the peak parameters against known counts and rank them."""
    rounds = read_truth_counts(args.truth_file)
    data = []
    for key, actual in rounds:
        _, frame, _ = load_capture(args.root, key)
        segment = frame[frame.segment == frame.segment.iloc[-1]]
        data.append((key, segment.time.to_numpy(), magnitude(segment), actual))

    grid = []
    for prominence in args.prominence:
        for interval in args.interval:
            config = build_config(prominence, interval, args.cutoff)
            counts, errors = [], []
            for _, times, acceleration, actual in data:
                analysis, peaks = detect(times, acceleration, config)
                count = len(active_peaks(analysis, peaks))
                counts.append(count)
                errors.append(abs(count - actual) / actual)
            grid.append({"config": config.to_dict(), "counts": counts,
                         "mae": statistics.mean(errors), "bias": statistics.mean(
                             count - actual for count, (_, _, _, actual) in zip(counts, data))})
    grid.sort(key=lambda row: (row["mae"], abs(row["bias"])))

    current = {"config": AlgorithmConfig().to_dict(), "counts": [], "mae": 0.0, "bias": 0.0}
    errors = []
    for index, (_, times, acceleration, actual) in enumerate(data):
        analysis, peaks = detect(times, acceleration, AlgorithmConfig())
        count = len(active_peaks(analysis, peaks))
        current["counts"].append(count)
        errors.append(abs(count - actual) / actual)
    current["mae"] = statistics.mean(errors)
    current["bias"] = statistics.mean(count - actual for count, (_, _, _, actual) in zip(current["counts"], data))

    if args.json:
        print(json.dumps({"rounds": [key for key, _ in rounds], "grid": grid,
                          "default": current}, ensure_ascii=False, indent=2))
        return

    print(f"真值批次 {len(data)} 组：" + "，".join(f"{key.split('/')[-1][:6]}={actual}"
                                                  for key, actual in rounds))
    print()
    print(f"{'阈值':>6}{'最小间隔':>9}{'检出':>22}{'MAE':>8}{'偏差':>8}")
    for row in grid[:args.top]:
        config = row["config"]
        counts = " ".join(f"{c:>4}" for c in row["counts"])
        print(f"{config['prominence']:>6.1f}{config['min_peak_interval']:>9.2f}{counts:>22}"
              f"{row['mae'] * 100:>7.1f}%{row['bias']:>+8.1f}")
    default = grid[0]
    print()
    default_counts = " ".join(f"{c:>4}" for c in current["counts"])
    print(f"当前默认 阈值 {current['config']['prominence']:g} / 间隔 "
          f"{current['config']['min_peak_interval']:g} s → 检出 {default_counts.strip()}，"
          f"MAE {current['mae'] * 100:.1f}%，偏差 {current['bias']:+.1f}")
    best = default["config"]
    gain = current["mae"] - default["mae"]
    print(f"最优     阈值 {best['prominence']:g} / 间隔 {best['min_peak_interval']:g} s → "
          f"MAE {default['mae'] * 100:.1f}%，偏差 {default['bias']:+.1f}")
    if gain < 0.02:
        print("结论     默认参数已经接近这组数据的最优，不必改；误差主要来自单次漏检/多检。")
    else:
        print(f"结论     默认可改善约 {gain * 100:.1f} 个百分点，但仅凭 {len(data)} 组拟合，"
              "换人或换佩戴位置前应补更多轮次再改默认值。")


def match_times(predicted, truth, tolerance):
    paired, i, j = [], 0, 0
    while i < len(predicted) and j < len(truth):
        if abs(predicted[i] - truth[j]) <= tolerance:
            paired.append(predicted[i] - truth[j])
            i, j = i + 1, j + 1
        elif predicted[i] < truth[j]:
            i += 1
        else:
            j += 1
    return paired


def command_compare(args):
    a, b = sorted(read_times(args.a)), sorted(read_times(args.b))
    if not a or not b:
        raise ValueError("empty truth sheet")
    offsets = match_times(a, b, args.tolerance)
    smaller, larger = min(len(a), len(b)), max(len(a), len(b))
    print(f"复核 A     {len(a)} 个事件")
    print(f"复核 B     {len(b)} 个事件")
    print(f"计数差     {larger - smaller}（占较多者的 {(larger - smaller) / larger * 100:.1f}%）")
    print(f"容差 {args.tolerance * 1000:.0f} ms 内匹配 {len(offsets)} 对"
          f"（占较少者的 {len(offsets) / smaller * 100:.1f}%）")
    if offsets:
        print(f"平均偏差   {statistics.mean(offsets) * 1000:+.1f} ms，"
              f"最大 {max(abs(o) for o in offsets) * 1000:.0f} ms")
    if len(offsets) / smaller < 0.99 or larger - smaller > 0.02 * larger:
        print("结论       两人复核不一致，需第三方逐帧裁决后再作为真值。")
    else:
        print("结论       两人复核一致，可作为真值。")


def main():
    parser = argparse.ArgumentParser(
        description="真实数据验证工具：采集质量门禁、真值转标注、双人复核一致性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="时间约定：device_time = truth_time + offset；offset = 同步事件的设备时间 - 视频时间。")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="数据根目录，默认仓库根")
    commands = parser.add_subparsers(dest="command", required=True)

    qc = commands.add_parser("qc", help="检查采集是否满足质量门禁")
    qc.add_argument("--capture", required=True, help="采集 key，形如 2026-11-03/20261103T101500_abcdef1234")
    qc.add_argument("--json", action="store_true", help="输出 JSON")
    qc.set_defaults(func=command_qc)

    annotate = commands.add_parser("annotate", help="把真值时间表转成可导入的标注 JSON")
    annotate.add_argument("--capture", required=True, help="采集 key")
    annotate.add_argument("--truth", required=True, type=Path, help="真值时间表 CSV/TXT，单列秒数")
    annotate.add_argument("--sync", nargs=2, metavar=("VIDEO", "DEVICE"), help="同步事件在两边的时刻")
    annotate.add_argument("--offset", type=float, help="直接给偏移，device = truth + offset")
    annotate.add_argument("--segment", type=int, default=0, help="设备启动片段，默认 0")
    annotate.add_argument("--valid", nargs="*", help="评分区间 device_start:device_end，可多次给")
    annotate.add_argument("--source", default="video", help="真值来源：video / audio / manual，默认 video")
    annotate.add_argument("--notes", default="", help="标注备注")
    annotate.add_argument("--out", type=Path, help="输出路径")
    annotate.set_defaults(func=command_annotate)

    quick = commands.add_parser("quick", help="当场快速检查：检出次数、频率、与人工计数的误差")
    quick.add_argument("--capture", required=True, help="采集 key")
    quick.add_argument("--segment", type=int, default=0, help="设备启动片段，默认 0")
    quick.add_argument("--window", help="设备时间窗口 start:end，例如 5:65；默认整段")
    quick.add_argument("--truth-count", type=int, help="人工计数（可两人独立数，一致后使用）")
    quick.add_argument("--sweep", action="store_true", help="扫多组阈值与最小间隔，看灵敏度")
    quick.add_argument("--prominence", type=float, default=None, help="自定义阈值，默认取算法内置值")
    quick.add_argument("--min-interval", type=float, default=None,
                       help="自定义最小峰间隔；默认取算法内置值（0.25 s）")
    quick.add_argument("--cutoff", type=float, default=5.0, help="低通截止频率，默认 5 Hz")
    quick.add_argument("--blocks", type=float, default=0, help="按固定秒数分块统计检出次数，便于和视频对齐")
    quick.add_argument("--plot", type=Path, help="输出波形与 Δt 图 PNG")
    quick.set_defaults(func=command_quick)

    score = commands.add_parser("score", help="批量比对多组采集：实际次数 vs 检出次数")
    score.add_argument("--truth-file", required=True, type=Path,
                       help="CSV，表头 capture,actual；capture 形如 2026-11-04/批次目录")
    score.add_argument("--prominence", type=float, default=None)
    score.add_argument("--min-interval", type=float, default=None)
    score.add_argument("--cutoff", type=float, default=None)
    score.add_argument("--keep-rest", action="store_true",
                       help="不剔除起止静置段的假峰（默认剔除，因为人工计数只数跳的）")
    score.add_argument("--json", action="store_true", help="输出 JSON")
    score.set_defaults(func=command_score)

    cal = commands.add_parser("calibrate", help="用已知计数网格搜索最优判峰参数")
    cal.add_argument("--truth-file", required=True, type=Path)
    cal.add_argument("--prominence", nargs="+", type=float,
                     default=[0.7, 2.0, 4.0, 6.0, 8.0, 10.0, 14.0], help="候选突出度")
    cal.add_argument("--interval", nargs="+", type=float,
                     default=[0.34, 0.36, 0.38, 0.40, 0.42], help="候选最小峰间隔")
    cal.add_argument("--cutoff", type=float, default=None, help="低通截止频率，默认取内置值")
    cal.add_argument("--top", type=int, default=8, help="显示前几名")
    cal.add_argument("--json", action="store_true")
    cal.set_defaults(func=command_calibrate)

    compare = commands.add_parser("compare", help="比较两份独立复核结果")
    compare.add_argument("--a", required=True, type=Path)
    compare.add_argument("--b", required=True, type=Path)
    compare.add_argument("--tolerance", type=float, default=0.1, help="配对容差，默认 0.1 s")
    compare.set_defaults(func=command_compare)

    args = parser.parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
