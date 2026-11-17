"""Action safety contracts. All mutations below use a fake CLI, never an engine."""
import dataclasses
import json
import threading
import time
import unittest
from unittest import mock

from macvitals import docker_actions as actions
from macvitals.docker_monitor import DockerContainer, DockerSnapshot, DockerError
from macvitals import macvitals as monitor
from test_ui import Screen


CID = "a" * 64
SOCKET = "unix:///Users/test/.orbstack/run/docker.sock"


def item(**changes):
    values = dict(id=CID, name="service", project="example", state="running",
                  started_at="2026-11-01T00:00:00Z", restart_count=0, auto_remove=False)
    values.update(changes)
    return DockerContainer(**values)


def snapshot(container=None, **changes):
    values = dict(status="ok", context="orbstack", endpoint=SOCKET, engine_id="engine-one",
                  containers=[container or item()], sampled_at=time.monotonic())
    values.update(changes)
    return DockerSnapshot(**values)


def target(container=None):
    container = container or item()
    return actions.ActionTarget.capture(snapshot(container), container)


class FakeCLI:
    def __init__(self, before=None, after=None, connection=None, engine_id="engine-one",
                 mutation_error=None):
        self.before, self.after = before or item(), after or item(state="exited")
        self.connection_result = connection or ("orbstack", SOCKET)
        self.engine_id, self.mutation_error = engine_id, mutation_error
        self.calls, self.inspections = [], 0
        self.timeout = 5
        self.entered, self.release = None, None

    def connection(self):
        return self.connection_result

    def _command(self, arguments):
        self.calls.append(list(arguments))
        if "info" in arguments:
            return json.dumps(self.engine_id)
        if "inspect" in arguments:
            self.inspections += 1
            row = dataclasses.asdict(self.before if self.inspections == 1 else self.after)
            return json.dumps(row)
        if self.entered is not None:
            self.entered.set()
            self.release.wait(2)
        if self.mutation_error:
            raise self.mutation_error
        return CID

    def stop(self):
        if self.release:
            self.release.set()

    def mutations(self):
        return [call for call in self.calls if len(call) > 3 and call[3] in actions.LABELS]


