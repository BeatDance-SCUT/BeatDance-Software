"""Versioned local captures, annotations and dataset snapshots."""

import csv
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
import math
from pathlib import Path
import re
from uuid import uuid4

import pandas as pd

from beatdance.imu_udp import RAW_COLUMNS, parse_packet
from beatdance.paths import PROJECT_ROOT


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return f"{prefix}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid4().hex[:10]}"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path, value):
    """Atomically replace metadata; immutable callers allocate a new id first."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def number(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name}: boolean is not a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name}: expected finite number")
    return result


def integer(value, name):
    result = number(value, name)
    if result < 0 or not result.is_integer():
        raise ValueError(f"{name}: expected nonnegative integer")
    return int(result)


def validate_capture(raw_bytes, metadata):
    """Validate the accepted-row CSV contract, without altering supplied bytes."""
    if (not isinstance(metadata, dict) or type(metadata.get("schema_version")) is not int
            or type(metadata.get("protocol_version")) is not int
            or metadata.get("schema_version") != 1 or metadata.get("protocol_version") != 1):
        raise ValueError("expected session schema 1 / UDP protocol 1")
    if not isinstance(metadata.get("capture_id"), str) or not metadata["capture_id"]:
        raise ValueError("session requires capture_id")
    labels = metadata.get("labels", {})
    if not isinstance(labels, dict) or any(not isinstance(v, str) for v in labels.values()):
        raise ValueError("session labels must contain strings")
    if metadata.get("status") not in ("stopped", "error"):
        raise ValueError("only closed captures can be imported or used in datasets")
    reader = csv.DictReader(io.StringIO(raw_bytes.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != RAW_COLUMNS:
        raise ValueError("raw CSV columns differ from the software capture contract")
    rows, owner, previous = [], None, None
    for line, row in enumerate(reader, 2):
        try:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("incomplete CSV row")
            values = dict(row)
            for key in ("v", "seq", "t_ms", "segment", "seq_gap"):
                values[key] = integer(row[key], key)
            for key in ("pc_time", "time", "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps", "temp_c"):
                values[key] = number(row[key], key)
            ipaddress.IPv4Address(values["sender_ip"])
            parse_packet(json.dumps({"v": values["v"], "device": values["device"], "seq": values["seq"],
                                    "t_ms": values["t_ms"], "imu_id": values["imu_id"],
                                    "acc_g": [values[k] for k in ("ax_g", "ay_g", "az_g")],
                                    "gyro_dps": [values[k] for k in ("gx_dps", "gy_dps", "gz_dps")],
                                    "temp_c": values["temp_c"]}).encode())
            current_owner = (values["device"], values["sender_ip"])
            if owner is not None and current_owner != owner:
                raise ValueError("mixed device ownership")
            if values["time"] < 0:
                raise ValueError("negative device-relative time")
            if previous is not None:
                if values["segment"] < previous["segment"]:
                    raise ValueError("segments must be ordered")
                if values["segment"] == previous["segment"]:
                    delta_seq = (values["seq"] - previous["seq"]) % (2 ** 32)
                    delta_ms = (values["t_ms"] - previous["t_ms"]) % (2 ** 32)
                    if not 0 < delta_seq < 2 ** 31 or not 0 < delta_ms < 2 ** 31:
                        raise ValueError("duplicate or out-of-order raw sample")
                    if values["seq_gap"] != delta_seq - 1 or not math.isclose(values["time"] - previous["time"], delta_ms / 1000, abs_tol=1e-7):
                        raise ValueError("raw timing / sequence gap mismatch")
                elif values["segment"] != previous["segment"] + 1 or values["time"] != 0 or values["seq_gap"] != 0:
                    raise ValueError("new segment must start at zero")
            elif values["segment"] != 0 or values["time"] != 0 or values["seq_gap"] != 0:
                raise ValueError("first raw sample must start at zero")
            owner, previous = current_owner, values
            rows.append(values)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"CSV line {line}: {exc}") from exc
    declared = metadata.get("owner")
    if owner and declared and list(owner) != list(declared):
        raise ValueError("session owner differs from CSV")
    return pd.DataFrame(rows, columns=RAW_COLUMNS)


class Store:
    """All paths are relative to one workspace root; ids never contain paths."""

    def __init__(self, root=PROJECT_ROOT):
        self.root = Path(root).resolve()
        self.recordings = self.root / "data" / "recordings"
        self.annotations = self.root / "data" / "annotations"
        self.datasets = self.root / "data" / "datasets"
        self.runs = self.root / "results" / "experiments"

    def object_path(self, category, object_id):
        if not isinstance(object_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", object_id):
            raise ValueError("invalid object id")
        base = {"annotation": self.annotations, "dataset": self.datasets, "run": self.runs}[category]
        return base / (object_id if category == "run" else f"{object_id}.json")

    def capture_path(self, key):
        if not isinstance(key, str) or "\\" in key or len(Path(key).parts) != 2:
            raise ValueError("capture key must be date/directory")
        path = (self.recordings / key).resolve()
        if not path.is_relative_to(self.recordings.resolve()) or any(p in (".", "..") for p in Path(key).parts):
            raise ValueError("capture path escapes recordings")
        return path

    def captures(self):
        found = []
        for path in sorted(self.recordings.glob("*/*/session.json"), reverse=True):
            key = path.parent.relative_to(self.recordings).as_posix()
            try:
                meta = read_json(path)
                if not isinstance(meta.get("labels", {}), dict):
                    raise ValueError("invalid session labels")
                found.append({"key": key, "metadata": meta, "error": None})
            except (OSError, ValueError) as exc:
                found.append({"key": key, "metadata": {}, "error": str(exc)})
        return found

    def capture(self, key):
        path = self.capture_path(key)
        raw = (path / "imu_raw.csv").read_bytes()
        meta_bytes = (path / "session.json").read_bytes()
        meta = json.loads(meta_bytes)
        frame = validate_capture(raw, meta)
        return frame, meta, {"raw_sha256": digest(raw), "session_sha256": digest(meta_bytes)}

    def import_capture(self, raw_bytes, metadata):
        validate_capture(raw_bytes, metadata)
        key = f"{datetime.now(timezone.utc):%Y-%m-%d}/{new_id('import')}"
        path = self.capture_path(key)
        path.mkdir(parents=True, exist_ok=False)
        (path / "imu_raw.csv").write_bytes(raw_bytes)
        imported = {**metadata, "capture_id": path.name, "imported_at_utc": utc_now(),
                    "source_capture_id": metadata.get("capture_id")}
        write_json(path / "session.json", imported)
        return key

    def list_objects(self, category):
        base = {"annotation": self.annotations, "dataset": self.datasets, "run": self.runs}[category]
        pattern = "*/manifest.json" if category == "run" else "*.json"
        return [read_json(p) for p in sorted(base.glob(pattern), reverse=True)]

    def save_annotation(self, capture_key, events, intervals, notes="", parent=None):
        frame, _, hashes = self.capture(capture_key)
        bounds = {int(s): (float(g.time.min()), float(g.time.max())) for s, g in frame.groupby("segment")}
        def validate_time(segment, value):
            segment, value = integer(segment, "segment"), number(value, "time")
            if segment not in bounds or not bounds[segment][0] <= value <= bounds[segment][1]:
                raise ValueError("annotation time outside capture segment")
            return segment, value
        clean_events, clean_intervals = [], []
        for event in events:
            s, t = validate_time(event["segment"], event["time"])
            clean_events.append({"segment": s, "time": t})
        if len({(e["segment"], e["time"]) for e in clean_events}) != len(clean_events):
            raise ValueError("duplicate annotation event")
        for interval in intervals:
            s, start = validate_time(interval["segment"], interval["start"])
            _, end = validate_time(s, interval["end"])
            if start >= end or interval["kind"] not in ("valid", "exclude"):
                raise ValueError("expected positive interval and valid/exclude kind")
            clean_intervals.append({"segment": s, "start": start, "end": end, "kind": interval["kind"]})
        if parent:
            old = read_json(self.object_path("annotation", parent))
            if old["capture_key"] != capture_key or old["raw_sha256"] != hashes["raw_sha256"]:
                raise ValueError("annotation parent source mismatch")
        value = {"schema_version": 1, "id": new_id("annotation"), "capture_key": capture_key,
                 "created_at_utc": utc_now(), "parent": parent, "raw_sha256": hashes["raw_sha256"],
                 "events": sorted(clean_events, key=lambda e: (e["segment"], e["time"])),
                 "intervals": clean_intervals, "notes": str(notes)}
        write_json(self.object_path("annotation", value["id"]), value)
        return value

    def save_dataset(self, name, items):
        if not str(name).strip() or not items:
            raise ValueError("dataset requires a name and at least one selection")
        clean, seen = [], {}
        for item in items:
            frame, _, hashes = self.capture(item["capture_key"])
            segment = integer(item["segment"], "segment")
            start, end = number(item["start"], "start"), number(item["end"], "end")
            selected = frame[frame.segment == segment]
            if selected.empty or start > end or start < selected.time.min() or end > selected.time.max():
                raise ValueError("dataset selection outside capture")
            group = (item["capture_key"], segment)
            if any(start <= b and end >= a for a, b in seen.get(group, [])):
                raise ValueError("overlapping dataset selections would double-count events")
            seen.setdefault(group, []).append((start, end))
            annotation_id = item.get("annotation_id") or None
            annotation_hash = None
            if annotation_id:
                annotation_path = self.object_path("annotation", annotation_id)
                annotation = read_json(annotation_path)
                if annotation["capture_key"] != item["capture_key"] or annotation["raw_sha256"] != hashes["raw_sha256"]:
                    raise ValueError("annotation source mismatch")
                annotation_hash = digest(annotation_path.read_bytes())
            clean.append({"capture_key": item["capture_key"], "segment": segment, "start": start, "end": end,
                          **hashes, "annotation_id": annotation_id, "annotation_sha256": annotation_hash})
        value = {"schema_version": 1, "id": new_id("dataset"), "name": str(name).strip(),
                 "created_at_utc": utc_now(), "items": clean}
        write_json(self.object_path("dataset", value["id"]), value)
        return value
