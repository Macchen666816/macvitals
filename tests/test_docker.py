"""Read-only Docker integration contracts; no Docker installation is required."""
import json
import io
import io
import io
import os
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from macvitals import docker_monitor as docker


A, B, C = "a" * 64, "b" * 64, "c" * 64
ENDPOINT = "unix:///Users/test/.orbstack/run/docker.sock"


def detail(cid=A, **values):
    result = dict(id=cid, name="/service", image="example:latest", state="running",
                  health=None, project="example", service="api", ports={},
                  started_at="2026-10-31T00:00:00Z", restart_count=0)
    result.update(values)
    return result


def measurement(cid=A, **values):
    result = dict(Container=cid, ID=cid[:12], Name="service", CPUPerc="250.00%",
                  MemUsage="64MiB / 2GiB", NetIO="1.5MB / 2MiB",
                  BlockIO="0B / 4.1kB", PIDs="0")
    result.update(values)
    return result


def valid_snapshot():
    return docker.DockerSnapshot(status="ok", context="orbstack", engine="OrbStack",
                                 engine_cpus=8, engine_memory_bytes=10 * 1024**3,
                                 containers=[docker.DockerContainer(id=A, name="service", cpu_pct=42.)],
                                 sampled_at=time.monotonic())


class DockerParsingTests(unittest.TestCase):
    def test_safe_detail_format_excludes_sensitive_fields(self):
        self.assertNotIn("Env", docker.SAFE_DETAIL_FORMAT)
        self.assertNotIn("Mounts", docker.SAFE_DETAIL_FORMAT)
        self.assertNotIn("Cmd", docker.SAFE_DETAIL_FORMAT)


class DockerReadoutTests(unittest.TestCase):
    def test_bounded_command_captures_both_streams_and_marks_truncation(self):
        process = mock.Mock(returncode=0)
        process.stdout = io.BytesIO(b"stdout-12345")
        process.stderr = io.BytesIO(b"stderr-67890")
        process.wait.return_value = 0
        process.poll.return_value = 0
        cli = docker.DockerMonitor(timeout=1)
        cli._executable, cli._env, cli._credentials = "/docker", {}, {}
        with mock.patch.object(docker.subprocess, "Popen", return_value=process):
            out, err, truncated = cli._bounded_command(["container", "logs"], limit=8)
        self.assertEqual((out, err), ("stdout-1", "stderr-6"))
        self.assertTrue(truncated)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_bounded_command_does_not_expose_raw_cli_error(self):
        process = mock.Mock(returncode=1)
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"secret-token-value")
        process.wait.return_value = 1
        process.poll.return_value = 1
        cli = docker.DockerMonitor(timeout=1)
        cli._executable, cli._env, cli._credentials = "/docker", {}, {}
        with mock.patch.object(docker.subprocess, "Popen", return_value=process):
            with self.assertRaises(docker.DockerError) as caught:
                cli._bounded_command(["container", "logs"], limit=128)
        self.assertNotIn("secret-token", caught.exception.message)


class DockerReadoutTests(unittest.TestCase):
    def test_bounded_command_captures_both_streams_and_marks_truncation(self):
        process = mock.Mock(returncode=0)
        process.stdout = io.BytesIO(b"stdout-12345")
        process.stderr = io.BytesIO(b"stderr-67890")
        process.wait.return_value = 0
        process.poll.return_value = 0
        cli = docker.DockerMonitor(timeout=1)
        cli._executable, cli._env, cli._credentials = "/docker", {}, {}
        with mock.patch.object(docker.subprocess, "Popen", return_value=process):
            out, err, truncated = cli._bounded_command(["container", "logs"], limit=8)
        self.assertEqual((out, err), ("stdout-1", "stderr-6"))
        self.assertTrue(truncated)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_bounded_command_does_not_expose_raw_cli_error(self):
        process = mock.Mock(returncode=1)
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"secret-token-value")
        process.wait.return_value = 1
        process.poll.return_value = 1
        cli = docker.DockerMonitor(timeout=1)
        cli._executable, cli._env, cli._credentials = "/docker", {}, {}
        with mock.patch.object(docker.subprocess, "Popen", return_value=process):
            with self.assertRaises(docker.DockerError) as caught:
                cli._bounded_command(["container", "logs"], limit=128)
        self.assertNotIn("secret-token", caught.exception.message)


