"""Regression tests for measured deltas and machine-dependent command formats."""
import importlib.util
import pathlib
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('sampling_monitor', pathlib.Path(__file__).parents[1] / 'macvitals' / 'macvitals.py')
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def collector():
    c = monitor.DataCollector.__new__(monitor.DataCollector)
    c.data = monitor.MonitorData()
    c.data.source_status = {}
    c.data.sys_info.total_cores = 8
    c.data.sys_info.ram_gb = 16
    c._primary_interface = None
    c._interface_checked_at = 0
    c._prev_net = c._prev_net_time = None
    c._prev_disk = c._prev_disk_time = None
    return c


class SamplingTests(unittest.TestCase):
    def test_cpu_uses_tick_deltas_and_first_frame_is_unknown(self):
        c = collector()
        samples = iter([(100, 50, 800, 50), (120, 60, 860, 60)])
        def sample(_host, _flavor, ticks, _count):
            for i, value in enumerate(next(samples)):
                ticks[i] = value
            return 0
        c._cpu_lib = mock.Mock()
        c._cpu_lib.host_statistics.side_effect = sample
        with mock.patch.object(monitor.os, 'getloadavg', return_value=(1, 2, 3)):
            c._collect_cpu()
            self.assertEqual(c.data.source_status['cpu'], 'warming')
            c._collect_cpu()
        self.assertEqual(c.data.cpu.user, 30)
        self.assertEqual(c.data.cpu.sys, 10)
        self.assertEqual(c.data.cpu.idle, 60)

    def test_process_cpu_delta_and_pid_reuse(self):
        c = collector()
        rows = [b'42 Mon Sep 21 10:00:00 2026 0:01.00 131072 /App Name\n',
                b'42 Mon Sep 21 10:00:00 2026 0:03.00 131072 /App Name\n',
                b'42 Mon Sep 21 10:00:05 2026 0:00.01 131072 /App Name\n']
        with mock.patch.object(monitor.subprocess, 'check_output', side_effect=rows), \
                mock.patch.object(monitor.time, 'monotonic', side_effect=[1, 2, 3]):
            c._collect_processes()
            self.assertEqual(c.data.source_status['processes'], 'warming')
            c._collect_processes()
            self.assertEqual(c.data.processes[0].cpu_pct, 25)
            self.assertEqual(c.data.processes[0].mem_mb, 128)
            self.assertEqual(c.data.processes[0].name, 'App Name')
            c._collect_processes()
            self.assertEqual(c.data.processes[0].cpu_pct, 0)

    def test_addressless_vpn_and_route_switch_reset(self):
        c = collector()
        samples = iter([
            b'utun2 1380 <Link#20> 10 0 1048576 5 0 524288 0\nen0 1500 <Link#2> aa:bb 100 0 90000000 10 0 80000000 0\n',
            b'utun2 1380 <Link#20> 20 0 2097152 10 0 1048576 0\nen0 1500 <Link#2> aa:bb 100 0 90000000 10 0 80000000 0\n',
            b'en0 1500 <Link#2> aa:bb 100 0 90000000 10 0 80000000 0\n'])
        routes = iter([b'interface: utun2', b'interface: en0'])
        def output(command, **_kwargs):
            return next(routes) if command[0] == 'route' else next(samples)
        with mock.patch.object(monitor.subprocess, 'check_output', side_effect=output):
            c._collect_network(10)
            c._collect_network(11)
            self.assertEqual(c.data.net.in_mb_s, 1)
            self.assertEqual(c.data.net.out_mb_s, .5)
            c._collect_network(20)
            self.assertEqual(c.data.net.in_mb_s, 0)
            self.assertEqual(c.data.source_status['network'], 'warming')

    def test_disk_counter_reset_does_not_keep_old_rate(self):
        c = collector()
        def row(n):
            return ('"Statistics" = {"Bytes (Read)"=%d,"Bytes (Write)"=%d,"Operations (Read)"=%d,"Operations (Write)"=%d}' % (n, n, n, n)).encode()
        with mock.patch.object(monitor.subprocess, 'check_output', side_effect=[row(1), row(1048577), row(0)]) as command:
            c._collect_disk(1)
            c._collect_disk(2)
            self.assertEqual(c.data.disk.read_mb_s, 1)
            c._collect_disk(3)
            self.assertEqual(c.data.disk.read_mb_s, 0)
            self.assertEqual(c.data.source_status['disk'], 'warming')
            # ioreg depth 1 excludes child partition/APFS Statistics dictionaries.
            self.assertEqual(command.call_args.args[0][-2:], ['-d', '1'])

    def test_apfs_capacity_counts_shared_space(self):
        c = collector()
        output = b'Filesystem 1024-blocks Used Available Capacity\n/dev/disk3s5 104857600 1000 41943040 1%\n'
        with mock.patch.object(monitor.subprocess, 'check_output', return_value=output):
            c._collect_disk_capacity()
        self.assertEqual(c.data.disk.root_total_gb, 100)
        self.assertEqual(c.data.disk.root_used_gb, 60)
        self.assertEqual(c.data.disk.root_pct, 60)

    def test_incomplete_counters_are_not_reported_as_zero(self):
        c = collector()
        with mock.patch.object(monitor.subprocess, 'check_output', return_value=b'"Statistics" = {}'):
            c._collect_disk(1)
        self.assertEqual(c.data.source_status['disk'], 'unavailable')
        with mock.patch.object(monitor.subprocess, 'check_output', return_value=b'Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'):
            c._collect_memory()
        self.assertEqual(c.data.source_status['memory'], 'unavailable')

    def test_pressure_uses_kernel_state_and_failed_sample_is_unknown(self):
        c = collector()
        def output(command, **_kwargs):
            if command[0] == 'vm_stat':
                return b'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 1.\nPages wired down: 2.\nPages occupied by compressor: 3.\nAnonymous pages: 4.\nFile-backed pages: 100.\n'
            if command[1] == 'vm.swapusage':
                return b'total = 1024.00M used = 1.00M free = 1023.00M'
            return b'4\n'
        with mock.patch.object(monitor.subprocess, 'check_output', side_effect=output):
            c._collect_memory()
        self.assertEqual(c.data.mem.pressure_level, 'Critical')
        self.assertAlmostEqual(c.data.mem.cached_gb, 100 * 16384 / 1024**3)
        with mock.patch.object(monitor.subprocess, 'check_output', side_effect=OSError):
            c._collect_memory()
        self.assertEqual(c.data.mem.pressure_level, 'Unknown')
        self.assertEqual(c.data.source_status['memory'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
