"""Run with: python -m unittest discover -s tests -v"""

import json
import math
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from scipy.signal import butter, find_peaks, sosfiltfilt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beatdance.imu_udp import RAW_COLUMNS, UINT32, UDPReceiver, parse_packet, read_capture_rows
from beatdance.recordings import normalize_frame, rows_to_frame, save_analysis
from beatdance.signal_processing import STANDARD_GRAVITY, analyze_acceleration, analyze_imu


def packet(seq=0, t_us=20000, **changes):
    """当前固件的扁平六轴报文：seq/t_us 为 uint32，加速度 g，角速度 deg/s。"""
    obj = dict(v=1, seq=seq, t_us=t_us,
               ax=0.0, ay=0.0, az=1 + 0.12 * math.sin(2 * math.pi * 1.8 * seq / 50),
               gx=1.25, gy=-0.5, gz=0.15)
    obj.update(changes)
    return json.dumps(obj).encode("utf-8")


def wait_for(receiver, key, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = receiver.snapshot(read_rows=False)[1]
        if state[key] >= expected:
            return state
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {key}={expected}: {state}")


class CaptureTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def receiver(self, start=True, **kwargs):
        receiver = UDPReceiver("127.0.0.1", 0, output_dir=self.output, **kwargs)
        self.addCleanup(receiver.stop)
        return receiver.start() if start else receiver

    def test_protocol_rejects_malformed_payloads(self):
        sample = parse_packet(packet())
        self.assertEqual(sample.az, 1)
        # uint32 seq：超过 65535 仍应被接受，否则设备跑满约 11 分钟后整包被拒。
        self.assertEqual(parse_packet(packet(seq=70000)).seq, 70000)
        invalid = [b"[]", b"null", b"{", b"\xff", packet(v=2), packet(v=True),
                   packet(ax=None), packet(az="1"), packet(gz=float("nan")),
                   packet(gy=float("inf")), packet(seq=-1), packet(seq=True),
                   packet(seq=UINT32), packet(t_us=UINT32), packet(t_us=-1)]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_packet(data)

    def test_real_udp_to_csv_to_analysis(self):
        receiver = self.receiver()
        receiver.set_labels("P001", "jump_rope", "test fixture, not a human recording")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for seq in range(250):
                sender.sendto(packet(seq, 20000 + seq * 20000), receiver.address)
                time.sleep(0.001)
            sender.sendto(b"not json", receiver.address)
            sender.sendto(packet(249, 20000 + 249 * 20000), receiver.address)  # duplicate
            sender.sendto(packet(100, 20000 + 100 * 20000), receiver.address)  # late packet
            sender.sendto(packet(252, 20000 + 252 * 20000), receiver.address)  # two missing sequences
        wait_for(receiver, "packets", 251)
        wait_for(receiver, "invalid", 1)
        receiver.stop()
        rows, state = receiver.snapshot()
        self.assertEqual(state["gaps"], 2)
        self.assertEqual(state["out_of_order"], 2)
        raw = pd.read_csv(receiver.csv_path)
        pd.testing.assert_frame_equal(rows_to_frame(rows), normalize_frame(raw), check_dtype=False)
        self.assertEqual(list(raw.columns), RAW_COLUMNS)
        self.assertEqual(raw["seq"].iloc[-1], 252)
        self.assertAlmostEqual(raw["time"].iloc[-1], 5.04)
        before = receiver.csv_path.read_bytes()
        derived = pd.read_csv(save_analysis(receiver.directory))
        self.assertGreater(derived["is_peak"].sum(), 5)
        self.assertEqual(receiver.csv_path.read_bytes(), before)
        metadata = json.loads((receiver.directory / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "stopped")
        self.assertEqual(metadata["labels"]["participant"], "P001")
        self.assertEqual(metadata["counts"]["packets"], len(raw))
        self.assertIn("analysis_magnitude.csv", metadata["derived_files"])
        # Stopping must release the port immediately.
        replacement = UDPReceiver(*receiver.address, output_dir=self.output)
        replacement.stop()

    def test_second_sender_is_ignored_after_owner_lock(self):
        receiver = self.receiver(False)
        receiver._accept(parse_packet(packet(0, 0)), "127.0.0.1", 1, 1)
        receiver._accept(parse_packet(packet(1, 20000)), "127.0.0.2", 1.02, 2)
        receiver.stop()
        self.assertEqual(receiver.snapshot()[1]["ignored_device"], 1)
        self.assertEqual(len(pd.read_csv(receiver.csv_path)), 1)

    def test_capture_paths_do_not_overwrite_and_empty_analysis_works(self):
        first, second = self.receiver(False), self.receiver(False)
        self.assertNotEqual(first.directory, second.directory)
        first.stop()
        second.stop()
        self.assertTrue(pd.read_csv(save_analysis(first.directory)).empty)
        self.assertTrue(second.csv_path.exists())

    def test_clock_and_sequence_wrap_reboot_and_reordering(self):
        receiver = self.receiver(False)
        receiver._accept(parse_packet(packet(UINT32 - 2, UINT32 - 10000)), "127.0.0.1", 1.0, 1)
        receiver._accept(parse_packet(packet(UINT32 - 1, UINT32 - 1000)), "127.0.0.1", 1.02, 2)
        receiver._accept(parse_packet(packet(0, 9000)), "127.0.0.1", 1.04, 3)   # uint32 回绕
        receiver._accept(parse_packet(packet(1, 19000)), "127.0.0.1", 1.06, 4)
        receiver._accept(parse_packet(packet(0, 0)), "127.0.0.1", 1.08, 5)     # 倒退但未静默 2 s
        receiver._accept(parse_packet(packet(0, 0)), "127.0.0.1", 5.0, 6)      # 判为重启
        receiver.stop()
        raw = pd.read_csv(receiver.csv_path)
        self.assertEqual(raw["seg"].tolist(), [0, 0, 0, 0, 1])
        self.assertAlmostEqual(raw["time"].iloc[1], 0.009, places=6)
        self.assertAlmostEqual(raw["time"].iloc[3], 0.029, places=6)
        self.assertEqual(receiver.snapshot()[1]["gaps"], 0)
        self.assertEqual(raw["time"].iloc[-1], 0)
        self.assertEqual(set(pd.read_csv(save_analysis(receiver.directory))["segment"]), {0, 1})

    def test_csv_tail_preserves_all_disk_rows_and_skips_partial_row(self):
        receiver = self.receiver(False)
        for seq in range(1500):
            receiver._accept(parse_packet(packet(seq, seq * 20)), "127.0.0.1", seq / 50, seq / 50)
        receiver.stop()
        rows = read_capture_rows(receiver.csv_path, max_rows=25)
        self.assertEqual(len(rows), 25)
        self.assertEqual(int(rows[0]["seq"]), 1475)
        self.assertEqual(len(pd.read_csv(receiver.csv_path)), 1500)
        with receiver.csv_path.open("ab") as stream:
            stream.write(b"incomplete,record")
        self.assertEqual(read_capture_rows(receiver.csv_path, 25), rows)

    def test_background_receiver_expires_without_browser_polling(self):
        receiver = self.receiver(idle_timeout=0.05)
        receiver._thread.join(timeout=2)
        self.assertFalse(receiver.snapshot()[1]["running"])
        self.assertTrue(receiver._csv_file.closed)

    def test_csv_write_error_stops_instead_of_displaying_unsaved_sample(self):
        receiver = self.receiver()
        with patch.object(receiver._writer, "writerow", side_effect=OSError("disk full")):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(packet(), receiver.address)
            receiver._thread.join(timeout=2)
        rows, state = receiver.snapshot()
        self.assertFalse(state["running"])
        self.assertEqual(rows, [])
        self.assertEqual(state["packets"], 0)
        self.assertIn("disk full", state["error"])

    def test_replay_script_drives_live_display_without_hardware(self):
        """用真实采集回放替代硬件，验证接收→CSV→显示整条链路。"""
        from scripts.replay_capture import replay
        from streamlit.testing.v1 import AppTest
        source = ROOT / "data" / "recordings" / "2026-11-04" / "121723_291114_4da4bcc3"
        if not (source / "imu_raw.csv").exists():
            self.skipTest("缺少真实采集样本")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        with patch("beatdance.paths.DEFAULT_RECORDINGS", self.output):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
            app.sidebar.radio[0].set_value("UDP 实时采集").run()
            app.sidebar.number_input[0].set_value(port)
            app.sidebar.button[0].click().run()
            receiver = app.session_state["udp_receiver"]
            self.addCleanup(receiver.stop)
            sent, _ = replay(source, "127.0.0.1", port, speed=50, verbose=False)
            wait_for(receiver, "packets", sent)
            app.run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.metric[0].value, str(sent))
            peaks = int(app.metric[4].value)
            self.assertGreater(peaks, 0)
            # 六轴曲线 + 波峰图均为 matplotlib 图，Δt–t 图同样渲染。
            self.assertGreaterEqual(len(app.get("image")), 2)
            self.assertIn("波峰间隔随时间变化 · Δt–t", [item.value for item in app.subheader])
            app.sidebar.button[1].click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue((receiver.directory / "analysis_magnitude.csv").exists())

    def test_legacy_nested_firmware_packets_are_still_accepted(self):
        """仓库里的旧版 .ino 发嵌套 acc_g/gyro_dps/t_ms，重烧后不应全部判为无效。"""
        def legacy(seq=0, t_ms=1000, **changes):
            obj = dict(v=1, device="esp32s3-imu-01", seq=seq, t_ms=t_ms, imu_id="0x70",
                       acc_g=[0.0, 0.0, 1.0], gyro_dps=[1.0, -0.5, 0.15], temp_c=28.5)
            obj.update(changes)
            return json.dumps(obj).encode("utf-8")

        sample = parse_packet(legacy(seq=3, t_ms=1500))
        self.assertEqual(sample.seq, 3)
        self.assertEqual(sample.t_us, 1_500_000)
        self.assertAlmostEqual(sample.az, 1.0)
        for data in (legacy(acc_g=[0, 1]), legacy(gyro_dps=[0, 0, True]),
                     legacy(acc_g=[0, 0, float("nan")]), legacy(t_ms=UINT32)):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_packet(data)

        receiver = self.receiver(False)
        for seq, t_ms in enumerate((0, 20, 40, 60)):
            receiver._accept(parse_packet(legacy(seq, t_ms)), "127.0.0.1", 1 + seq / 50, 1 + seq / 50)
        receiver.stop()
        raw = pd.read_csv(receiver.csv_path)
        self.assertEqual(len(raw), 4)
        self.assertAlmostEqual(raw["time"].iloc[-1], 0.06, places=6)

    def test_ui_csv_live_stop_restart_and_history(self):
        from streamlit.testing.v1 import AppTest
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        with patch("beatdance.paths.DEFAULT_RECORDINGS", self.output):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25).run()
            self.assertEqual(len(app.exception), 0)
            app.sidebar.radio[0].set_value("UDP 实时采集").run()
            app.sidebar.number_input[0].set_value(port)
            app.sidebar.text_input[1].set_value("P001")
            app.sidebar.button[0].click().run()
            receiver = app.session_state["udp_receiver"]
            self.addCleanup(receiver.stop)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                for seq in range(80):
                    sender.sendto(packet(seq, 1000 + seq * 20), ("127.0.0.1", port))
                    time.sleep(0.001)
            wait_for(receiver, "packets", 80)
            app.run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.metric[0].value, "80")
            saved_time = float(read_capture_rows(receiver.csv_path)[-1]["time"])
            self.assertTrue(any(f"采集时间 {saved_time:.3f} s" in m.value for m in app.markdown))
            app.sidebar.button[1].click().run()
            self.assertEqual(len(app.exception), 0)
            original = receiver.csv_path.read_bytes()
            self.assertTrue((receiver.directory / "analysis_magnitude.csv").exists())
            app.sidebar.button[0].click().run()
            second = app.session_state["udp_receiver"]
            self.addCleanup(second.stop)
            self.assertNotEqual(second.directory, receiver.directory)
            app.sidebar.radio[0].set_value("历史采集").run()
            self.assertEqual(len(app.exception), 0)
            self.assertFalse(second.snapshot()[1]["running"])
            self.assertEqual(receiver.csv_path.read_bytes(), original)
            self.assertEqual(len(list(self.output.glob("*/*/imu_raw.csv"))), 2)


