"""Host/Docker isolation and diagnostic privacy at the application boundary."""
import contextlib
import io
import json
import threading
import unittest
from unittest import mock

from macvitals import macvitals as monitor
from macvitals.docker_monitor import DockerContainer, DockerSnapshot, DockerMonitor


class DockerIntegrationTests(unittest.TestCase):
    def test_snapshot_reads_worker_memory_only_and_does_not_mutate_published_host(self):
        collector = monitor.DataCollector.__new__(monitor.DataCollector)
        collector._snapshot_lock = threading.Lock()
        collector._published = monitor.MonitorData()
        collector._published.cpu.user = 23
        docker = DockerMonitor(enabled=False)
        docker._snapshot = DockerSnapshot(status="ok", sampled_at=10,
                                         containers=[DockerContainer(id="a" * 64, name="example")])
        collector._docker = docker
        with mock.patch("macvitals.docker_monitor.time.monotonic", return_value=11), \
                mock.patch.object(docker, "_command", side_effect=AssertionError("I/O on render thread")):
            sample = collector.snapshot()
        self.assertEqual(sample.cpu.user, 23)
        self.assertEqual(sample.source_status["docker"], "ok")
        self.assertEqual(sample.source_meta["docker"]["age_s"], 1)
        self.assertNotIn("docker", collector._published.source_meta)
        sample.docker.containers.clear()
        self.assertEqual(len(docker._snapshot.containers), 1)

    def test_diagnostic_contains_counts_and_omits_container_identity(self):
        data = monitor.MonitorData()
        data.docker = DockerSnapshot(
            status="ok", engine="OrbStack", engine_cpus=8,
            containers=[DockerContainer(id="c" * 64, name="private-service", image="private-image",
                                        ports="private-port", project="private-project", state="running")])
        fake = mock.Mock()
        fake.data = data
        fake._pm_error = ""
        fake._docker.snapshot.return_value = data.docker
        fake.snapshot.return_value = data
        output = io.StringIO()
        with mock.patch.object(monitor, "DataCollector", return_value=fake), \
                mock.patch.object(monitor.time, "sleep"), contextlib.redirect_stdout(output):
            monitor.diagnose(.5, docker_context="orbstack")
        report = json.loads(output.getvalue())
        self.assertEqual(report["docker"]["running_count"], 1)
        self.assertEqual(report["docker"]["container_count"], 1)
        for secret in ("private-service", "private-image", "private-port", "private-project", "c" * 64):
            self.assertNotIn(secret, output.getvalue())
        fake._docker.start.assert_called_once()
        fake.stop_powermetrics.assert_called_once()

    def test_pause_stops_new_docker_passes_without_io(self):
        worker = DockerMonitor()
        worker.set_paused(True)
        with mock.patch.object(worker, "collect") as collect:
            worker.start()
            self.assertTrue(worker._thread.is_alive())
            worker.stop()
        collect.assert_not_called()
        self.assertFalse(worker._thread.is_alive())

    def test_docker_arguments_are_optional_and_mutually_exclusive(self):
        self.assertFalse(monitor.parse_args([]).no_docker)
        self.assertEqual(monitor.parse_args(["--docker-context", "orbstack"]).docker_context, "orbstack")
        for arguments in (["--no-docker", "--docker-context", "orbstack"],
                          ["--docker-context", ""], ["--docker-context", "bad\ncontext"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                monitor.parse_args(arguments)

    def test_container_start_time_is_local_readable_and_never_year_one(self):
        item = DockerContainer(state="exited", started_at="2026-10-31T12:00:00.123456789Z")
        self.assertIn("2026-10-31", monitor.MonitorUI._docker_started(item))
        self.assertNotIn("已运行", monitor.MonitorUI._docker_started(item))
        item.state = "running"
        self.assertIn("已运行", monitor.MonitorUI._docker_started(item))
        item.started_at = "0001-01-01T00:00:00Z"
        self.assertEqual(monitor.MonitorUI._docker_started(item), "--")


if __name__ == "__main__":
    unittest.main()
