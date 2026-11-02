"""BeatDance UDP v1 receiver, adapted from Hardware-UDP-Code/imu_udp_plot.py.

The worker never calls Streamlit. Each dashboard session owns its receiver,
which releases the port on stop or after the dashboard stops polling for 60 s.
"""

from collections import deque
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import socket
import threading
import time
from uuid import uuid4

from .paths import DEFAULT_RECORDINGS

UINT32 = 2**32
RAW_COLUMNS = [
    "pc_time", "device", "sender_ip", "seq", "t_ms", "imu_id",
    "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps", "temp_c",
    "v", "segment", "time", "seq_gap",
]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_capture_rows(path, max_rows=30000):
    """Read complete rows from the CSV tail without scanning a long recording.

    All v1 fields are single-line (device IDs are restricted accordingly).
    A concurrent partial final row is left for the next refresh.
    """
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    with Path(path).open("rb") as stream:
        header = stream.readline().decode("utf-8")
        start = stream.tell()
        stream.seek(0, 2)
        end = stream.tell()
        chunks = []
        newline_count = 0
        while end > start and newline_count <= max_rows:
            size = min(65536, end - start)
            end -= size
            stream.seek(end)
            chunk = stream.read(size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    if end > start:
        data = data.partition(b"\n")[2]  # discard the first partial line
    lines = data.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b"\n"):
        lines.pop()
    return list(csv.DictReader([header, *(line.decode("utf-8") for line in lines[-max_rows:])]))


@dataclass(frozen=True)
class IMUSample:
    device: str
    seq: int
    t_ms: int
    imu_id: str
    ax_g: float
    ay_g: float
    az_g: float
    gx_dps: float
    gy_dps: float
    gz_dps: float
    temp_c: float