class ActionSafetyTests(unittest.TestCase):
    def execute(self, cli=None, chosen=None, action="stop"):
        worker = actions.DockerActions()
        worker._transport = cli or FakeCLI()
        worker._run(chosen or target(), action)
        return worker.snapshot(), worker._transport

    def test_capture_requires_full_id_endpoint_engine_and_fresh_snapshot(self):
        for changes in ({"status": "stale"}, {"sampled_at": time.monotonic() - 13},
                        {"endpoint": "tcp://localhost"}, {"engine_id": ""}):
            with self.subTest(changes=changes), self.assertRaises(DockerError):
                actions.ActionTarget.capture(snapshot(**changes), item())
        for container in (item(id=CID[:12]), item(auto_remove=None), item(restart_count=None)):
            with self.assertRaises(DockerError):
                actions.ActionTarget.capture(snapshot(container), container)

    def test_only_supported_states_and_no_auto_remove_stop(self):
        self.assertEqual(target(item(state="paused")).allowed(), ("unpause",))
        for state in ("dead", "restarting", "removing"):
            self.assertEqual(target(item(state=state)).allowed(), ())
        self.assertEqual(target(item(auto_remove=True)).allowed(), ())
        self.assertEqual(target(item(state="created")).allowed(), ("start",))

    def test_exact_id_pinned_endpoint_and_no_shell_or_batch_command(self):
        result, cli = self.execute()
        self.assertEqual(result.status, "success")
        self.assertEqual(cli.mutations(), [["--host", SOCKET, "container", "stop", CID]])
        self.assertTrue(all(call[:2] == ["--host", SOCKET] for call in cli.calls))

    def test_connection_and_engine_changes_refuse_before_mutation(self):
        for cli in (FakeCLI(connection=("different", SOCKET)),
                    FakeCLI(connection=("orbstack", "unix:///other.sock")), FakeCLI(engine_id="engine-two")):
            result, cli = self.execute(cli)
            self.assertEqual(result.status, "rejected")
            self.assertFalse(cli.mutations())

    def test_target_changes_or_replacement_refuse_before_mutation(self):
        for changes in ({"id": "b" * 64}, {"state": "paused"}, {"name": "renamed"},
                        {"project": "other"}, {"restart_count": 1}, {"started_at": "new"},
                        {"auto_remove": True}):
            with self.subTest(changes=changes):
                result, cli = self.execute(FakeCLI(before=item(**changes)))
                self.assertEqual(result.status, "rejected")
                self.assertFalse(cli.mutations())

    def test_expired_confirmation_never_mutates(self):
        chosen = dataclasses.replace(target(), sampled_at=time.monotonic() - 31)
        result, cli = self.execute(chosen=chosen)
        self.assertEqual(result.status, "rejected")
        self.assertFalse(cli.calls)

    def test_long_preflight_expiry_is_rechecked(self):
        chosen = target()
        with mock.patch.object(actions.time, "monotonic", side_effect=[chosen.sampled_at + 1, chosen.sampled_at + 31]):
            result, cli = self.execute(chosen=chosen)
        self.assertEqual(result.status, "rejected")
        self.assertFalse(cli.mutations())

    def test_success_requires_observed_state_not_just_exit_code(self):
        result, _ = self.execute(FakeCLI(after=item()))
        self.assertEqual(result.status, "unconfirmed")
        result, _ = self.execute(FakeCLI(after=item()), action="restart")
        self.assertEqual(result.status, "unconfirmed")
        result, _ = self.execute(FakeCLI(after=item(started_at="later")), action="restart")
        self.assertEqual(result.status, "success")
        before = item(state="exited")
        result, _ = self.execute(FakeCLI(before=before, after=item()), target(before), "start")
        self.assertEqual(result.status, "success")
        before = item(state="running")
        result, cli = self.execute(FakeCLI(before=before, after=item(state="paused")), target(before), "pause")
        self.assertEqual(result.status, "success")
        self.assertEqual(cli.mutations(), [["--host", SOCKET, "container", "pause", CID]])
        before = item(state="paused")
        result, cli = self.execute(FakeCLI(before=before, after=item(state="running")), target(before), "unpause")
        self.assertEqual(result.status, "success")
        self.assertEqual(cli.mutations(), [["--host", SOCKET, "container", "unpause", CID]])

    def test_timeout_or_command_error_is_unknown_and_never_retried(self):
        for error in (DockerError("unavailable", "timeout private-endpoint"), OSError("secret")):
            result, cli = self.execute(FakeCLI(mutation_error=error))
            self.assertEqual(result.status, "unconfirmed")
            self.assertEqual(len(cli.mutations()), 1)
            self.assertNotIn("private-endpoint", result.message)
            self.assertNotIn("secret", result.message)

    def test_bad_inspect_data_fails_closed(self):
        cli = FakeCLI()
        cli._command = mock.Mock(side_effect=[json.dumps("engine-one"), "[]"])
        result, _ = self.execute(cli)
        self.assertEqual(result.status, "rejected")

    def test_one_background_task_and_shutdown_blocks_future_submissions(self):
        cli = FakeCLI()
        cli.entered, cli.release = threading.Event(), threading.Event()
        worker = actions.DockerActions()
        with mock.patch.object(actions, "DockerMonitor", return_value=cli):
            try:
                self.assertTrue(worker.submit(target(), "stop"))
                self.assertTrue(cli.entered.wait(2))
                self.assertFalse(worker.submit(target(), "restart"))
                self.assertEqual(worker.snapshot().status, "running")
            finally:
                worker.stop()
        self.assertFalse(worker._thread.is_alive())
        self.assertFalse(worker.submit(target(), "stop"))
        self.assertEqual(len(cli.mutations()), 1)

    def test_unknown_or_inappropriate_actions_never_start_a_thread(self):
        worker = actions.DockerActions()
        with mock.patch.object(actions.threading, "Thread") as thread:
            for action in ("remove", "kill", "start", "exec", "--all"):
                self.assertFalse(worker.submit(target(), action))
        thread.assert_not_called()

    def test_thread_start_failure_does_not_latch_busy_or_crash_ui(self):
        worker = actions.DockerActions()
        with mock.patch.object(actions.threading.Thread, "start", side_effect=RuntimeError("cannot start")):
            self.assertFalse(worker.submit(target(), "stop"))
        self.assertEqual(worker.snapshot().status, "rejected")
        worker.stop()


