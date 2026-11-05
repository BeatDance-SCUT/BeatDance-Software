"""Replay a saved capture as UDP v1 packets: python -m scripts.replay_capture.

调试时不需要硬件持续发送：把已经保存的 imu_raw.csv 按原始时间节奏
重新打包成固件报文发到本机端口，可反复复现接收与显示问题。
"""

import argparse
import json
from pathlib import Path
import socket
import time

from beatdance.paths import DEFAULT_RECORDINGS
from beatdance.recordings import load_raw_csv

UINT32 = 2**32


def latest_capture():
    manifests = sorted(DEFAULT_RECORDINGS.glob("*/*/imu_raw.csv"))
    return manifests[-1] if manifests else None


def resolve_source(source):
    path = Path(source) if source is not None else latest_capture()
    if path is None:
        raise FileNotFoundError("没有找到任何历史采集，请先开始一次采集或用 --source 指定 CSV")
    if path.is_dir():
        path = path / "imu_raw.csv"
    if not path.exists():
        raise FileNotFoundError(f"找不到原始 CSV：{path}")
    return path


def build_packets(frame, loops=1):
    """按段合成严格单调的 seq / t_us，避免依赖原始 CSV 的计数值。"""
    packets = []
    for _ in range(loops):
        for segment, group in frame.groupby("segment", sort=False):
            for seq, (t, ax, ay, az, gx, gy, gz) in enumerate(zip(
                    group["time"], group["ax_g"], group["ay_g"], group["az_g"],
                    group["gx_dps"], group["gy_dps"], group["gz_dps"])):
                packets.append((t, json.dumps({
                    "v": 1, "seq": seq, "t_us": int(round(t * 1e6)) % UINT32,
                    "ax": round(float(ax), 4), "ay": round(float(ay), 4), "az": round(float(az), 4),
                    "gx": round(float(gx), 3), "gy": round(float(gy), 3), "gz": round(float(gz), 3),
                }).encode("utf-8")))
    return packets


def replay(source=None, host="127.0.0.1", port=5005, speed=1.0, loops=1, verbose=True):
    """Send one saved capture to host:port; returns (sent, elapsed_seconds)."""
    if speed <= 0 or loops <= 0:
        raise ValueError("speed 与 loops 必须为正数")
    path = resolve_source(source)
    frame = load_raw_csv(path)
    packets = build_packets(frame, loops)
    if verbose:
        print(f"源文件：{path}")
        print(f"样本：{len(frame)} 行，段：{frame.segment.nunique()}，"
              f"时长：{frame.time.max():.2f} s，发送 {len(packets)} 包 → {host}:{port}，速度 ×{speed}")
    started = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        for index, (t_us, payload) in enumerate(packets):
            deadline = started + t_us / speed
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            sender.sendto(payload, (host, port))
            if verbose and index and index % 500 == 0:
                print(f"  已发送 {index}/{len(packets)} 包（设备时间 {t_us:.2f} s）")
    elapsed = time.monotonic() - started
    if verbose:
        print(f"完成：{len(packets)} 包，用时 {elapsed:.2f} s")
    return len(packets), elapsed


def main():
    parser = argparse.ArgumentParser(description="把已保存的采集回放成 UDP v1 报文")
    parser.add_argument("--source", help="采集目录或 imu_raw.csv；默认使用最新一次采集")
    parser.add_argument("--host", default="127.0.0.1", help="目标地址，默认本机回环")
    parser.add_argument("--port", type=int, default=5005, help="目标端口，默认 5005")
    parser.add_argument("--speed", type=float, default=1.0, help="倍速，10 表示 10 倍速")
    parser.add_argument("--loops", type=int, default=1, help="重复发送次数")
    args = parser.parse_args()
    replay(args.source, args.host, args.port, args.speed, args.loops)


if __name__ == "__main__":
    main()