class DockerReadoutTests(unittest.TestCase):
    def test_bounded_command_captures_both_streams_and_marks_truncation(self):
        process = mock.Mock(returncode=0)
        process.stdout = io.BytesIO(b"stdout-12345")
        process.stderr = io.BytesIO(b"stderr-67890")
        process.wait.return_value = 0
        process.poll.return_value = 0
        cli = docker.DockerMonitor(timeout=1)
        cli._executable, cli._env, cli._credentials = "/docker", {}, {}
        with mock.patch.object(docker.subprocess, "Popen", return_value=process):
            out, err, truncated = cli._bounded_command(["container", "logs"], limit=8)
        self.assertEqual((out, err), ("stdout-1", "stderr-6"))
        self.assertTrue(truncated)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_bounded_command_does_not_expose_raw_cli_error(self):
        process = mock.Mock(returncode=1)
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"secret-token-value")
        process.wait.return_value = 1
        process.poll.return_value = 1
        cli = docker.DockerMonitor(timeout=1)
        cli._executable, cli._env, cli._credentials = "/docker", {}, {}
        with mock.patch.object(docker.subprocess, "Popen", return_value=process):
            with self.assertRaises(docker.DockerError) as caught:
                cli._bounded_command(["container", "logs"], limit=128)
        self.assertNotIn("secret-token", caught.exception.message)

    def test_binary_and_decimal_sizes_remain_distinct(self):
        for text, expected in (("1.5MB", 1500000), ("1.5 MiB", 1572864),
                               ("2GiB", 2 * 1024**3), ("4.1kB", 4100), ("0B", 0)):
            with self.subTest(text=text):
                self.assertEqual(docker.parse_bytes(text), expected)

    def test_unusable_and_overflowing_sizes_are_unknown(self):
        for value in (None, "", "--", "-1 B", "NaN MiB", "1e3 B", "12 QQ", "100",
                      "1" + "0" * 300 + " PiB"):
            with self.subTest(value=str(value)[:50]):
                self.assertIsNone(docker.parse_bytes(value))

    def test_cli_metrics_preserve_single_cpu_scale_and_cache_adjusted_memory(self):
        row = measurement(MemoryStats={"usage": 999999999, "cache": 1})
        item = docker.containers_from_rows([detail()], [row])[0]
        self.assertEqual(item.cpu_pct, 250.)
        # The already adjusted CLI MemUsage is authoritative; no second cache subtraction.
        self.assertEqual(item.memory_bytes, 64 * 1024**2)
        self.assertEqual(item.memory_limit_bytes, 2 * 1024**3)
        self.assertEqual(item.network_rx_bytes, 1500000)
        self.assertEqual(item.network_tx_bytes, 2 * 1024**2)
        self.assertEqual(item.block_read_bytes, 0)
        self.assertEqual(item.block_write_bytes, 4100)
        self.assertEqual(item.pids, 0)

    def test_missing_metrics_and_valid_zero_are_different(self):
        unknown = docker.containers_from_rows([detail()], [measurement(CPUPerc="--", MemUsage="--", PIDs="--")])[0]
        self.assertIsNone(unknown.cpu_pct)
        self.assertIsNone(unknown.memory_bytes)
        self.assertIsNone(unknown.pids)
        zero = docker.containers_from_rows([detail()], [measurement(CPUPerc="0.00%", MemUsage="0B / 2GiB")])[0]
        self.assertEqual(zero.cpu_pct, 0.)
        self.assertEqual(zero.memory_bytes, 0)
        for text in ("NaN%", "inf%", "-1%", "250"):
            self.assertIsNone(docker.containers_from_rows([detail()], [measurement(CPUPerc=text)])[0].cpu_pct)

    def test_metrics_join_by_full_id_not_name_or_list_position(self):
        items = docker.containers_from_rows([detail(A, name="/same"), detail(B, name="/same")],
                                            [measurement(B, CPUPerc="10%"), measurement(C, CPUPerc="99%")])
        self.assertIsNone(items[0].cpu_pct)
        self.assertEqual(items[1].cpu_pct, 10.)
        short = docker.containers_from_rows([detail()], [measurement(A[:12])])[0]
        self.assertIsNone(short.cpu_pct)

    def test_stopped_or_paused_containers_do_not_inherit_running_metrics(self):
        for state in ("exited", "dead", "created", "restarting", "paused"):
            with self.subTest(state=state):
                item = docker.containers_from_rows([detail(state=state, health="unhealthy")], [measurement()])[0]
                self.assertEqual(item.state, state)
                self.assertEqual(item.health, "unhealthy")
                self.assertIsNone(item.cpu_pct)
                self.assertIsNone(item.memory_bytes)

    def test_ports_and_names_are_safe_terminal_text(self):
        row = detail(name="/service\x1b\nname", ports={"80/tcp": [
            {"HostIp": "127.0.0.1", "HostPort": "8080"},
            {"HostIp": "::", "HostPort": "8080"}], "443/tcp": None})
        item = docker.containers_from_rows([row], [])[0]
        self.assertNotIn("\x1b", item.name)
        self.assertNotIn("\n", item.name)
        self.assertIn("127.0.0.1:8080", item.ports)
        self.assertIn("[::]:8080", item.ports)
        self.assertIn("443/tcp（未发布）", item.ports)
        self.assertIsNone(item.health)

    def test_invalid_json_rows_fail_without_partial_publish(self):
        for text in ('{"id":"ok"}\nnot-json', '[1,2]', 'null'):
            with self.subTest(text=text):
                with self.assertRaises(docker.DockerError):
                    docker._json_rows(text)


