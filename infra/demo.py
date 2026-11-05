"""Deterministic synthetic six-axis capture. Never evidence of real accuracy."""

import csv
import io
import math

from beatdance.imu_udp import RAW_COLUMNS
from .store import Store


def synthetic_fixture():
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=RAW_COLUMNS)
    writer.writeheader()
    events = []
    for segment in (0, 1):
        previous_seq = None
        for seq in range(400):
            t = seq / 50
            if segment == 0 and 3 <= t < 3.5:
                continue
            writer.writerow(dict(pc_time=1700000000 + segment * 20 + t, device="synthetic-imu", sender_ip="127.0.0.1",
                seq=seq, t_ms=seq * 20, imu_id="0x70", ax_g=0.01 * math.sin(t), ay_g=0.01 * math.cos(t),
                az_g=1 + 0.2 * math.sin(4 * math.pi * t), gx_dps=math.sin(t), gy_dps=math.cos(t),
                gz_dps=0, temp_c=25, v=1, segment=segment, time=t,
                seq_gap=0 if previous_seq is None else seq - previous_seq - 1))
            previous_seq = seq
        events.extend({"segment": segment, "time": 0.125 + i * 0.5} for i in range(16)
                      if not (segment == 0 and 3 <= 0.125 + i * 0.5 < 3.5))
    metadata = {"schema_version": 1, "protocol_version": 1, "capture_id": "synthetic-fixture-v1",
                "status": "stopped", "owner": ["synthetic-imu", "127.0.0.1"],
                "started_at_utc": "2023-11-14T22:13:20+00:00", "ended_at_utc": "2023-11-14T22:13:48+00:00",
                "synthetic": True, "labels": {"participant": "SYNTHETIC", "activity": "synthetic-sine",
                "notes": "人工构造六轴与峰时刻，非真实跳绳、非真实识别精度证据。佩戴：不适用。"},
                "units": {"acceleration": "g", "angular_velocity": "deg/s", "time": "device-relative seconds"},
                "counts": {"packets": 775, "gaps": 25, "restarts": 1}, "target_sample_rate_hz": 50}
    intervals = [{"segment": s, "start": 0.0, "end": 7.98, "kind": "valid"} for s in (0, 1)]
    return stream.getvalue().encode("utf-8"), metadata, events, intervals


def install_demo(store: Store):
    raw, meta, events, intervals = synthetic_fixture()
    key = store.import_capture(raw, meta)
    annotation = store.save_annotation(key, events, intervals, "人工构造真值；仅用于软件流程验收")
    dataset = store.save_dataset("合成流程示例（非真实精度）", [
        {"capture_key": key, "segment": s, "start": 0, "end": 7.98, "annotation_id": annotation["id"]}
        for s in (0, 1)])
    return key, annotation, dataset


def main():
    import argparse
    from pathlib import Path
    from beatdance.paths import PROJECT_ROOT
    parser = argparse.ArgumentParser(description="Install a NEW synthetic capture, annotation and dataset")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    key, annotation, dataset = install_demo(Store(args.root))
    print(f"SYNTHETIC ONLY\ncapture={key}\nannotation={annotation['id']}\ndataset={dataset['id']}")


if __name__ == "__main__":
    main()
