"""Rendering assertions include cell width, missing values and scroll reachability."""
import importlib.util
import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock
from macvitals.process_monitor import AppInfo, ProcessSnapshot, aggregate_apps

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
            for page in range(len(ui.PAGE_NAMES)):
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
        ui.process_view = "processes"
        ui.data.source_status = {"processes": "ok"}
        new = monitor.ProcessInfo(pid=1, name="new", cpu_pct=99)
        new.cpu_available = False
        sampled = monitor.ProcessInfo(pid=2, name="sampled", cpu_pct=3)
        sampled.cpu_available = True
        ui.data.processes = [new, sampled]
        rows = ui._process_rows(ui.data.processes)
        self.assertTrue(rows[-1].endswith("new"))
        self.assertIn("--", rows[-1])
        self.assertTrue(rows[-2].endswith("sampled"))

    def test_process_page_limits_rows_after_selected_sort(self):
        ui = self.ui()
        ui.page = ui.PAGE_PROCESSES
        ui.process_view = "processes"
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

        cpu_rows = ui._process_rows(ui.data.processes)
        self.assertEqual(len(cpu_rows), ui.PROCESS_DISPLAY_LIMIT)
        self.assertTrue(cpu_rows[0].endswith("process-19"))
        self.assertTrue(cpu_rows[-1].endswith("process-5"))

        ui.process_sort = "memory"
        memory_rows = ui._process_rows(ui.data.processes)
        self.assertEqual(len(memory_rows), ui.PROCESS_DISPLAY_LIMIT)
        self.assertTrue(memory_rows[0].endswith("process-0"))
        self.assertTrue(memory_rows[-1].endswith("process-14"))

    def test_app_view_aggregates_before_top_15_and_sorts_missing_last(self):
        ui = self.ui()
        ui.page = ui.PAGE_PROCESSES
        ui.data.source_status["processes"] = "partial"
        processes = [monitor.ProcessInfo(
            pid=i, name=f"helper-{i}", app_key="/Big.app", app_name="Big",
            app_path="/Big.app", mem_mb=2., cpu_pct=.5, cpu_available=True)
            for i in range(30)]
        processes += [monitor.ProcessInfo(
            pid=100 + i, name=f"small-{i}", app_key=f"/{i}.app", app_name=f"App{i}",
            mem_mb=10., cpu_pct=1., cpu_available=True) for i in range(20)]
        processes.append(monitor.ProcessInfo(pid=900, name="unreadable", app_key="/Unknown.app",
                                           app_name="Unknown", mem_available=False))
        ui.data.processes = processes
        ui.data.process_sample = ProcessSnapshot(processes=processes, apps=aggregate_apps(processes),
                                                enumerated_count=51, readable_count=50)
        self.assertEqual(ui.process_view, "apps")
        self.assertEqual(len(ui._process_apps()), 15)
        self.assertEqual(ui._process_apps()[0].name, "Big")
        ui.handle_key(ord("m"))
        self.assertEqual(ui._process_apps()[0].mem_mb, 60.)
        self.assertEqual(ui._process_apps()[0].name, "Big")
        self.assertNotIn("Unknown", [app.name for app in ui._process_apps()])
        self.assertIn("部分进程本轮未读取", "\n".join(ui._lines()))

    def test_app_selection_is_stable_and_details_survive_sorting_and_resize(self):
        ui = self.ui()
        ui.page = ui.PAGE_PROCESSES
        a = AppInfo(key="/a.app", name="甲应用", path="/a.app", cpu_pct=2.,
                    cpu_available=True, mem_mb=1., mem_available=True)
        b = AppInfo(key="/b.app", name="乙应用", path="/b.app", cpu_pct=1.,
                    cpu_available=True, mem_mb=2., mem_available=True)
        ui.data.process_sample.apps = [a, b]
        ui.data.source_status["processes"] = "ok"
        ui._lines()
        self.assertEqual(ui.app_selected_key, a.key)
        ui.handle_key(ord("n"))
        ui.handle_key(10)
        self.assertEqual(ui.app_selected_key, b.key)
        self.assertTrue(ui.app_expanded)
        ui.handle_key(ord("m"))
        self.assertEqual(ui.app_selected_key, b.key)
        self.assertIn("应用详情 / 乙应用", "\n".join(ui._lines()))
        for h, w in ((5, 24), (8, 42), (24, 80)):
            ui.stdscr = Screen(h, w)
            ui.handle_key(monitor.curses.KEY_END)
            ui.render()
        ui.handle_key(ord("v"))
        self.assertEqual(ui.process_view, "processes")
        ui.handle_key(ord("v"))
        self.assertEqual(ui.app_selected_key, b.key)
        ui.data.process_sample.apps = [a]
        ui._lines()
        self.assertEqual(ui.app_selected_key, a.key)
        ui.handle_key(27)
        self.assertFalse(ui.app_expanded)

    def test_partial_app_totals_and_unknown_process_memory_are_explicit(self):
        ui = self.ui()
        ui.page = ui.PAGE_PROCESSES
        known = monitor.ProcessInfo(pid=1, name="known", app_key="a", app_name="A",
                                    cpu_available=True, cpu_pct=3., mem_mb=20.)
        unknown = monitor.ProcessInfo(pid=2, name="unknown", app_key="a", app_name="A",
                                      mem_available=False, mem_mb=900.)
        ui.data.processes = [known, unknown]
        ui.data.process_sample = ProcessSnapshot(apps=aggregate_apps(ui.data.processes),
                                                enumerated_count=2, readable_count=1)
        ui.data.source_status["processes"] = "partial"
        text = "\n".join(ui._lines())
        self.assertIn("≥3.0%", text)
        self.assertIn("≥20.0", text)
        self.assertIn("当前可读成员的合计", text)
        ui.handle_key(ord("v"))
        text = "\n".join(ui._lines())
        self.assertIn("unknown", text)
        self.assertNotIn("900.0", text)
        self.assertIn("--", text)

    def test_process_rss_fallback_never_makes_a_fake_app_total(self):
        ui = self.ui()
        ui.page = ui.PAGE_PROCESSES
        ui.data.source_status["processes"] = "ok"
        ui.data.process_sample = ProcessSnapshot(memory_kind="rss")
        self.assertIn("应用聚合暂不可用", "\n".join(ui._lines()))
        ui.handle_key(ord("v"))
        self.assertIn("RSS", "\n".join(ui._lines()))

    def test_process_controls_live_only_in_bottom_bar(self):
        ui = self.ui(24, 100)
        ui.page = ui.PAGE_PROCESSES
        ui.data.source_status["processes"] = "ok"
        ui.data.process_sample.apps = [AppInfo(key="/A.app", name="A", path="/A.app")]
        blocks = ui._process_blocks()
        panel_text = "\n".join(line for _, _, lines in blocks for line in lines)
        self.assertNotIn("Enter", panel_text)
        self.assertNotIn("n/b", panel_text)
        self.assertNotIn("统计范围", " ".join(title for title, _, _ in blocks))
        ui.render()
        footer = ui.stdscr.rows[23]
        self.assertIn("n/b选择", footer)
        self.assertIn("Enter展开", footer)
        self.assertNotIn("不等于", panel_text)
        self.assertNotIn("实际值可能更高", panel_text)

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
        for page in range(len(ui.PAGE_NAMES)):
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

    @staticmethod
    def container(name, **values):
        defaults = dict(id=name, name=name, image="example:latest", project="", service="",
                        state="running", health=None, status_text="Up 1 hour", ports="",
                        started_at="2026-10-31T12:00:00Z", restart_count=0,
                        cpu_pct=None, memory_bytes=None, memory_limit_bytes=None,
                        network_rx_bytes=None, network_tx_bytes=None, block_read_bytes=None,
                        block_write_bytes=None, pids=None)
        defaults.update(values)
        return SimpleNamespace(**defaults)

    @staticmethod
    def docker(ui, containers=(), **values):
        defaults = dict(status="ok", message="", context="orbstack", engine="OrbStack",
                        engine_cpus=8, engine_memory_bytes=10 * 1024**3,
                        containers=list(containers), sampled_at=1., collection_seconds=2.)
        defaults.update(values)
        ui.data.docker = SimpleNamespace(**defaults)
        ui.page = ui.PAGE_DOCKER
        return ui.data.docker

    def test_docker_page_navigation_and_independent_sorting(self):
        ui = self.ui()
        ui.handle_key(ord("7"))
        self.assertEqual(ui.page, ui.PAGE_DOCKER)
        ui.handle_key(monitor.curses.KEY_RIGHT)
        self.assertEqual(ui.page, ui.PAGE_OVERVIEW)
        ui.handle_key(monitor.curses.KEY_LEFT)
        self.assertEqual(ui.page, ui.PAGE_DOCKER)
        ui.handle_key(ord("m"))
        self.assertEqual(ui.docker_sort, "memory")
        self.assertEqual(ui.process_sort, "cpu")

    def test_docker_unknown_and_stale_metrics_never_become_zero(self):
        ui = self.ui()
        unknown = self.container("unknown")
        sampled = self.container("sampled", cpu_pct=250., memory_bytes=4 * 1024**2)
        snapshot = self.docker(ui, [unknown, sampled])
        text = "\n".join(ui._lines())
        self.assertIn("250.0%", text)
        self.assertIn("4.0 MiB", text)
        self.assertNotIn("0.0%", text.replace("250.0%", ""))
        self.assertIn("100% ≈ 1 个虚拟核", text)
        self.assertIn("不是 Mac 整机容量", text)
        snapshot.status = "stale"
        text = "\n".join(ui._lines())
        self.assertNotIn("250.0%", text)
        self.assertNotIn("4.0 MiB", text)
        self.assertNotIn("运行中", text)
        self.assertIn("旧读数已失效", text)

    def test_docker_health_never_overrides_stopped_state(self):
        ui = self.ui()
        stopped = self.container("stopped-db", state="exited", health="unhealthy", cpu_pct=42.)
        running = self.container("running-web")
        self.docker(ui, [stopped, running])
        self.assertEqual(ui._docker_state(stopped), "已停止")
        self.assertIsNone(ui._docker_metric(stopped, "cpu_pct"))
        ui.docker_selected_id = running.id
        ui.handle_key(ord("d"))
        text = "\n".join(ui._lines())
        self.assertNotIn("健康检查失败", text)
        self.assertIn("未配置健康检查", text)
        self.assertIn("运行中健康检查失败 0", "\n".join(ui._docker_summary()))

    def test_docker_grouping_global_sort_and_selection_survive_refresh(self):
        ui = self.ui()
        a = self.container("a", project="alpha", cpu_pct=0., memory_bytes=90)
        b = self.container("b", project="beta", cpu_pct=200., memory_bytes=10)
        c = self.container("c", project="alpha", cpu_pct=None, memory_bytes=None)
        snapshot = self.docker(ui, [c, b, a])
        self.assertEqual([item.id for item in ui._docker_containers()], ["a", "c", "b"])
        ui.handle_key(ord("g"))
        self.assertEqual([item.id for item in ui._docker_containers()], ["b", "a", "c"])
        ui.handle_key(ord("m"))
        self.assertEqual([item.id for item in ui._docker_containers()], ["a", "b", "c"])
        ui._docker_blocks()
        ui.handle_key(ord("n"))
        self.assertEqual(ui.docker_selected_id, "b")
        self.assertGreater(ui._scroll[ui.PAGE_DOCKER], 100)
        snapshot.containers = [a, c, b]
        ui._docker_blocks()
        self.assertEqual(ui.docker_selected_id, "b")
        snapshot.containers = [a, c]
        ui._docker_blocks()
        self.assertEqual(ui.docker_selected_id, "a")
        ui.handle_key(ord("b"))
        self.assertEqual(ui.docker_selected_id, "c")

    def test_docker_overview_is_optional_and_handles_stale_connection(self):
        ui = self.ui()
        self.docker(ui, status="not_installed", message="未找到 Docker 客户端")
        ui.page = ui.PAGE_OVERVIEW
        self.assertFalse(any(title == "Docker" for title, _, _ in ui._blocks()))
        ui.data.docker.status = "ok"
        self.assertTrue(any(title == "Docker" for title, _, _ in ui._blocks()))
        ui.data.docker.status = "stale"
        self.assertIn("连接中断", "\n".join(ui._docker_summary()))

    def test_docker_many_containers_and_long_names_remain_scrollable(self):
        for h, w in ((5, 24), (8, 42), (24, 80), (30, 100)):
            ui = self.ui(h, w)
            self.docker(ui, [self.container("中文容器" * 8 + str(i), project="服务项目" * 3,
                                           cpu_pct=float(i)) for i in range(25)])
            self.assertEqual(len(ui._docker_containers()), 25)
            ui.handle_key(monitor.curses.KEY_END)
            ui.render()
            self.assertTrue(all(len(row) <= 1 for row in ui._layout(w)))
            for _, text, _, _ in ui._layout(w)[-1]:
                self.assertTrue(any(text in row for row in ui.stdscr.rows.values()))
            self.assertEqual(len(ui._docker_containers()), 25)

    def test_compose_project_view_is_read_only_and_aggregates_known_values(self):
        ui = self.ui()
        a = self.container("api-1", project="alpha", service="api", cpu_pct=12.5,
                           memory_bytes=100, state="running")
        b = self.container("api-2", project="alpha", service="api", cpu_pct=None,
                           memory_bytes=None, state="running")
        c = self.container("db-1", project="alpha", service="db", state="exited")
        independent = self.container("standalone", project="", service="")
        self.docker(ui, [a, b, c, independent])
        ui.handle_key(ord("v"))
        self.assertEqual(ui.docker_view, "projects")
        selected_before = ui.docker_selected_id
        ui.handle_key(ord("n"))
        self.assertEqual(ui.docker_selected_id, selected_before)
        self.assertEqual(ui.docker_project_selected, "alpha")
        text = "\n".join(line for _, _, rows in ui._docker_blocks() for line in rows)
        self.assertIn("alpha", text)
        self.assertNotIn("standalone", text)
        ui.handle_key(10)
        ui.handle_key(10)
        blocks = ui._docker_blocks()
        service = next(rows for title, _, rows in blocks if title.startswith("服务 / api"))
        self.assertIn("CPU 合计 12.5%", service[0])
        self.assertIn("读数不完整", service[0])
        ui.handle_key(ord("o"))
        self.assertIsNone(ui._docker_dialog)
        self.assertIn("只读", ui._docker_notice)

    def test_docker_page_hides_secondary_details_and_command_guide_by_default(self):
        ui = self.ui()
        item = self.container("service", project="alpha", cpu_pct=10.0)
        self.docker(ui, [item])
        blocks = ui._docker_blocks()
        titles = [title for title, _, _ in blocks]
        self.assertNotIn("常用 Docker 命令 / 仅供复制", titles)
        self.assertFalse(any(title.startswith("容器详情 /") for title in titles))
        ui.handle_key(ord("d"))
        self.assertTrue(any(title.startswith("容器详情 /") for title, _, _ in ui._docker_blocks()))
        ui.handle_key(ord("h"))
        self.assertIn("常用 Docker 命令 / 仅供复制", [title for title, _, _ in ui._docker_blocks()])


if __name__ == "__main__":
    unittest.main()
