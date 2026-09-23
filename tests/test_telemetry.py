import io
import threading
import time
import unittest
from unittest import mock

from macvitals.macvitals import AppleSMC, DataCollector, MonitorData, parse_args


def collector():
    c = DataCollector.__new__(DataCollector)
    c.data = MonitorData()
    c._pm_lock = threading.Lock()
    c._pm_data, c._pm_last_update = {}, 0
    c._aux_sensors, c._aux_fans = [], []
    c._pm_error = ""
    c._pm_proc = None
    return c


class TelemetryTests(unittest.TestCase):
    def test_source_metadata_describes_current_snapshot_only(self):
        c = collector()
        c.data.has_sudo = False
        c.data.source_status = {"cpu": "ok", "memory": "warming"}
        c._pm_last_update = 0
        c._update_source_meta(1.0)
        self.assertEqual(c.data.source_meta["cpu"]["source"], "Mach host_statistics")
        self.assertEqual(c.data.source_meta["cpu"]["meaning"], "整机 CPU ticks 差分")
        self.assertEqual(c.data.source_meta["memory"]["status"], "warming")
        self.assertNotIn("history", str(c.data.source_meta).lower())

    def test_dev_mode_is_opt_in(self):
        self.assertFalse(parse_args([]).dev)
        self.assertTrue(parse_args(["--dev"]).dev)

    def test_published_snapshot_cannot_be_mutated_by_ui(self):
        c = collector()
        c._snapshot_lock = threading.Lock()
        c._published = MonitorData()
        c._published.cpu_history.append(20)
        snapshot = c.snapshot()
        snapshot.cpu_history.append(80)
        snapshot.source_status['cpu'] = 'ok'
        self.assertEqual(list(c._published.cpu_history), [20])
        self.assertEqual(c._published.source_status, {})

    def test_expired_pm_temperature_and_pressure_become_unavailable(self):
        c = collector()
        c._parse_pm_sample('CPU die temperature: 60 C\nThermal Pressure: Heavy\n')
        c._merge_powermetrics()
        self.assertEqual(c.data.cpu.temp_c, 60)
        c._pm_last_update = time.monotonic() - 100
        c._merge_powermetrics()
        self.assertEqual(c.data.cpu.temp_c, 0)
        self.assertEqual(c.data.source_status['thermal'], 'unavailable')

    def test_m5_topology_uses_names_not_perflevel_positions(self):
        c = collector()
        values = {"machdep.cpu.brand_string": "Apple M5 Pro", "hw.ncpu": "18",
                  "hw.memsize": str(64 * 1024 ** 3), "hw.model": "Mac17,8",
                  "hw.nperflevels": "2", "hw.perflevel0.name": "Super",
                  "hw.perflevel0.logicalcpu": "6", "hw.perflevel1.name": "Performance",
                  "hw.perflevel1.logicalcpu": "12"}
        with mock.patch("subprocess.check_output", side_effect=lambda args, **kw: values[args[-1]].encode()):
            c._collect_sys_info()
        self.assertEqual(c.data.sys_info.core_groups, [("超级核心", 6), ("性能核心", 12)])
        self.assertEqual(c.data.sys_info.cpu_cores_e, 0)
        self.assertEqual(c.data.sys_info.total_cores, 18)

    def test_sensor_ids_are_stable_when_a_measurement_site_is_absent(self):
        names = AppleSMC._temperature_sensors("Apple M5 Pro", 12, 0, {"Tp00", "Tp08", "Tp0O", "TH0x"})
        self.assertIn(("Tp08", "CPU 超级核测点 3"), names)
        self.assertIn(("Tp0O", "CPU 性能核测点 1"), names)
        unknown = AppleSMC._temperature_sensors("Apple M9", 10, 2, {"TH0x", "Tp00"})
        self.assertEqual(unknown, [("TH0x", "SSD 闪存")])

    def test_numbered_clusters_and_gpu_residency(self):
        c = collector()
        c._parse_pm_sample("S0-Cluster HW active frequency: 4321 MHz\n"
                           "P1-Cluster HW active frequency: 2800 MHz\n"
                           "GPU HW active frequency: 900 MHz\nGPU HW active residency: 0.00%\n"
                           "CPU Power: 0 mW\nCurrent pressure level: Nominal\n")
        c._merge_powermetrics()
        self.assertEqual(c.data.cpu.cluster_freqs, {"S0": 4321, "P1": 2800})
        self.assertFalse(c.data.gpu.active)
        self.assertIn("cpu_power", c.data.pm_metrics)
        self.assertNotIn("gpu_power", c.data.pm_metrics)
        self.assertEqual(c.data.source_status["thermal"], "ok")

    def test_stream_only_commits_complete_frames(self):
        c = collector()
        c._running = True
        c._pm_proc = mock.Mock(stdout=io.StringIO(
            "Machine header\n*** Sample 1\nCPU Power: 1000 mW\n"
            "*** Sample 2\nCPU Power: 9999"))
        c._read_powermetrics()
        self.assertEqual(c._pm_data["cpu_power"], 1.0)
        c._pm_proc.poll.return_value = 1
        c._merge_powermetrics()
        self.assertEqual(c.data.pm_metrics, set())

    def test_disappearing_sensor_does_not_retain_old_temperature(self):
        c = collector()
        c._smc = mock.Mock()
        c._smc.snapshot.return_value = ([("CPU 性能核测点 1", 60.0)], [])
        with mock.patch("subprocess.check_output", return_value=b""):
            c._collect_aux_sensors()
            self.assertEqual(c.data.cpu.temp_c, 60)
            c._smc.snapshot.return_value = ([], [])
            c._collect_aux_sensors()
        self.assertEqual(c.data.cpu.temp_c, 0)
        self.assertEqual(c.data.source_status["sensors"], "unavailable")

    def test_invalid_intervals_are_rejected(self):
        for value in ("nan", "inf", "0", "-1", "11"):
            with self.subTest(value=value), mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                parse_args(["--interval", value])


if __name__ == "__main__":
    unittest.main()
