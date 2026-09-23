import importlib.util
import pathlib
import subprocess
import time
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).parents[1] / "macvitals" / "macvitals.py"
SPEC = importlib.util.spec_from_file_location("macvitals", MODULE_PATH)
macvitals = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macvitals)


def collector_for_test():
    collector = macvitals.DataCollector.__new__(macvitals.DataCollector)
    collector.data = macvitals.MonitorData()
    collector.data.source_status = {}
    collector.data.sys_info.cpu_cores_p = 6
    collector.data.sys_info.cpu_cores_e = 2
    collector.data.sys_info.total_cores = 8
    collector._prev_net = None
    collector._prev_net_time = None
    collector._prev_disk = None
    collector._prev_disk_time = None
    collector._primary_interface = None
    collector._interface_checked_at = 0.0
    collector._pm_data = {}
    collector._pm_last_update = 0.0
    collector._pm_lock = __import__("threading").Lock()
    collector._aux_sensors = []
    collector._aux_fans = []
    collector._smc = None
    return collector


class CollectorTests(unittest.TestCase):
    def test_process_rss_is_converted_from_kb_to_mb(self):
        collector = collector_for_test()
        output = (
            "458 Thu Sep 17 16:39:35 2026 0:01.00 133120 /Applications/Example App.app/Contents/MacOS/Example App\n"
            "152 Thu Sep 17 16:39:35 2026 0:02.00 226304 /System/Library/WindowServer\n"
        ).encode()

        with mock.patch.object(subprocess, "check_output", return_value=output):
            collector._collect_processes()

        self.assertEqual(collector.data.processes[0].pid, 458)
        self.assertFalse(collector.data.processes[0].cpu_available)
        self.assertAlmostEqual(collector.data.processes[0].mem_mb, 130.0)
        self.assertEqual(collector.data.processes[0].name, "Example App")

    def test_network_uses_only_the_unique_link_row(self):
        collector = collector_for_test()
        first = b"""Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
en0 1500 <Link#15> aa:bb 100 0 1048576 50 0 524288 0
en0 1500 192.168.1 192.168.1.2 100 - 1048576 50 - 524288 -
en0 1500 fe80:: fe80::1 100 - 1048576 50 - 524288 -
"""
        second = first.replace(b"1048576", b"2097152").replace(b"524288", b"1048576")

        netstat_samples = iter([first, second])

        def command_output(command, **_kwargs):
            if command[0] == "route":
                return b"   interface: en0\n"
            return next(netstat_samples)

        with mock.patch.object(subprocess, "check_output", side_effect=command_output):
            collector._collect_network(10.0)
            collector._collect_network(11.0)

        self.assertAlmostEqual(collector.data.net.in_mb_s, 1.0)
        self.assertAlmostEqual(collector.data.net.out_mb_s, 0.5)
        self.assertEqual(collector.data.net.in_packets, 100)

    def test_disk_uses_raw_read_write_counters(self):
        collector = collector_for_test()
        first = b'''"Statistics" = {"Bytes (Read)"=1048576,"Bytes (Write)"=2097152,"Operations (Read)"=10,"Operations (Write)"=20}'''
        second = b'''"Statistics" = {"Bytes (Read)"=3145728,"Bytes (Write)"=3145728,"Operations (Read)"=14,"Operations (Write)"=22}'''

        with mock.patch.object(subprocess, "check_output", side_effect=[first, second]):
            collector._collect_disk(20.0)
            collector._collect_disk(21.0)

        self.assertAlmostEqual(collector.data.disk.read_mb_s, 2.0)
        self.assertAlmostEqual(collector.data.disk.write_mb_s, 1.0)
        self.assertAlmostEqual(collector.data.disk.read_ops, 4.0)
        self.assertAlmostEqual(collector.data.disk.write_ops, 2.0)

    def test_powermetrics_sample_replaces_old_gpu_state(self):
        collector = collector_for_test()
        collector._parse_pm_sample(
            "P-Cluster HW active frequency: 2500 MHz\n"
            "E-Cluster HW active frequency: 1200 MHz\n"
            "CPU Power: 4000 mW\nGPU HW active frequency: 900 MHz\nGPU HW active residency: 25%\nGPU Power: 1500 mW\n"
            "CPU die temperature: 61.5 C\nGPU die temperature: 55.2 C\nFan 0 speed: 2200 rpm"
        )
        collector._merge_powermetrics()
        self.assertTrue(collector.data.gpu.active)
        self.assertEqual(collector.data.gpu.freq_mhz, 900)
        self.assertEqual(dict(collector.data.thermal.sensors)["CPU 温度"], 61.5)
        self.assertEqual(collector.data.thermal.fans[0][1], 2200)

        collector._parse_pm_sample("CPU Power: 800 mW\nGPU HW active frequency: 0 MHz\nGPU Power: 0 mW")
        collector._merge_powermetrics()
        self.assertFalse(collector.data.gpu.active)
        self.assertEqual(collector.data.gpu.freq_mhz, 0)
        self.assertAlmostEqual(collector.data.cpu.power_w, 0.8)

    def test_stale_powermetrics_values_are_cleared(self):
        collector = collector_for_test()
        collector.data.has_sudo = True
        collector.data.cpu.power_w = 10
        collector.data.gpu.active = True
        collector._pm_data = {"cpu_power": 10, "gpu_active": True}
        collector._pm_last_update = time.monotonic() - 10

        collector._merge_powermetrics()

        self.assertEqual(collector.data.cpu.power_w, 0)
        self.assertFalse(collector.data.gpu.active)

    def test_smc_snapshot_sets_average_temps_and_fans(self):
        collector = collector_for_test()
        collector._smc = mock.Mock()
        collector._smc.snapshot.return_value = (
            [("CPU 性能核心 1", 50.0), ("CPU 性能核心 2", 60.0), ("GPU 1", 48.0)],
            [("风扇 1", 1800.0)],
        )
        battery = b'    "Temperature" = 3000\n'

        with mock.patch.object(subprocess, "check_output", return_value=battery):
            collector._collect_aux_sensors()

        self.assertEqual(collector.data.cpu.temp_c, 55.0)
        self.assertEqual(collector.data.gpu.temp_c, 48.0)
        self.assertEqual(collector.data.thermal.fans, [("风扇 1", 1800.0)])
        self.assertIn(("电池", 30.0), collector.data.thermal.sensors)

    def test_m1_sensor_names_preserve_measurement_sites(self):
        available = {
            "Tp01", "Tp05", "Tp0D", "Tp0H", "Tp0L", "Tp0P", "Tp0X", "Tp0b",
            "Tp09", "Tp0T", "Tg05", "Tg0D", "Tm02", "Tm06", "TH0x", "TW0P",
            "Ts0P", "Ts1P", "TaLP", "TaRF",
        }

        sensors = macvitals.AppleSMC._temperature_sensors(
            "Apple M1 Pro", 6, 2, available
        )
        names = [name for _, name in sensors]

        self.assertEqual(names[:8], [f"CPU 性能核测点 {i}" for i in range(1, 9)])
        self.assertIn("CPU 能效核测点 2", names)
        self.assertIn("GPU 测点 1", names)
        self.assertIn("内存测点 2", names)
        self.assertIn("SSD 闪存", names)
        self.assertIn("Wi-Fi", names)
        self.assertIn("左掌托", names)
        self.assertIn("CPU 性能核测点 7", names)
        self.assertFalse(any(key in name for key in available for name in names))


