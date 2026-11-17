"""Native ABI, current-window metrics, coverage and App identity regressions."""
import ctypes
import errno
import unittest
from unittest import mock

from macvitals.process_monitor import (
    AppInfo, ProcessInfo, ProcessMonitor, ProcessUnavailable, RUsageInfoV0,
    _Libproc, aggregate_apps, app_bundle,
)


def usage(cpu=0, start=1, footprint=1048576, rss=2097152):
    result = RUsageInfoV0()
    result.ri_user_time = cpu
    result.ri_system_time = 0
    result.ri_proc_start_abstime = start
    result.ri_phys_footprint = footprint
    result.ri_resident_size = rss
    return result


class Backend:
    def __init__(self, rows):
        self.rows = rows
        self.timebase = (1, 1)

    def pids(self):
        return list(self.rows)

    def path(self, pid):
        return self.rows[pid][0]

    def name(self, pid):
        return self.rows[pid][1]

    def usage(self, pid):
        result = self.rows[pid][2]
        if isinstance(result, Exception):
            raise result
        return result


class ProcessMonitorTests(unittest.TestCase):
    def monitor(self, rows, timestamps=(10, 10.01, 11, 11.01, 12, 12.01)):
        backend = Backend(rows)
        monitor = ProcessMonitor(backend=backend, clock=mock.Mock(side_effect=timestamps))
        return monitor, backend

    def test_v0_layout_matches_darwin_sdk_and_native_flavor(self):
        self.assertEqual(ctypes.sizeof(RUsageInfoV0), 96)
        self.assertEqual(RUsageInfoV0.ri_user_time.offset, 16)
        self.assertEqual(RUsageInfoV0.ri_resident_size.offset, 64)
        self.assertEqual(RUsageInfoV0.ri_phys_footprint.offset, 72)
        self.assertEqual(RUsageInfoV0.ri_proc_start_abstime.offset, 80)
        backend = _Libproc.__new__(_Libproc)
        backend.lib = mock.Mock()
        backend.lib.proc_pid_rusage.return_value = 0
        backend.usage(42)
        self.assertEqual(backend.lib.proc_pid_rusage.call_args.args[:2], (42, 0))

    def test_nanosecond_cpu_delta_and_pid_reuse_not_mach_ticks(self):
        rows = {42: ("/Applications/Example.app/Contents/MacOS/Example", "Example", usage(cpu=1000000000, start=29))}
        monitor, backend = self.monitor(rows)
        first = monitor.collect(total_cores=8, ram_gb=16)
        self.assertEqual(first.status, "warming")
        self.assertFalse(first.processes[0].cpu_available)
        backend.rows[42] = (*backend.rows[42][:2], usage(cpu=3000000000, start=29))
        second = monitor.collect(total_cores=8, ram_gb=16)
        self.assertEqual(second.status, "ok")
        self.assertEqual(second.processes[0].cpu_pct, 25.0)
        self.assertEqual(second.processes[0].mem_mb, 1.0)
        self.assertEqual(second.processes[0].rss_mb, 2.0)
        self.assertEqual(second.processes[0].mem_pct, 100 / (16 * 1024))
        backend.rows[42] = (*backend.rows[42][:2], usage(cpu=3000000001, start=30))
        reused = monitor.collect(total_cores=8, ram_gb=16)
        self.assertFalse(reused.processes[0].cpu_available)

    def test_counter_regression_and_zero_elapsed_are_unknown(self):
        monitor, backend = self.monitor({42: ("/bin/task", "task", usage(cpu=100))},
                                        timestamps=(10, 10, 11, 11, 11, 11))
        monitor.collect()
        backend.rows[42] = ("/bin/task", "task", usage(cpu=99))
        self.assertFalse(monitor.collect().processes[0].cpu_available)
        backend.rows[42] = ("/bin/task", "task", usage(cpu=1000))
        self.assertFalse(monitor.collect().processes[0].cpu_available)

    def test_cpu_converts_apple_silicon_mach_ticks_to_nanoseconds(self):
        monitor, backend = self.monitor({42: ("/bin/task", "task", usage(cpu=24000000))})
        monitor._timebase_numer, monitor._timebase_denom = 125, 3
        monitor.collect(total_cores=8)
        backend.rows[42] = ("/bin/task", "task", usage(cpu=72000000))
        process = monitor.collect(total_cores=8).processes[0]
        self.assertEqual(process.cpu_pct, 25.0)

    def test_pid_reused_during_scan_does_not_receive_old_app_identity(self):
        monitor, backend = self.monitor({42: ("/A.app/Contents/MacOS/A", "A", usage())})
        with mock.patch.object(backend, "usage", side_effect=[usage(start=1), usage(start=2)]):
            result = monitor.collect()
        process = result.processes[0]
        self.assertFalse(process.mem_available)
        self.assertFalse(process.cpu_available)
        self.assertFalse(process.app_key)
        self.assertFalse(result.apps)
        self.assertEqual(process.read_status, "changed")

    def test_same_name_apps_have_separate_path_keys_helpers_stay_in_parent(self):
        outer = "/Applications/Example.app"
        other = "/Users/me/Applications/Example.app"
        monitor, _ = self.monitor({
            1: (outer + "/Contents/MacOS/Example", "Example", usage(footprint=10)),
            2: (outer + "/Contents/Frameworks/Helper.app/Contents/MacOS/Helper", "Helper", usage(footprint=20)),
            3: (other + "/Contents/MacOS/Example", "Example", usage(footprint=30)),
            4: ("/opt/node/bin/node", "node", usage()),
            5: ("/usr/local/bin/node", "node", usage()),
            6: ("/System/Library/Frameworks/WebKit.framework/XPCServices/com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent", "com.apple.WebKit.WebContent", usage()),
        })
        result = monitor.collect()
        self.assertEqual(len(result.apps), 2)
        groups = {group.key: group for group in result.apps}
        self.assertEqual(len(groups[outer].processes), 2)
        self.assertEqual(groups[outer].mem_mb, 30 / 1048576)
        self.assertEqual(len(groups[other].processes), 1)
        self.assertFalse(result.processes[3].app_key)
        self.assertFalse(result.processes[4].app_key)
        self.assertFalse(result.processes[5].app_key)

    def test_outer_bundle_requires_path_component_and_full_identity(self):
        self.assertEqual(app_bundle("/Applications/A.app/Contents/Frameworks/B.app/X"), "/Applications/A.app")
        self.assertEqual(app_bundle("/Applications/A.APP/Contents/MacOS/A"), "/Applications/A.APP")
        self.assertEqual(app_bundle("/Applications/A.app"), "/Applications/A.app")
        self.assertEqual(app_bundle("relative/A.app/Contents/MacOS/A"), "")
        self.assertEqual(app_bundle("/tmp/A.app-data/A"), "")

    def test_failures_are_classified_and_preserve_unknown_rows(self):
        root = "/Applications/A.app/Contents/MacOS/A"
        monitor, _ = self.monitor({
            1: (root, "A", usage()),
            2: (root, "A Helper", PermissionError(errno.EPERM, "not permitted")),
            3: ("", "", ProcessLookupError(errno.ESRCH, "gone")),
            4: ("/bin/other", "other", OSError(errno.EIO, "error")),
        })
        snapshot = monitor.collect()
        self.assertEqual(snapshot.status, "partial")
        self.assertEqual((snapshot.enumerated_count, snapshot.readable_count), (4, 1))
        self.assertEqual((snapshot.denied_count, snapshot.exited_count, snapshot.error_count), (1, 1, 1))
        self.assertEqual(snapshot.path_unavailable_count, 1)
        self.assertEqual([row.read_status for row in snapshot.processes], ["ok", "denied", "exited", "error"])
        self.assertFalse(snapshot.processes[1].mem_available)
        self.assertIsNone(snapshot.processes[1].rss_mb)
        self.assertEqual(snapshot.apps[0].mem_mb, 1.0)
        self.assertTrue(snapshot.apps[0].mem_available)
        self.assertFalse(snapshot.apps[0].mem_complete)

    def test_group_unknown_and_partial_cpu_do_not_become_confirmed_zero(self):
        group_members = [
            ProcessInfo(pid=1, name="a", app_key="/A.app", app_name="A", cpu_pct=12, cpu_available=True, mem_mb=2),
            ProcessInfo(pid=2, name="b", app_key="/A.app", app_name="A", cpu_pct=99, cpu_available=False, mem_available=False),
        ]
        group = aggregate_apps(group_members, ram_gb=16)[0]
        self.assertEqual(group.cpu_pct, 12)
        self.assertTrue(group.cpu_available)
        self.assertFalse(group.cpu_complete)
        self.assertEqual(group.mem_mb, 2)
        self.assertFalse(group.mem_complete)
        unknown = aggregate_apps([group_members[1]])[0]
        self.assertFalse(unknown.mem_available)
        self.assertFalse(unknown.cpu_available)

    def test_no_top15_limit_in_collector_before_aggregation(self):
        rows = {pid: ("/A.app/Contents/MacOS/A", "A", usage(footprint=1048576)) for pid in range(1, 36)}
        monitor, _ = self.monitor(rows)
        result = monitor.collect()
        self.assertEqual(len(result.processes), 35)
        self.assertEqual(len(result.apps[0].processes), 35)
        self.assertEqual(result.apps[0].mem_mb, 35)

    def responsibility_monitor(self, target_usage=None):
        xpc = "/System/Library/Frameworks/WebKit.framework/XPCServices/WebContent.xpc/Contents/MacOS/WebContent"
        monitor, backend = self.monitor({
            1: ("/Applications/Safari.app/Contents/MacOS/Safari", "Safari", target_usage or usage(start=1)),
            2: (xpc, "WebContent", usage(start=2)),
            3: ("/opt/bin/node", "node", usage(start=3)),
            4: ("/Applications/Other.app/Contents/MacOS/Other", "Other", usage(start=4)),
        })
        backend.responsibility_available = True
        backend.responsible_pid = mock.Mock(return_value=1)
        return monitor, backend

    def test_optional_responsibility_maps_system_xpc_not_cli_or_other_bundle(self):
        monitor, backend = self.responsibility_monitor()
        result = monitor.collect()
        rows = {process.pid: process for process in result.processes}
        self.assertEqual(rows[1].app_attribution, "bundle")
        self.assertEqual(rows[2].app_attribution, "responsibility")
        self.assertEqual(rows[2].app_key, rows[1].app_key)
        self.assertFalse(rows[3].app_key)
        self.assertEqual(rows[4].app_key, "/Applications/Other.app")
        backend.responsible_pid.assert_called_once_with(2)
        safari = next(group for group in result.apps if group.key == rows[1].app_key)
        self.assertEqual(len(safari.processes), 2)

    def test_invalid_responsible_pid_never_guesses_an_app(self):
        for responsible_pid in (0, -1, 2, 999999):
            with self.subTest(pid=responsible_pid):
                monitor, backend = self.responsibility_monitor()
                backend.responsible_pid.return_value = responsible_pid
                result = monitor.collect()
                self.assertFalse(result.processes[1].app_key)

    def test_system_cryptex_xpc_paths_receive_responsibility_attribution(self):
        for prefix in ("/System/Volumes/Preboot/Cryptexes/Incoming/OS/System/Library/",
                       "/System/Volumes/Preboot/Cryptexes/OS/System/Library/",
                       "/System/Volumes/Preboot/Cryptexes/App/System/Library/"):
            with self.subTest(prefix=prefix):
                monitor, backend = self.responsibility_monitor()
                old = backend.rows[2]
                backend.rows[2] = (prefix + "Frameworks/WebKit.framework/XPCServices/WebContent.xpc/Contents/MacOS/WebContent", old[1], old[2])
                result = monitor.collect()
                self.assertEqual(result.processes[1].app_attribution, "responsibility")

    def test_user_directory_resembling_system_xpc_is_not_reassigned(self):
        monitor, backend = self.responsibility_monitor()
        old = backend.rows[2]
        backend.rows[2] = ("/Users/me/System/Library/XPCServices/Other.xpc/Contents/MacOS/Other", old[1], old[2])
        result = monitor.collect()
        self.assertFalse(result.processes[1].app_key)
        backend.responsible_pid.assert_not_called()

    def test_responsibility_missing_or_raising_keeps_native_snapshot(self):
        monitor, backend = self.responsibility_monitor()
        backend.responsibility_available = False
        result = monitor.collect()
        self.assertFalse(result.responsibility_available)
        self.assertFalse(result.processes[1].app_key)
        backend.responsible_pid.assert_not_called()
        backend.responsibility_available = True
        backend.responsible_pid.side_effect = OSError("optional API failed")
        result = monitor.collect()
        self.assertFalse(result.processes[1].app_key)
        self.assertEqual(result.status, "ok")

    def test_responsibility_target_must_have_readable_stable_identity(self):
        monitor, _ = self.responsibility_monitor(PermissionError(errno.EPERM, "denied"))
        result = monitor.collect()
        self.assertFalse(result.processes[1].app_key)
        monitor, backend = self.responsibility_monitor()
        original_usage = backend.usage
        reads = {}
        def replacement(pid):
            reads[pid] = reads.get(pid, 0) + 1
            if pid == 1 and reads[pid] > 2:
                return usage(start=999)
            return original_usage(pid)
        backend.usage = replacement
        result = monitor.collect()
        self.assertFalse(result.processes[1].app_key)

    def test_missing_runtime_responsibility_symbol_is_optional(self):
        library = mock.Mock()
        system = mock.Mock()
        def timebase(pointer):
            pointer._obj.numer, pointer._obj.denom = 125, 3
            return 0
        system.mach_timebase_info.side_effect = timebase
        with mock.patch("macvitals.process_monitor.sys.platform", "darwin"), \
             mock.patch("macvitals.process_monitor.ctypes.CDLL", side_effect=[library, system, object()]):
            backend = _Libproc()
        self.assertFalse(backend.responsibility_available)
        self.assertEqual(backend.responsible_pid(123), 0)
        self.assertEqual(backend.timebase, (125, 3))

    def test_baseline_and_display_cache_only_track_current_set(self):
        monitor, backend = self.monitor({1: ("/A.app/Contents/MacOS/A", "A", usage())})
        with mock.patch("macvitals.process_monitor.os.path.getsize", return_value=200), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch("macvitals.process_monitor.plistlib.load", return_value={"CFBundleDisplayName": "A\x1b[31m\n"}):
            first = monitor.collect()
        self.assertNotIn("\x1b", first.apps[0].name)
        self.assertNotIn("\n", first.apps[0].name)
        self.assertEqual(set(monitor._names), {"/A.app"})
        backend.rows = {2: ("/B.app/Contents/MacOS/B", "B", usage(start=2))}
        monitor.collect()
        self.assertEqual(set(monitor._previous), {(2, 2)})
        self.assertEqual(set(monitor._names), {"/B.app"})
        backend.rows = {}
        with self.assertRaises(ProcessUnavailable):
            monitor.collect()
        self.assertEqual(monitor._previous, {})
        self.assertEqual(monitor._names, {})

    def test_display_metadata_is_optional_and_bounded(self):
        monitor, _ = self.monitor({})
        with mock.patch("macvitals.process_monitor.os.path.getsize", return_value=2 * 1024**2), \
             mock.patch("builtins.open") as opened:
            self.assertEqual(monitor._app_name("/A.app"), "A")
        opened.assert_not_called()

    def test_enumeration_grows_when_process_set_fills_buffer(self):
        backend = _Libproc.__new__(_Libproc)
        backend.lib = mock.Mock()
        capacities = []
        def listing(buffer, size):
            if buffer is None:
                return 4
            capacities.append(len(buffer))
            if len(capacities) == 1:
                return len(buffer)
            buffer[0], buffer[1], buffer[2], buffer[3] = 42, 42, 0, 43
            return 4
        backend.lib.proc_listallpids.side_effect = listing
        self.assertEqual(backend.pids(), [42, 43])
        self.assertGreater(capacities[1], capacities[0])

    def test_enumeration_supports_more_than_original_8192_pids(self):
        backend = _Libproc.__new__(_Libproc)
        backend.lib = mock.Mock()
        def listing(buffer, size):
            if buffer is None:
                return 9000
            for index in range(9000):
                buffer[index] = index + 1
            return 9000
        backend.lib.proc_listallpids.side_effect = listing
        self.assertEqual(len(backend.pids()), 9000)


if __name__ == "__main__":
    unittest.main()