class SignalTests(unittest.TestCase):
    def test_original_simulated_algorithm_is_preserved(self):
        t = np.arange(0, 10, 1 / 50)
        raw = 9.81 + 1.2 * np.sin(2 * np.pi * 1.8 * t) + 0.8 * np.random.default_rng(42).standard_normal(len(t))
        expected = sosfiltfilt(butter(4, 5, fs=50, output="sos"), raw)
        peaks, _ = find_peaks(expected, distance=20, prominence=0.7)
        actual = analyze_acceleration(t, raw)
        np.testing.assert_allclose(actual["filtered_acceleration"], expected, atol=1e-10)
        np.testing.assert_array_equal(np.flatnonzero(actual["is_peak"]), peaks)

    def test_gap_is_not_interpolated_and_short_spans_have_no_peaks(self):
        t = np.r_[np.arange(0, 0.5, 0.02), np.arange(2, 2.5, 0.02)]
        result = analyze_acceleration(t, np.sin(t))
        self.assertEqual(result["span"].nunique(), 2)
        self.assertFalse(result["time"].between(0.6, 1.9).any())
        self.assertEqual(result["is_peak"].sum(), 0)

    def test_g_conversion_and_latest_segment_only(self):
        frame = pd.DataFrame(dict(segment=[0, 1, 1], time=[0, 0, 0.02],
                                  ax_g=[4, 0, 0], ay_g=[0, 0, 0], az_g=[0, 1, 1]))
        result = analyze_imu(frame)
        np.testing.assert_allclose(result["raw_acceleration"], STANDARD_GRAVITY)
        self.assertEqual(len(result), 2)

    def test_invalid_times_are_rejected(self):
        for times in ([0, 0], [1, 0], [0, float("nan")]):
            with self.subTest(times=times), self.assertRaises(ValueError):
                analyze_acceleration(times, [1, 1])


if __name__ == "__main__":
    unittest.main()
