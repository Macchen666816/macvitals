"""Rendering assertions include cell width, missing values and scroll reachability."""
import importlib.util
import pathlib
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "monitor_ui_tests", pathlib.Path(__file__).parents[1] / "macvitals/macvitals.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class Screen:
    def __init__(self, h=24, w=80):
        self.h, self.w, self.rows = h, w, {}

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.rows.clear()

    def addstr(self, y, x, text, attr=0):
        width = sum(monitor.MonitorUI._cell_width(c) for c in text)
        assert 0 <= y < self.h
        assert x + width <= self.w - (y == self.h - 1)
        self.rows[y] = self.rows.get(y, "") + text

    def refresh(self):
        pass


class UIRegressionTests(unittest.TestCase):
    def ui(self, h=24, w=80):
        with mock.patch.object(monitor.curses, "has_colors", return_value=False):
            return monitor.MonitorUI(Screen(h, w), monitor.MonitorData())

    def test_unknown_metrics_are_not_zero_even_as_root(self):
        ui = self.ui()
        ui.data.has_sudo = True
        ui.data.pm_metrics = set()
        ui.page = ui.PAGE_GPU
        lines = ui._lines()
        self.assertIn("GPU 功率: --", lines)
        self.assertIn("活跃频率: --", lines)
        ui.data.pm_metrics = {"gpu_power"}
        self.assertIn("GPU 功率: 0.00 W", ui._lines())

    def test_developer_sources_are_opt_in_and_fit(self):
        ui = self.ui(12, 55)
        ui.data.developer_mode = False
        ui.data.source_meta = {"cpu": {"source": "Mach", "status": "ok", "age_s": 0.1}}
        self.assertIsNone(ui._developer_sources())
        ui.data.developer_mode = True
        ui.page = ui.PAGE_CPU
        ui.data.source_meta = {
            "cpu": {"source": "Mach host_statistics", "status": "ok", "age_s": 0.1},
            "thermal": {"source": "pmset", "status": "unavailable", "age_s": 0.1},
            "powermetrics": {"source": "powermetrics complete sample", "status": "warming", "age_s": None},
        }
        self.assertIn("DEV |", ui._developer_sources())
        ui.render()

    def test_all_pages_scroll_to_last_line_at_narrow_sizes(self):
        for h, w in [(5, 24), (8, 42), (24, 80)]:
            ui = self.ui(h, w)
            ui.data.source_status = {"processes": "ok"}
            ui.data.processes = [monitor.ProcessInfo(pid=n, name="中文进程" + str(n)) for n in range(35)]
            ui.data.thermal.sensors = [("CPU 性能核心测点 " + str(n), 52.) for n in range(35)]
            for page in range(6):
                ui.page = page
                ui.handle_key(monitor.curses.KEY_END)
                ui.render()
                last_spans = ui._layout(w)[-1]
                for _, text, _, _ in last_spans:
                    self.assertTrue(any(text in row for row in ui.stdscr.rows.values()))

    def test_eight_color_terminal_never_requests_color_eight(self):
        ui = self.ui()
        with mock.patch.object(monitor.curses, "has_colors", return_value=True), \
             mock.patch.object(monitor.curses, "start_color"), \
             mock.patch.object(monitor.curses, "use_default_colors"), \
             mock.patch.object(monitor.curses, "COLOR_PAIRS", 64, create=True), \
             mock.patch.object(monitor.curses, "init_pair") as pair:
            ui._init_colors()
        self.assertTrue(all(call.args[1] < 8 for call in pair.call_args_list))

    def test_basic_mode_keeps_temperature_trend(self):
        ui = self.ui()
        ui.data.cpu.temp_c = 52
        ui.data.cpu_temp_history.extend([50, 52])
        ui.page = ui.PAGE_CPU
        self.assertTrue(any("趋势" in row and "°C" in row for row in ui._lines()))

    def test_new_process_cpu_is_unknown_and_sorted_last(self):
        ui = self.ui()
        ui.page = ui.PAGE_PROCESSES
        ui.data.source_status = {"processes": "ok"}
        new = monitor.ProcessInfo(pid=1, name="new", cpu_pct=99)
        new.cpu_available = False
        sampled = monitor.ProcessInfo(pid=2, name="sampled", cpu_pct=3)
        sampled.cpu_available = True
        ui.data.processes = [new, sampled]
        rows = ui._lines()
        self.assertTrue(rows[-1].endswith("new"))
        self.assertIn("--", rows[-1])
        self.assertTrue(rows[-2].endswith("sampled"))

    def test_process_page_limits_rows_after_selected_sort(self):
        ui = self.ui()
        ui.page = ui.PAGE_PROCESSES
        ui.data.source_status = {"processes": "ok"}
        ui.data.processes = [
            monitor.ProcessInfo(
                pid=100 + index,
                name=f"process-{index}",
                cpu_pct=float(index),
                cpu_available=True,
                mem_mb=float(20 - index),
                mem_pct=float(20 - index),
            )
            for index in range(20)
        ]

        cpu_rows = ui._lines()[4:]
        self.assertEqual(len(cpu_rows), ui.PROCESS_DISPLAY_LIMIT)
        self.assertTrue(cpu_rows[0].endswith("process-19"))
        self.assertTrue(cpu_rows[-1].endswith("process-5"))

        ui.process_sort = "memory"
        memory_rows = ui._lines()[4:]
        self.assertEqual(len(memory_rows), ui.PROCESS_DISPLAY_LIMIT)
        self.assertTrue(memory_rows[0].endswith("process-0"))
        self.assertTrue(memory_rows[-1].endswith("process-14"))

    def test_cjk_wrap_uses_display_cells(self):
        self.assertEqual(monitor.MonitorUI._wrap("温度52°C", 4), ["温度", "52°C"])

    def test_overview_main_metrics_fit_normal_terminal_without_scroll(self):
        ui = self.ui(30, 100)
        ui.data.source_status = {source: "ok" for source in (
            "cpu", "memory", "memory_pressure", "swap", "disk", "disk_capacity", "network", "thermal", "uptime")}
        ui.data.pm_metrics = {"cpu_power", "gpu_power", "gpu_usage", "gpu_freq", "ane_power"}
        ui.data.thermal.fans = [("左风扇", 2200), ("右风扇", 2300)]
        ui.data.cpu.temp_c, ui.data.gpu.temp_c = 52, 48
        ui.render()
        visible = "\n".join(ui.stdscr.rows.values())
        for label in ("━━ CPU", "━━ GPU", "━━ 内存", "━━ 网络", "━━ 磁盘", "风扇 |", "开机时间:"):
            self.assertIn(label, visible)
        self.assertLessEqual(len(ui._layout(100)), 26)

    def test_panels_keep_colors_and_never_cross_columns(self):
        ui = self.ui(30, 100)
        for page in range(6):
            ui.page = page
            for width in (24, 55, 88, 100, 140):
                for spans in ui._layout(width):
                    previous_end = 0
                    for x, text, color, bold in spans:
                        self.assertGreaterEqual(x, previous_end)
                        previous_end = x + sum(ui._cell_width(c) for c in text)
                        self.assertLessEqual(previous_end, width - 1)
        ui.page = ui.PAGE_OVERVIEW
        titles = [span for row in ui._layout(100) for span in row if span[3]]
        self.assertTrue(any('CPU' in t[1] and t[2] == ui.COLOR_CHART for t in titles))
        self.assertTrue(any('GPU' in t[1] and t[2] == ui.COLOR_GPU for t in titles))
        self.assertTrue(any('内存' in t[1] and t[2] == ui.COLOR_GOOD for t in titles))
        self.assertTrue(any(len(row) == 2 for row in ui._layout(100)))
        self.assertTrue(all(len(row) <= 1 for row in ui._layout(55)))


if __name__ == "__main__":
    unittest.main()