class DockerCollectionTests(unittest.TestCase):
    def pipeline(self, *, context=None, endpoint=ENDPOINT, identifiers=None, details=None, stats=None):
        monitor = docker.DockerMonitor(context=context)
        identifiers = [A] if identifiers is None else identifiers
        details = [detail()] if details is None else details
        stats = [measurement()] if stats is None else stats
        calls = []

        def command(arguments, partial=False):
            calls.append((list(arguments), dict(monitor._env), partial))
            if arguments == ["context", "show"]:
                return "orbstack\n"
            if arguments[:2] == ["context", "inspect"]:
                return json.dumps([{"Endpoints": {"docker": {"Host": endpoint}}}])
            if "info" in arguments:
                return json.dumps({"OperatingSystem": "OrbStack", "Name": "orbstack", "NCPU": 8, "MemTotal": 10 * 1024**3})
            if "ls" in arguments:
                return "\n".join(identifiers)
            if "inspect" in arguments:
                return "\n".join(map(json.dumps, details))
            if "stats" in arguments:
                return "\n".join(map(json.dumps, stats))
            self.fail("Unexpected Docker command: " + repr(arguments))

        monitor._command = mock.Mock(side_effect=command)
        return monitor, calls

    def collect(self, monitor):
        env = {"PATH": "/test/bin", "HOME": "/Users/test", "DOCKER_CONTEXT": "orbstack",
               "DOCKER_HOST": "unix:///wrong.sock", "DOCKER_TLS_VERIFY": "1",
               "DOCKER_TLS": "1", "DOCKER_CERT_PATH": "/unused", "DOCKER_CONFIG": "/Users/test/.docker"}
        with mock.patch.object(docker, "cli_identity", return_value=(env, {})), \
             mock.patch.object(docker.shutil, "which", return_value="/test/bin/docker"):
            return monitor.collect()

    def test_no_cli_is_optional_and_never_starts_data_commands(self):
        monitor = docker.DockerMonitor()
        with mock.patch.object(docker, "cli_identity", return_value=({"PATH": ""}, {})), \
             mock.patch.object(docker.shutil, "which", return_value=None), \
             mock.patch.object(monitor, "_command") as command:
            with self.assertRaises(docker.DockerError) as caught:
                monitor.collect()
        self.assertEqual(caught.exception.status, "not_installed")
        command.assert_not_called()

    def test_remote_context_is_refused_before_any_daemon_data_query(self):
        for endpoint in ("ssh://example", "tcp://127.0.0.1:2375", "npipe:////./pipe/docker_engine"):
            monitor, calls = self.pipeline(endpoint=endpoint)
            with self.subTest(endpoint=endpoint), self.assertRaises(docker.DockerError) as caught:
                self.collect(monitor)
            self.assertEqual(caught.exception.status, "remote_unsupported")
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(args[0] == "context" for args, _, _ in calls))

    def test_explicit_context_and_selected_endpoint_bind_all_data_commands(self):
        monitor, calls = self.pipeline(context="chosen")
        result = self.collect(monitor)
        self.assertEqual(result.context, "chosen")
        self.assertEqual(result.engine_cpus, 8)
        self.assertEqual(result.containers[0].cpu_pct, 250.)
        self.assertEqual(calls[0][0], ["context", "inspect", "chosen"])
        for arguments, env, _ in calls[1:]:
            self.assertEqual(arguments[:2], ["--host", ENDPOINT])
            for key in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
                self.assertNotIn(key, env)
            self.assertEqual(env["DOCKER_CONFIG"], "/Users/test/.docker")

    def test_empty_or_deleted_containers_do_not_trigger_unscoped_stats(self):
        for ids, details in (([], []), ([A], [])):
            monitor, calls = self.pipeline(identifiers=ids, details=details)
            result = self.collect(monitor)
            self.assertEqual(result.containers, [])
            self.assertFalse(any("stats" in arguments for arguments, _, _ in calls))

    def test_unlisted_inspect_responses_are_filtered_before_stats(self):
        monitor, calls = self.pipeline(identifiers=[A, B], details=[detail(A), detail(C)],
                                       stats=[measurement(C), measurement(A)])
        result = self.collect(monitor)
        self.assertEqual([item.id for item in result.containers], [A])
        stats_calls = [arguments for arguments, _, _ in calls if "stats" in arguments]
        self.assertEqual(len(stats_calls), 1)
        self.assertIn(A, stats_calls[0])
        self.assertNotIn(B, stats_calls[0])
        self.assertNotIn(C, stats_calls[0])

    def test_invalid_or_excessive_container_lists_are_rejected(self):
        for identifiers in (["--all"], [A] * 513):
            monitor, calls = self.pipeline(identifiers=identifiers)
            with self.assertRaises(docker.DockerError):
                self.collect(monitor)
            self.assertFalse(any("stats" in arguments or "container" in arguments and "inspect" in arguments
                                 for arguments, _, _ in calls))

    def test_invalid_context_schema_fails_before_querying_engine(self):
        for response in ([], {}, [None], [{"Endpoints": []}], [{"Endpoints": {"docker": None}}]):
            monitor = docker.DockerMonitor(context="orbstack")
            monitor._command = mock.Mock(return_value=json.dumps(response))
            with self.subTest(response=response), self.assertRaises(docker.DockerError):
                self.collect(monitor)
            monitor._command.assert_called_once_with(["context", "inspect", "orbstack"])