class ActionUITests(unittest.TestCase):
    def ui(self, h=30, w=100):
        data = monitor.MonitorData()
        data.docker = snapshot()
        controller = mock.Mock()
        controller.snapshot.return_value = actions.ActionResult()
        controller.submit.return_value = True
        with mock.patch.object(monitor.curses, "has_colors", return_value=False):
            ui = monitor.MonitorUI(Screen(h, w), data, controller)
        ui.page = ui.PAGE_DOCKER
        return ui

    def key(self, ui, key):
        ui.handle_key(ord(key) if isinstance(key, str) else key)
        ui.render()

    def confirm_dialog(self, ui):
        self.key(ui, "o")
        self.key(ui, "t")
        self.assertEqual(ui._docker_dialog["action"], "stop")

    def test_enter_defaults_to_cancel_and_no_mutation(self):
        ui = self.ui()
        ui._scroll[ui.PAGE_DOCKER] = 10 ** 9
        self.confirm_dialog(ui)
        self.key(ui, 10)
        ui.docker_actions.submit.assert_not_called()
        self.assertIsNone(ui._docker_dialog)
        self.assertEqual(ui._scroll[ui.PAGE_DOCKER], 0)

    def test_explicit_confirmation_keeps_original_id_across_data_updates(self):
        ui = self.ui()
        self.confirm_dialog(ui)
        ui.data.docker.containers = [item(id="b" * 64, name="different")]
        self.key(ui, 9)
        self.key(ui, 10)
        chosen, action = ui.docker_actions.submit.call_args.args
        self.assertEqual(chosen.id, CID)
        self.assertEqual(action, "stop")

    def test_modal_keys_do_not_change_page_or_container(self):
        ui = self.ui()
        self.key(ui, "o")
        for key in ("1", "n", "m", "p"):
            self.key(ui, key)
        self.assertEqual(ui.page, ui.PAGE_DOCKER)
        self.assertFalse(ui.paused)
        self.key(ui, 27)
        ui.docker_actions.submit.assert_not_called()

    def test_tiny_or_resized_confirmation_cannot_execute(self):
        ui = self.ui()
        self.confirm_dialog(ui)
        self.key(ui, 9)
        for h, w in ((1, 1), (4, 20), (5, 24), (12, 55)):
            ui.stdscr.h, ui.stdscr.w = h, w
            ui.handle_key(10)  # resize before the next redraw
            ui.render()
            ui.handle_key(10)  # resize after a redraw
            self.assertIsNotNone(ui._docker_dialog)
        ui.docker_actions.submit.assert_not_called()
        self.key(ui, 27)
        self.assertIsNone(ui._docker_dialog)

    def test_pause_stale_and_busy_disable_opening(self):
        for case in ("paused", "stale", "busy"):
            ui = self.ui()
            if case == "paused":
                ui.paused = True
            elif case == "stale":
                ui.data.docker.status = "stale"
            else:
                ui.docker_actions.snapshot.return_value = actions.ActionResult(status="running")
            self.key(ui, "o")
            self.assertIsNone(ui._docker_dialog)
            ui.docker_actions.submit.assert_not_called()

    def test_disconnection_while_confirming_cannot_submit(self):
        ui = self.ui()
        self.confirm_dialog(ui)
        ui.data.docker.status = "stale"
        self.key(ui, 9)
        self.key(ui, 10)
        ui.docker_actions.submit.assert_not_called()

    def test_auto_remove_container_is_explained_not_offered_stop(self):
        ui = self.ui()
        ui.data.docker.containers[0].auto_remove = True
        self.key(ui, "o")
        self.assertIsNone(ui._docker_dialog)
        self.assertIn("自动删除", ui._docker_notice)

    def test_running_and_result_messages_fit_all_terminal_sizes(self):
        ui = self.ui()
        for status in ("running", "success", "unconfirmed", "rejected"):
            ui.docker_actions.snapshot.return_value = actions.ActionResult(status, "操作消息", "中文目标" * 40, "stop")
            for h, w in ((1, 1), (5, 24), (12, 55), (30, 100)):
                ui.stdscr.h, ui.stdscr.w = h, w
                ui.render()


if __name__ == "__main__":
    unittest.main()