def parse_packet(packet):
    """Validate one UTF-8 JSON datagram against the firmware's v1 schema."""
    try:
        obj = json.loads(packet.decode("utf-8"))
        if not isinstance(obj, dict) or type(obj.get("v")) is not int or obj["v"] != 1:
            raise ValueError("unsupported protocol version")
        if not isinstance(obj["device"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", obj["device"]):
            raise ValueError("device must be a single-line ID (letters, digits, _, . or -)")
        for key in ("seq", "t_ms"):
            if type(obj[key]) is not int or not 0 <= obj[key] < UINT32:
                raise ValueError(f"{key} must be uint32")
        if obj["imu_id"] not in ("0x70", "0x71"):
            raise ValueError("unsupported IMU")
        six = []
        for key in ("acc_g", "gyro_dps"):
            vector = obj[key]
            if not isinstance(vector, list) or len(vector) != 3:
                raise ValueError(f"{key} must contain three numbers")
            six.extend(vector)
        numbers = [*six, obj["temp_c"]]
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in numbers):
            raise ValueError("sensor values must be finite numbers")
        return IMUSample(obj["device"], obj["seq"], obj["t_ms"], obj["imu_id"],
                         *(float(value) for value in numbers))
    except (KeyError, TypeError, UnicodeDecodeError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid IMU packet") from exc


class UDPReceiver:
    """One device per capture; persist every accepted sample before displaying it."""

    def __init__(self, bind="0.0.0.0", port=5005, output_dir=DEFAULT_RECORDINGS, idle_timeout=60):
        if not 0 <= port <= 65535 or idle_timeout <= 0:
            raise ValueError("invalid receiver configuration")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._arrivals = deque(maxlen=10000)
        self._owner = None
        self._previous = None
        self._elapsed_ms = 0
        self._segment = 0
        self._last_receive = None
        self._heartbeat = time.monotonic()
        self._idle_timeout = idle_timeout
        self._counts = dict(packets=0, invalid=0, ignored_device=0,
                            out_of_order=0, gaps=0, restarts=0)
        self._error = None
        self._finished = False
        self._csv_file = None
        self._thread = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Do not share the UDP port with another dashboard/plotter.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self._sock.bind((bind, port))
            self._sock.settimeout(0.2)
            stamp = datetime.now(timezone.utc)
            self.capture_id = stamp.strftime("%H%M%S_%f") + "_" + uuid4().hex[:8]
            self.directory = Path(output_dir).resolve() / stamp.strftime("%Y-%m-%d") / self.capture_id
            self.directory.mkdir(parents=True)
            self.csv_path = self.directory / "imu_raw.csv"
            self._csv_file = self.csv_path.open("x", encoding="utf-8", newline="", buffering=1)
            self._writer = csv.DictWriter(self._csv_file, fieldnames=RAW_COLUMNS)
            self._writer.writeheader()
            self._metadata = {
                "schema_version": 1, "protocol_version": 1, "capture_id": self.capture_id,
                "started_at_utc": stamp.isoformat(), "ended_at_utc": None,
                "status": "recording", "bind": bind, "port": self._sock.getsockname()[1],
                "raw_file": "imu_raw.csv", "columns": RAW_COLUMNS,
                "units": {"pc_time": "Unix seconds (PC wall clock)", "t_ms": "device uint32 milliseconds",
                          "time": "seconds since first accepted sample in each segment",
                          "acceleration": "g (includes gravity)", "angular_velocity": "deg/s", "temp_c": "deg C"},
                "target_sample_rate_hz": 50,
                "owner": None, "labels": {"participant": "", "activity": "", "notes": ""},
                "sequence_policy": "First device+sender IP only; duplicates/reordering excluded. "
                                   "Backward counters after 2 s silence start a new segment.",
                "data_validation": "Software capture only; motion/labels require experiment validation.",
            }
            self._write_metadata()
        except (OSError, ValueError):
            self._sock.close()
            if self._csv_file is not None:
                self._csv_file.close()
            raise
        self.address = self._sock.getsockname()

    def start(self):
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("create a new receiver for a new capture")
        self._thread = threading.Thread(target=self._run, name="beatdance-udp", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        elif self._csv_file is not None:
            self._finish()

    def set_labels(self, participant="", activity="", notes=""):
        with self._lock:
            self._metadata["labels"] = dict(participant=participant, activity=activity, notes=notes)
            self._write_metadata()

    def _write_metadata(self):
        metadata = {**self._metadata, "counts": dict(self._counts),
                    "owner": self._owner, "error": self._error}
        path = self.directory / "session.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    def _finish(self):
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if self._csv_file is not None and not self._csv_file.closed:
                self._csv_file.close()
            self._metadata.update(status="error" if self._error else "stopped", ended_at_utc=utc_now())
            try:
                self._write_metadata()
            except OSError as exc:
                self._error = f"Could not finalize session metadata: {exc}"

    def _run(self):
        try:
            while not self._stop.is_set():
                with self._lock:
                    expired = time.monotonic() - self._heartbeat > self._idle_timeout
                if expired:
                    break
                try:
                    packet, addr = self._sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not self._stop.is_set():
                        with self._lock:
                            self._error = str(exc)
                    break
                try:
                    sample = parse_packet(packet)
                except ValueError:
                    with self._lock:
                        self._counts["invalid"] += 1
                    continue
                try:
                    self._accept(sample, addr[0], time.monotonic(), time.time())
                except (OSError, ValueError) as exc:
                    with self._lock:
                        self._error = f"CSV write failed: {exc}"
                    break
        finally:
            self._stop.set()
            self._sock.close()
            self._finish()

    def _accept(self, sample, sender_ip, now, pc_time):
        with self._lock:
            owner = (sample.device, sender_ip)
            if self._owner is not None and owner != self._owner:
                self._counts["ignored_device"] += 1
                return
            gap = 0
            if self._previous is not None:
                delta_seq = (sample.seq - self._previous.seq) % UINT32
                delta_ms = (sample.t_ms - self._previous.t_ms) % UINT32
                # Backward counters after >=2 s without an accepted sample are
                # treated as a reboot. Ordinary duplicates/reordering are dropped.
                reboot = (delta_seq >= UINT32 // 2
                          and sample.seq < self._previous.seq
                          and sample.t_ms < self._previous.t_ms
                          and delta_ms >= UINT32 // 2
                          and now - self._last_receive >= 2)
                if reboot:
                    self._segment += 1
                    self._elapsed_ms = 0
                    self._counts["restarts"] += 1
                elif delta_seq == 0 or delta_seq >= UINT32 // 2 or not 0 < delta_ms < UINT32 // 2:
                    self._counts["out_of_order"] += 1
                    return
                else:
                    self._elapsed_ms += delta_ms
                    gap = delta_seq - 1
            record = asdict(sample)
            record.update(pc_time=pc_time, sender_ip=sender_ip, v=1, seq_gap=gap,
                          segment=self._segment, time=self._elapsed_ms / 1000)
            self._writer.writerow(record)
            self._csv_file.flush()
            self._owner = owner
            self._previous = sample
            self._last_receive = now
            self._counts["packets"] += 1
            self._counts["gaps"] += gap
            self._arrivals.append(now)

    def snapshot(self, read_rows=True):
        """Read persisted rows and counters; renew the browser's lease."""
        now = time.monotonic()
        with self._lock:
            self._heartbeat = now
            while self._arrivals and now - self._arrivals[0] > 1:
                self._arrivals.popleft()
            # Hold the writer lock so rows and counters describe one CSV snapshot.
            rows = read_capture_rows(self.csv_path) if read_rows else []
            return rows, {
                **self._counts, "running": not self._stop.is_set(),
                "age": None if self._last_receive is None else now - self._last_receive,
                "packet_rate": len(self._arrivals), "owner": self._owner,
                "error": self._error,
            }