class DockerLifecycleTests(unittest.TestCase):
    def test_snapshots_are_detached_and_expired_values_disappear(self):
        monitor = docker.DockerMonitor()
        monitor._snapshot = valid_snapshot()
        detached = monitor.snapshot()
        detached.containers[0].cpu_pct = 999.
        detached.containers.clear()
        self.assertEqual(monitor.snapshot().containers[0].cpu_pct, 42.)
        with mock.patch.object(docker.time, "monotonic", return_value=monitor._snapshot.sampled_at + 13):
            expired = monitor.snapshot()
        self.assertEqual(expired.status, "stale")
        self.assertEqual(expired.containers, [])
        self.assertIsNone(expired.engine_memory_bytes)
        self.assertEqual(monitor._snapshot.status, "ok")

    def test_failed_sampling_clears_old_values_then_recovers(self):
        monitor = docker.DockerMonitor()
        states = []
        monitor.collect = mock.Mock(side_effect=[valid_snapshot(), docker.DockerError("unavailable", "offline"), valid_snapshot()])

        def wait(_):
            states.append(monitor.snapshot())
            if len(states) == 3:
                monitor._stop.set()

        with mock.patch.object(monitor._stop, "wait", side_effect=wait):
            monitor._loop()
        self.assertEqual([item.status for item in states], ["ok", "stale", "ok"])
        self.assertEqual(states[1].containers, [])
        self.assertIsNone(states[1].engine_cpus)
        self.assertEqual(states[2].containers[0].cpu_pct, 42.)

    def test_disabled_monitor_starts_no_worker(self):
        monitor = docker.DockerMonitor(enabled=False)
        with mock.patch.object(docker.threading, "Thread") as thread:
            monitor.start()
        thread.assert_not_called()
        self.assertEqual(monitor.snapshot().status, "disabled")

    def child_monitor(self, timeout=0.1):
        monitor = docker.DockerMonitor(timeout=timeout)
        monitor._executable = sys.executable
        monitor._env, monitor._credentials = dict(os.environ), {}
        return monitor

    def test_timeout_kills_and_reaps_real_child(self):
        monitor = self.child_monitor()
        real_popen, children = subprocess.Popen, []

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        with mock.patch.object(docker.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(docker.DockerError) as caught:
                monitor._command(["-c", "import time; time.sleep(5)"])
        self.assertEqual(caught.exception.status, "unavailable")
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(monitor._process)

    def test_daemon_and_permission_errors_do_not_leak_raw_stderr(self):
        for error in ("connection refused at private-endpoint", "permission denied: private-endpoint"):
            monitor = self.child_monitor()
            process = mock.Mock(returncode=1)
            process.communicate.return_value = ("", error)
            with mock.patch.object(docker.subprocess, "Popen", return_value=process):
                with self.assertRaises(docker.DockerError) as caught:
                    monitor._command(["info"])
            self.assertNotIn("private-endpoint", caught.exception.message)
            self.assertIsNone(monitor._process)

    def test_deleted_container_partial_output_can_be_consumed(self):
        monitor = self.child_monitor()
        process = mock.Mock(returncode=1)
        process.communicate.return_value = (json.dumps(detail()), "No such container: deleted")
        with mock.patch.object(docker.subprocess, "Popen", return_value=process):
            text = monitor._command(["container", "inspect", A, B], partial=True)
        self.assertEqual(docker._json_rows(text)[0]["id"], A)

    def test_stop_cancels_and_reaps_active_child(self):
        monitor = self.child_monitor(timeout=3)
        real_popen, children, errors = subprocess.Popen, [], []
        launched = threading.Event()

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            launched.set()
            return process

        def command():
            try:
                monitor._command(["-c", "import time; time.sleep(5)"])
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(docker.subprocess, "Popen", side_effect=launch):
            monitor._thread = threading.Thread(target=command)
            monitor._thread.start()
            try:
                self.assertTrue(launched.wait(2))
                monitor.stop()
                self.assertFalse(monitor._thread.is_alive())
                self.assertIsNotNone(children[0].poll())
                self.assertIsNone(monitor._process)
                self.assertEqual(len(errors), 1)
            finally:
                if children and children[0].poll() is None:
                    children[0].kill()
                    children[0].wait()
                monitor._thread.join(timeout=4)


class DockerIdentityTests(unittest.TestCase):
    def test_sudo_group_list_respects_kernel_limit_and_keeps_primary_first(self):
        account = SimpleNamespace(pw_name="alice", pw_dir="/Users/alice", pw_gid=20)
        with mock.patch.dict(os.environ, {"HOME": "/var/root", "SUDO_UID": "501", "SUDO_USER": "alice"}, clear=True), \
             mock.patch.object(docker.os, "geteuid", return_value=0), \
             mock.patch.object(docker.pwd, "getpwuid", return_value=account), \
             mock.patch.object(docker.os, "getgrouplist", return_value=list(range(30)) + [20]), \
             mock.patch.object(docker.os, "sysconf", return_value=16):
            _, credentials = docker.cli_identity()
        self.assertEqual(credentials["user"], 501)
        self.assertEqual(credentials["group"], 20)
        self.assertEqual(credentials["extra_groups"], [20] + list(range(15)))

    def test_sudo_uses_original_home_uid_gid_and_groups_without_preexec(self):
        account = SimpleNamespace(pw_name="alice", pw_dir="/Users/alice", pw_gid=20)
        with mock.patch.dict(os.environ, {"HOME": "/var/root", "PATH": "/usr/bin", "SUDO_UID": "501", "SUDO_USER": "alice"}, clear=True), \
             mock.patch.object(docker.os, "geteuid", return_value=0), \
             mock.patch.object(docker.pwd, "getpwuid", return_value=account), \
             mock.patch.object(docker.os, "getgrouplist", return_value=[20, 80]):
            env, credentials = docker.cli_identity()
        self.assertEqual(env["HOME"], "/Users/alice")
        self.assertEqual(env["DOCKER_CONFIG"], "/Users/alice/.docker")
        self.assertIn("/Users/alice/.orbstack/bin", env["PATH"])
        self.assertEqual(credentials, {"user": 501, "group": 20, "extra_groups": [20, 80]})
        process = mock.Mock(returncode=0)
        process.communicate.return_value = ("ok", "")
        monitor = docker.DockerMonitor()
        monitor._executable, monitor._env, monitor._credentials = "/docker", env, credentials
        with mock.patch.object(docker.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(monitor._command(["version"]), "ok")
        self.assertEqual(popen.call_args.kwargs["user"], 501)
        self.assertEqual(popen.call_args.kwargs["extra_groups"], [20, 80])
        self.assertNotIn("preexec_fn", popen.call_args.kwargs)

    def test_invalid_original_identity_fails_instead_of_using_root_config(self):
        account = SimpleNamespace(pw_name="other", pw_dir="/Users/other", pw_gid=20)
        with mock.patch.dict(os.environ, {"HOME": "/var/root", "SUDO_UID": "501", "SUDO_USER": "alice"}, clear=True), \
             mock.patch.object(docker.os, "geteuid", return_value=0), \
             mock.patch.object(docker.pwd, "getpwuid", return_value=account):
            with self.assertRaises(docker.DockerError):
                docker.cli_identity()


if __name__ == "__main__":
    unittest.main()
