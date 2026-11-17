"""Explicit single-container actions. No shell, retries, history or batch operations."""
import copy
import json
import re
import threading
import time
from dataclasses import dataclass

try:
    from .docker_monitor import DockerMonitor, DockerError, INSPECT_FORMAT, _json_rows, containers_from_rows, _Stopped
except ImportError:
    from docker_monitor import DockerMonitor, DockerError, INSPECT_FORMAT, _json_rows, containers_from_rows, _Stopped


LABELS = {"start": "启动", "stop": "停止", "restart": "重启", "pause": "暂停", "unpause": "恢复"}


@dataclass(frozen=True)
class ActionTarget:
    id: str
    name: str
    project: str
    state: str
    started_at: str
    restart_count: int
    auto_remove: bool
    context: str
    endpoint: str
    engine_id: str
    sampled_at: float

    @classmethod
    def capture(cls, snapshot, container):
        if (snapshot.status != "ok" or not snapshot.sampled_at or
                time.monotonic() - snapshot.sampled_at > 12 or
                not snapshot.context or not snapshot.endpoint.startswith("unix:///") or
                not snapshot.engine_id or not re.fullmatch(r"[0-9a-f]{64}", container.id) or
                container.restart_count is None or container.auto_remove is None):
            raise DockerError("unavailable", "等待完整、最新的容器数据后再操作")
        return cls(container.id, container.name, container.project, container.state,
                   container.started_at, container.restart_count, container.auto_remove,
                   snapshot.context, snapshot.endpoint, snapshot.engine_id, snapshot.sampled_at)

    def allowed(self):
        if self.state in ("created", "exited"):
            return ("start",)
        if self.state == "running" and self.auto_remove is False:
            return ("stop", "restart", "pause")
        if self.state == "paused" and self.auto_remove is False:
            return ("unpause",)
        return ()


@dataclass
class ActionResult:
    status: str = "idle"
    message: str = ""
    target_name: str = ""
    action: str = ""


class DockerActions:
    """Own transport and one worker, independent from the sampling pipeline."""
    def __init__(self, context=None):
        self.context = context
        self._lock = threading.Lock()
        self._thread = None
        self._transport = None
        self._closed = False
        self._result = ActionResult()

    def snapshot(self):
        with self._lock:
            return copy.copy(self._result)

    def submit(self, target, action):
        with self._lock:
            if self._closed or self._result.status == "running":
                return False
            if action not in LABELS or action not in target.allowed():
                self._result = ActionResult("rejected", "当前状态不支持该操作，请重新选择", target.name, action)
                return False
            self._result = ActionResult("running", "正在核实目标；请勿重复操作", target.name, action)
            # Never share the collector's mutable identity, timeout or process.
            self._transport = DockerMonitor(context=self.context)
            self._thread = threading.Thread(target=self._run, args=(target, action),
                                            name="macvitals-docker-action", daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._thread = None
                self._result = ActionResult("rejected", "无法启动操作任务；未执行，请稍后重试", target.name, action)
                return False
        return True

    def _inspect(self, cli, base, cid):
        rows = _json_rows(cli._command(base + ["container", "inspect", "--format", INSPECT_FORMAT, cid]))
        items = containers_from_rows(rows, [])
        if len(items) != 1 or items[0].id != cid:
            raise DockerError("unavailable", "目标容器已消失或无法确认，请重新选择")
        return items[0]

    def _run(self, target, action):
        cli, dispatched = self._transport, False
        status, message = "rejected", "未执行操作，请重新选择"
        try:
            if time.monotonic() - target.sampled_at > 30:
                raise DockerError("unavailable", "确认已过期；未执行，请重新打开操作面板")
            context, endpoint = cli.connection()
            if (context, endpoint) != (target.context, target.endpoint):
                raise DockerError("unavailable", "Docker 连接目标已变化；未执行，请重新选择")
            base = ["--host", endpoint]
            engine_id = json.loads(cli._command(base + ["info", "--format", "{{json .ID}}"] ))
            if not engine_id or engine_id != target.engine_id:
                raise DockerError("unavailable", "Docker 引擎已变化；未执行，请重新选择")
            current = self._inspect(cli, base, target.id)
            expected = (target.name, target.project, target.state, target.started_at,
                        target.restart_count, target.auto_remove)
            actual = (current.name, current.project, current.state, current.started_at,
                      current.restart_count, current.auto_remove)
            if actual != expected or action not in target.allowed():
                raise DockerError("unavailable", "容器状态或身份已变化；未执行，请重新选择")
            # Last possible check, immediately before issuing the sole mutation.
            # Docker CLI provides no atomic compare-and-act; another client may
            # still change this same container after our check, never another ID.
            if time.monotonic() - target.sampled_at > 30:
                raise DockerError("unavailable", "确认已过期；未执行，请重新选择")
            cli.timeout = 60.0
            dispatched = True
            cli._command(base + ["container", action, target.id])
            cli.timeout = 5.0
            current = self._inspect(cli, base, target.id)
            achieved = ((action == "start" and current.state == "running") or
                        (action == "stop" and current.state == "exited") or
                        (action == "restart" and current.state == "running" and
                         current.started_at != target.started_at) or
                        (action == "pause" and current.state == "paused") or
                        (action == "unpause" and current.state == "running"))
            if achieved:
                status, message = "success", LABELS[action] + "完成；已核实容器状态（不代表应用已就绪）"
            else:
                status, message = "unconfirmed", "命令已返回，目标状态尚未确认；请观察刷新，不会自动重试"
        except (DockerError, OSError, ValueError, TypeError, KeyError, IndexError, _Stopped) as exc:
            status = "unconfirmed" if dispatched else "rejected"
            # Do not expose raw CLI stderr, credentials or endpoint paths.
            message = ("操作结果未确认（可能仍在执行）；请检查容器状态，不会自动重试" if dispatched
                       else (exc.message if isinstance(exc, DockerError) else
                             "目标复核未通过或连接不可用；未执行，请刷新后重新选择"))
        finally:
            with self._lock:
                self._result = ActionResult(status, message, target.name, action)

    def stop(self):
        with self._lock:
            self._closed = True
            cli, thread = self._transport, self._thread
        if cli is not None:
            cli.stop()
        if thread is not None:
            thread.join(timeout=6)