class FakeScreen:
    def __init__(self, height, width):
        self.height = height
        self.width = width
        self.rows = []

    def erase(self):
        self.rows.clear()

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y, x, text, _attr=0):
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise macvitals.curses.error("outside screen")
        self.rows.append((y, x, text))

    def refresh(self):
        pass


class UITests(unittest.TestCase):
    def make_ui(self, height, width):
        ui = macvitals.MonitorUI.__new__(macvitals.MonitorUI)
        ui.stdscr = FakeScreen(height, width)
        ui.data = macvitals.MonitorData()
        ui.page = macvitals.MonitorUI.PAGE_OVERVIEW
        ui.paused = False
        ui.process_sort = "cpu"
        ui.sensor_scroll = 0
        return ui

    def test_render_does_not_fail_in_tiny_terminal(self):
        with mock.patch.object(macvitals.curses, "color_pair", return_value=0):
            for height, width in [(1, 1), (4, 20), (8, 42), (12, 55)]:
                for page in range(len(macvitals.MonitorUI.PAGE_NAMES)):
                    with self.subTest(size=(height, width), page=page):
                        ui = self.make_ui(height, width)
                        ui.page = page
                        ui.render()

    def test_process_and_sensor_keyboard_controls(self):
        ui = self.make_ui(24, 90)
        ui.page = macvitals.MonitorUI.PAGE_PROCESSES
        ui.handle_key(ord("m"))
        self.assertEqual(ui.process_sort, "memory")
        ui.handle_key(ord("c"))
        self.assertEqual(ui.process_sort, "cpu")
        ui.page = macvitals.MonitorUI.PAGE_SENSORS
        ui.handle_key(macvitals.curses.KEY_DOWN)
        self.assertEqual(ui.sensor_scroll, 1)
        ui.handle_key(macvitals.curses.KEY_UP)
        self.assertEqual(ui.sensor_scroll, 0)


if __name__ == "__main__":
    unittest.main()
