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
    seq: int
    t_us: int
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


def parse_packet(packet):
    """Validate one UDP v1 JSON datagram from the ESP32."""
    try:
        obj = json.loads(packet.decode("utf-8"))

        if not isinstance(obj, dict) or type(obj.get("v")) is not int or obj["v"] != 1:
            raise ValueError("unsupported protocol version")

        if type(obj.get("seq")) is not int or not 0 <= obj["seq"] < 2**16:
            raise ValueError("seq must be uint16")

        if type(obj.get("t_us")) is not int or not 0 <= obj["t_us"] < 2**32:
            raise ValueError("t_us must be uint32")

        keys = ("ax", "ay", "az", "gx", "gy", "gz")
        values = []

        for key in keys:
            value = obj.get(key)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{key} must be a finite number")
            values.append(float(value))

        return IMUSample(
            seq=obj["seq"],
            t_us=obj["t_us"],
            ax=values[0],
            ay=values[1],
            az=values[2],
            gx=values[3],
            gy=values[4],
            gz=values[5],
        )

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
        self._elapsed_us = 0
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
            owner = sender_ip

        if self._owner is not None and owner != self._owner:
            self._counts["ignored_device"] += 1
            return

        gap = 0

        if self._previous is not None:
            previous = self._previous

            # seq 是 uint16，范围 0 ~ 65535
            delta_seq = (sample.seq - previous.seq) % (2**16)

            # t_us 是 uint32，范围 0 ~ 2**32-1
            delta_us = (sample.t_us - previous.t_us) % (2**32)

            # 如果两个计数器都明显倒退，且中间至少间隔 2 秒，
            # 暂时判断为设备重启。
            reboot = (
                delta_seq >= 2**15
                and sample.seq < previous.seq
                and sample.t_us < previous.t_us
                and delta_us >= 2**31
                and now - self._last_receive >= 2
            )

            if reboot:
                self._segment += 1
                self._elapsed_us = 0
                self._counts["restarts"] += 1

            elif (
                delta_seq == 0
                or delta_seq >= 2**15
                or not 0 < delta_us < 2**31
            ):
                self._counts["out_of_order"] += 1
                return

            else:
                self._elapsed_us += delta_us
                gap = delta_seq - 1

        record = {
            "host_s": pc_time,
            "seg": self._segment,
            "seq": sample.seq,
            "t_us": sample.t_us,
            "t_ms": sample.t_us // 1000,
            "ax": sample.ax,
            "ay": sample.ay,
            "az": sample.az,
            "gx": sample.gx,
            "gy": sample.gy,
            "gz": sample.gz,
        }

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
