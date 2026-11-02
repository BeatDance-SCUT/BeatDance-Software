"""CSV loading and reproducible derived datasets; raw captures stay untouched."""

import json
from pathlib import Path

import pandas as pd

from .imu_udp import utc_now
from .signal_processing import analyze_imu


SIGNALS = ("Magnitude", "X", "Y", "Z")


def rows_to_frame(rows):
    frame = pd.DataFrame(rows)
    if not frame.empty:
        for key in frame.columns.difference(["device", "sender_ip", "imu_id"]):
            frame[key] = pd.to_numeric(frame[key], errors="raise")
    return frame


def save_analysis(directory, signal="Magnitude"):
    """Analyze the entire saved capture (each boot and continuous span separately)."""
    if signal not in SIGNALS:
        raise ValueError("unknown acceleration signal")
    directory = Path(directory)
    raw = pd.read_csv(directory / "imu_raw.csv")
    results = []
    for segment, frame in raw.groupby("segment", sort=False):
        result = analyze_imu(frame, signal)
        result.insert(0, "segment", segment)
        result.insert(0, "signal", signal)
        results.append(result)
    columns = ["signal", "segment", "time", "raw_acceleration", "filtered_acceleration", "is_peak", "span"]
    analysis = pd.concat(results, ignore_index=True) if results else pd.DataFrame(columns=columns)
    path = directory / f"analysis_{signal.lower()}.csv"
    temp = path.with_suffix(".csv.tmp")
    analysis.to_csv(temp, index=False)
    temp.replace(path)
    metadata_path = directory / "session.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.setdefault("derived_files", {})[path.name] = {
        "created_at_utc": utc_now(), "signal": signal, "acceleration_unit": "m/s^2",
        "input": "imu_raw.csv (all accepted samples)", "rows": len(analysis),
        "filter": "4th order Butterworth, cutoff=min(5 Hz, 0.4*fs), zero-phase sosfiltfilt",
        "resampling": "median device-time spacing per continuous span; linear interpolation",
        "max_gap_seconds": 0.2, "minimum_span_seconds": 1,
        "peak_prominence_m_s2": 0.7, "peak_min_distance_samples": "max(1, int(fs/2.5))",
        "note": "Short spans also need >15 samples for filtering. Peaks are algorithm outputs, not labels.",
    }
    temp_metadata = metadata_path.with_suffix(".json.tmp")
    temp_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_metadata.replace(metadata_path)
    return path
