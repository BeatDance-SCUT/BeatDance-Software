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


def command_quick(args):
    """Fast post-round check: detected count and rate against a human count."""
    store = Store(args.root)
    frame, meta, _ = store.capture(args.capture)
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
    acceleration = np.sqrt(segment.ax_g ** 2 + segment.ay_g ** 2 + segment.az_g ** 2).to_numpy() * STANDARD_GRAVITY

    if args.sweep:
        grid = [AlgorithmConfig(prominence=p, min_peak_interval=interval)
                for p in (0.7, 2.0, 5.0, 10.0) for interval in (0.40, 0.30, 0.25)]
    else:
        grid = [AlgorithmConfig(prominence=args.prominence, min_peak_interval=args.min_interval,
                                cutoff_hz=args.cutoff)]
    results = []
    for config in grid:
        analysis = analyze_acceleration(times, acceleration, config=config)
        peaks = analysis[analysis.is_peak == 1]
        lag = np.diff(peaks.time.to_numpy())
        span = float(peaks.time.max() - peaks.time.min()) if len(peaks) > 1 else 0.0
        bpm = 60.0 * (len(peaks) - 1) / span if span > 0 else float("nan")
        error = (abs(len(peaks) - args.truth_count) / args.truth_count
                 if args.truth_count else None)
        results.append((config, analysis, peaks, lag, bpm, error))

    print(f"采集     {args.capture}")
    print(f"窗口     {low:.2f} – {high:.2f} s（{high - low:.1f} s，{len(segment)} 样本）")
    if args.truth_count:
        print(f"人工计数 {args.truth_count} 次 ≈ {args.truth_count * 60 / (high - low):.1f} 次/分")
    print()
    print(f"{'阈值':>6} {'最小间隔':>8} {'检出':>5} {'平均频率':>10} {'误差':>8}")
    for config, _, peaks, _, bpm, error in results:
        error_text = f"{error * 100:.1f}%" if error is not None else "—"
        rate_text = f"{bpm:.1f}/分" if bpm == bpm else "—"
        print(f"{config.prominence:>6.1f} {config.min_peak_interval:>8.2f} {len(peaks):>5} "
              f"{rate_text:>10} {error_text:>8}")
    print()
    config, analysis, peaks, lag, bpm, error = results[0]
    if len(lag):
        print(f"间隔     中位数 {np.median(lag) * 1000:.0f} ms，"
              f"最短 {lag.min() * 1000:.0f} ms，最长 {lag.max() * 1000:.0f} ms，"
              f"CV {np.std(lag) / np.mean(lag):.2f}")
    if len(lag) and lag.min() < 0.22:
        print("警告     出现 <220 ms 的间隔，可能是同一次跳跃被重复计数。")
    if len(lag) and lag.max() > 2.0:
        print("提示     存在 >2 s 的间隔，检查是否中途停顿或漏检成串。")
    if args.plot:
        save_plot(args.plot, times, acceleration, analysis, peaks,
                  f"{args.capture}  segment {args.segment}  {low:.1f}-{high:.1f}s")
        print(f"图已保存 {args.plot}")


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
    quick.add_argument("--prominence", type=float, default=0.7, help="自定义阈值，默认 0.7")
    quick.add_argument("--min-interval", type=float, default=0.4, help="自定义最小峰间隔，默认 0.4 s")
    quick.add_argument("--cutoff", type=float, default=5.0, help="低通截止频率，默认 5 Hz")
    quick.add_argument("--plot", type=Path, help="输出波形与 Δt 图 PNG")
    quick.set_defaults(func=command_quick)

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
