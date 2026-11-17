"""Optional, read-only Docker CLI snapshots. No SDK, history or daemon of our own.

Docker's CLI owns API negotiation, context resolution and stats calculations.
We deliberately consume its cache-adjusted memory and single-vCPU CPU convention
instead of embedding a second implementation of those algorithms.
"""
import copy
import json
import math
import os
import pwd
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class DockerContainer:
    id: str = ""
    name: str = ""
    image: str = ""
    project: str = ""
    service: str = ""
    state: str = ""
    health: Optional[str] = None
    status_text: str = ""
    ports: str = ""
    started_at: str = ""
    restart_count: Optional[int] = None
    auto_remove: Optional[bool] = None
    cpu_pct: Optional[float] = None
    memory_bytes: Optional[int] = None
    memory_limit_bytes: Optional[int] = None
    network_rx_bytes: Optional[int] = None
    network_tx_bytes: Optional[int] = None
    block_read_bytes: Optional[int] = None
    block_write_bytes: Optional[int] = None
    pids: Optional[int] = None


@dataclass
class DockerSnapshot:
    status: str = "checking"
    message: str = "正在检查 Docker"
    context: str = ""
    engine: str = ""
    endpoint: str = ""  # internal binding, omitted from diagnostic reports
    engine_id: str = ""
    engine_cpus: Optional[int] = None
    engine_memory_bytes: Optional[int] = None
    containers: List[DockerContainer] = field(default_factory=list)
    sampled_at: float = 0.0  # monotonic; never persisted
    collection_seconds: float = 0.0


class DockerError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class _Stopped(Exception):
    pass


def _text(value, limit=256):
    # Neither control sequences nor unbounded labels should reach a terminal.
    return "".join(c for c in str(value or "") if c.isprintable())[:limit]


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _integer(value):
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def parse_bytes(value):
    """Docker human-size JSON fields use both decimal and IEC units."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|kB|KB|MB|GB|TB|PB|KiB|MiB|GiB|TiB|PiB)\s*", str(value))
    if not match:
        return None
    units = {"B": 1, "kB": 1000, "KB": 1000}
    units.update({u + "B": 1000 ** i for i, u in enumerate("M G T P".split(), 2)})
    units.update({u + "iB": 1024 ** i for i, u in enumerate("K M G T P".split(), 1)})
    result = _number(match.group(1))
    size = result * units[match.group(2)] if result is not None else float("inf")
    return int(size) if math.isfinite(size) else None


def _pair(value):
    parts = str(value or "").split("/")
    return tuple(parse_bytes(part) for part in parts) if len(parts) == 2 else (None, None)


def _json_rows(text):
    rows = []
    for line in text.splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise DockerError("unavailable", "Docker 返回了无法识别的数据") from exc
            if not isinstance(row, dict):
                raise DockerError("unavailable", "Docker 返回了无法识别的数据")
            rows.append(row)
    return rows


def _ports(bindings):
    if not isinstance(bindings, dict):
        return ""
    ports = []
    for target, hosts in sorted(bindings.items()):
        if hosts:
            for host in hosts:
                if not isinstance(host, dict):
                    continue
                ip = _text(host.get("HostIp"), 128)
                if ":" in ip:
                    ip = "[" + ip + "]"
                ports.append(f"{ip or '*'}:{_text(host.get('HostPort'), 8)} → {_text(target, 32)}")
        else:
            ports.append(_text(target, 32) + "（未发布）")
    return "; ".join(ports)[:2048]


# Select fields at the CLI boundary: never collect Config.Env, mounts, commands,
# healthcheck output, registry credentials or application logs.
INSPECT_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Config.Image}},'
    '"state":{{json .State.Status}},'
    '"health":{{with index .State "Health"}}{{json .Status}}{{else}}null{{end}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"ports":{{json .NetworkSettings.Ports}},'
    '"started_at":{{json .State.StartedAt}},"restart_count":{{json .RestartCount}},'
    '"auto_remove":{{json .HostConfig.AutoRemove}}}'
)

# Safe detail view: deliberately excludes environment, mounts, labels beyond
# Compose identity, command/entrypoint, health output and registry metadata.
SAFE_DETAIL_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Config.Image}},'
    '"state":{{json .State.Status}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"started_at":{{json .State.StartedAt}},"restart_count":{{json .RestartCount}},'
    '"auto_remove":{{json .HostConfig.AutoRemove}},"ports":{{json .NetworkSettings.Ports}}}'
)


def containers_from_rows(details, stats):
    """Merge only exact container IDs; failed/stopped metrics stay unknown."""
    measurements = {row.get("Container") or row.get("ID"): row for row in stats}
    containers = []
    for row in details:
        cid = row.get("id", "")
        if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{12,64}", cid):
            raise DockerError("unavailable", "Docker 容器标识无法识别")
        item = DockerContainer(
            id=cid, name=_text(row.get("name")).lstrip("/"),
            image=_text(row.get("image")), project=_text(row.get("project")),
            service=_text(row.get("service")), state=_text(row.get("state"), 32),
            health=_text(row.get("health"), 32) or None,
            ports=_ports(row.get("ports")), started_at=_text(row.get("started_at"), 64),
            restart_count=_integer(row.get("restart_count")),
        )
        # A stopped container may retain an old unhealthy flag. Preserve raw
        # health for details; UI must give the lifecycle state precedence.
        item.status_text = item.state
        item.auto_remove = row.get("auto_remove") if isinstance(row.get("auto_remove"), bool) else None
        sample = measurements.get(cid)
        if item.state == "running" and sample:
            cpu = str(sample.get("CPUPerc", ""))
            item.cpu_pct = _number(cpu[:-1]) if cpu.endswith("%") else None
            item.memory_bytes, item.memory_limit_bytes = _pair(sample.get("MemUsage"))
            item.network_rx_bytes, item.network_tx_bytes = _pair(sample.get("NetIO"))
            item.block_read_bytes, item.block_write_bytes = _pair(sample.get("BlockIO"))
            item.pids = _integer(sample.get("PIDs"))
        containers.append(item)
    return containers


def cli_identity():
    """Resolve the original sudo user without preexec_fn in a threaded app.

    Python 3.9+ Popen can set uid/gid/groups directly. Docker does not need to
    inherit the privileges needed by powermetrics, nor root's HOME/context.
    """
    env, credentials = dict(os.environ), {}
    home = os.path.expanduser("~")
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        try:
            uid = int(os.environ["SUDO_UID"])
            user = pwd.getpwuid(uid)
            if uid == 0 or user.pw_name != os.environ.get("SUDO_USER"):
                raise ValueError("Invalid original user")
        except (ValueError, KeyError) as exc:
            raise DockerError("unavailable", "无法确认原用户，请使用普通用户运行 Docker 监测") from exc
        home = user.pw_dir
        env.update(HOME=home, USER=user.pw_name, LOGNAME=user.pw_name)
        # sudo normally strips this variable; honor a deliberately preserved
        # DOCKER_CONFIG instead of unconditionally overwriting it.
        env.setdefault("DOCKER_CONFIG", os.path.join(home, ".docker"))
        # Directory services can enumerate more memberships than setgroups(2)
        # accepts on macOS. Popen rejects that list before executing Docker.
        # Preserve the primary group first, deduplicate, then obey the kernel
        # limit; the socket is still accessed with the original user UID.
        groups = list(dict.fromkeys([user.pw_gid] + os.getgrouplist(user.pw_name, user.pw_gid)))
        limit = os.sysconf("SC_NGROUPS_MAX")
        if limit > 0:
            groups = groups[:limit]
        credentials = {"user": uid, "group": user.pw_gid, "extra_groups": groups}
    paths = [env.get("PATH", ""), os.path.join(home, ".orbstack", "bin"),
             os.path.join(home, ".docker", "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    env["PATH"] = os.pathsep.join(paths)
    env["LC_ALL"] = "C"
    return env, credentials


class DockerMonitor:
    """One cancellable worker; latest detached snapshot only, polling >=3s."""
    MAX_CONTAINERS = 512
    MAX_OUTPUT = 4 * 1024 * 1024
    MAX_READOUT_OUTPUT = 64 * 1024

    def __init__(self, enabled=True, context=None, interval=3.0, timeout=5.0):
        self.context = context
        self.interval = max(3.0, interval)
        self.timeout = timeout
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._process = None
        self._thread = None
        self._snapshot = DockerSnapshot() if enabled else DockerSnapshot(
            status="disabled", message="Docker 监测已关闭（--no-docker）")
        self.enabled = enabled
        self._env = self._credentials = None
        self._executable = None

    def start(self):
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="macvitals-docker", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        with self._process_lock:
            if self._process is not None:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=self.timeout + 1)

    def set_paused(self, paused):
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._snapshot)
        if result.status == "ok" and time.monotonic() - result.sampled_at > max(12.0, self.interval * 3):
            result.status, result.message = "stale", "Docker 数据已过期，等待重新连接"
            result.containers = []
            result.engine_cpus = result.engine_memory_bytes = None
        return result

    def _command(self, arguments, partial=False):
        with self._process_lock:
            if self._stop.is_set():
                raise _Stopped()
            self._process = subprocess.Popen(
                [self._executable] + arguments, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", env=self._env, **self._credentials)
            process = self._process
        try:
            try:
                out, err = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.communicate()
                raise DockerError("unavailable", "Docker 响应超时，请检查引擎状态") from exc
            if self._stop.is_set():
                raise _Stopped()
            if len(out) > self.MAX_OUTPUT:
                raise DockerError("unavailable", "Docker 返回数据过大，未显示不完整列表")
            if process.returncode and not (partial and out.strip()):
                if "permission denied" in err.lower():
                    raise DockerError("unavailable", "无权访问 Docker，请检查当前用户的引擎连接")
                raise DockerError("unavailable", "Docker 暂不可用，请确认引擎已启动且连接可访问")
            return out
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None

    def _bounded_command(self, arguments, limit=None):
        """Drain both pipes while retaining at most limit bytes from each."""
        limit = self.MAX_READOUT_OUTPUT if limit is None else max(1, int(limit))
        with self._process_lock:
            if self._stop.is_set():
                raise _Stopped()
            process = subprocess.Popen(
                [self._executable] + arguments, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
                env=self._env, **self._credentials)
            self._process = process
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}

        def drain(name, stream):
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                remaining = limit - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[name] = True

        readers = [threading.Thread(target=drain, args=(name, stream), daemon=True)
                   for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
        for reader in readers:
            reader.start()
        try:
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise DockerError("unavailable", "Docker 响应超时；已停止等待") from exc
            for reader in readers:
                reader.join(timeout=1)
            if self._stop.is_set():
                raise _Stopped()
            decode = lambda value: value.decode("utf-8", errors="replace")
            stdout, stderr = decode(captured["stdout"]), decode(captured["stderr"])
            if process.returncode:
                if "permission denied" in stderr.lower():
                    raise DockerError("unavailable", "无权访问 Docker，请检查当前用户的引擎连接")
                raise DockerError("unavailable", "Docker 日志读取失败；可能是容器未提供日志")
            return stdout, stderr, any(truncated.values())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
            with self._process_lock:
                if self._process is process:
                    self._process = None

    def collect(self):
        """Single read-only pass, also useful for diagnostics without the UI."""
        started = time.monotonic()
        context, endpoint = self.connection()
        base = ["--host", endpoint]
        info_format = ('{"ID":{{json .ID}},"OperatingSystem":{{json .OperatingSystem}},"Name":{{json .Name}},'
                       '"NCPU":{{json .NCPU}},"MemTotal":{{json .MemTotal}}}')
        info = json.loads(self._command(base + ["info", "--format", info_format]))
        if not isinstance(info, dict):
            raise DockerError("unavailable", "Docker 引擎信息无法识别")
        identifiers = self._command(base + ["container", "ls", "--all", "--quiet", "--no-trunc"]).split()
        if len(identifiers) > self.MAX_CONTAINERS:
            raise DockerError("unavailable", "容器超过 512 个，本版未展示不完整列表")
        if any(not re.fullmatch(r"[0-9a-f]{12,64}", cid) for cid in identifiers):
            raise DockerError("unavailable", "Docker 容器列表无法识别")
        details = _json_rows(self._command(base + ["container", "inspect", "--format", INSPECT_FORMAT] + identifiers,
                                          partial=True)) if identifiers else []
        details = [row for row in details if row.get("id") in identifiers]
        running = [row["id"] for row in details if row.get("state") == "running"]
        stats = _json_rows(self._command(base + ["stats", "--no-stream", "--no-trunc", "--format", "{{json .}}"] + running,
                                        partial=True)) if running else []
        return DockerSnapshot(
            status="ok", message="c/m 排序 · g 切换分组 · o 容器操作",
            context=_text(context), engine=_text(info.get("OperatingSystem") or info.get("Name")),
            endpoint=endpoint, engine_id=_text(info.get("ID")),
            engine_cpus=_integer(info.get("NCPU")), engine_memory_bytes=_integer(info.get("MemTotal")),
            containers=containers_from_rows(details, stats), sampled_at=time.monotonic(),
            collection_seconds=time.monotonic() - started)

    def inspect_container(self, container_id, endpoint=None):
        """Return a bounded, allow-listed detail object for one container."""
        if not re.fullmatch(r"[0-9a-f]{12,64}", str(container_id or "")):
            raise DockerError("unavailable", "容器标识无法识别")
        if endpoint is None:
            _, endpoint = self.connection()
        rows = _json_rows(self._command(["--host", endpoint, "container", "inspect",
                                         "--format", SAFE_DETAIL_FORMAT, container_id]))
        if len(rows) != 1 or rows[0].get("id") != container_id:
            raise DockerError("unavailable", "容器详情暂不可用")
        return rows[0]

    def logs(self, container_id, tail=80, endpoint=None):
        """Read recent logs only; output is transient and capped."""
        if not re.fullmatch(r"[0-9a-f]{12,64}", str(container_id or "")):
            raise DockerError("unavailable", "容器标识无法识别")
        tail = max(1, min(int(tail), 200))
        if endpoint is None:
            _, endpoint = self.connection()
        stdout, stderr, truncated = self._bounded_command(
            ["--host", endpoint, "container", "logs", "--tail", str(tail), container_id])
        output = stdout
        if stderr:
            output += ("\n" if output and not output.endswith("\n") else "") + stderr
        if truncated:
            output += "\n[输出已截断：每个通道最多 64 KiB]"
        return output

    def connection(self):
        """Resolve a local endpoint and prepare this transport's original-user identity."""
        self._env, self._credentials = cli_identity()
        self._executable = shutil.which("docker", path=self._env["PATH"])
        if not self._executable:
            raise DockerError("not_installed", "未找到 Docker CLI；已有硬件监测不受影响")

        # Use the actual CLI's resolution (including vendor builds), then bind
        # every data command to the exact inspected local endpoint for this pass.
        context = self.context or self._command(["context", "show"]).strip()
        if not context or len(context) > 256 or context.startswith("-"):
            raise DockerError("unavailable", "Docker context 无法识别")
        contexts = json.loads(self._command(["context", "inspect", context]))
        if (not isinstance(contexts, list) or not contexts or not isinstance(contexts[0], dict)
                or not isinstance(contexts[0].get("Endpoints"), dict)
                or not isinstance(contexts[0]["Endpoints"].get("docker"), dict)):
            raise DockerError("unavailable", "Docker context 信息无法识别")
        endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
        if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
            raise DockerError("remote_unsupported", "当前 Docker 连接指向远程引擎；首版仅展示本机容器")
        # Global TLS/context environment flags must not silently override the
        # endpoint selected above. Local Unix sockets don't use TLS.
        self._env = {k: v for k, v in self._env.items() if k not in (
            "DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")}
        return context, endpoint

    def _loop(self):
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.1)
                continue
            started = time.monotonic()
            try:
                result = self.collect()
            except _Stopped:
                return
            except (DockerError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                status = exc.status if isinstance(exc, DockerError) else "unavailable"
                message = exc.message if isinstance(exc, DockerError) else "Docker 数据暂不可用，稍后自动重试"
                with self._lock:
                    previous = self._snapshot
                    result = DockerSnapshot(status=status, message=message, context=previous.context)
                    if status == "unavailable" and previous.sampled_at:
                        result.status = "stale"
                        result.sampled_at = previous.sampled_at
            with self._lock:
                self._snapshot = result
            delay = self.interval if result.status == "ok" else max(10.0, self.interval)
            self._stop.wait(max(0.1, delay - (time.monotonic() - started)))


class DockerReadouts:
    """One-shot transient detail requests, kept separate from polling/actions."""
    def __init__(self, context=None):
        self.context = context
        self._lock = threading.Lock()
        self._thread = None
        self._transport = None
        self._cancelled = False
        self._result = {"status": "idle", "kind": "", "text": "", "target": ""}

    def snapshot(self):
        with self._lock:
            return dict(self._result)

    def submit(self, kind, container, snapshot, tail=80):
        if (kind not in ("logs", "inspect") or snapshot.status != "ok" or not snapshot.sampled_at
                or time.monotonic() - snapshot.sampled_at > 12 or not snapshot.endpoint or not snapshot.engine_id):
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancelled = False
            cli = DockerMonitor(enabled=False, context=snapshot.context or self.context, timeout=5.0)
            self._transport = cli
            self._result = {"status": "running", "kind": kind, "text": "正在读取 Docker 数据…",
                            "target": container.name, "id": container.id, "context": snapshot.context}
            self._thread = threading.Thread(target=self._run, args=(cli, kind, container, snapshot, tail),
                                             name="macvitals-docker-readout", daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._thread = None
                self._transport = None
                self._result = {"status": "error", "kind": kind, "text": "无法启动只读请求",
                                "target": container.name}
                return False
        return True

    def _run(self, cli, kind, container, snapshot, tail):
        try:
            context, endpoint = cli.connection()
            if context != snapshot.context or endpoint != snapshot.endpoint:
                raise DockerError("unavailable", "Docker 连接目标已变化，请重新选择容器")
            engine = json.loads(cli._command(["--host", endpoint, "info", "--format", "{{json .ID}}"] ))
            if engine != snapshot.engine_id:
                raise DockerError("unavailable", "Docker 引擎已变化，请重新选择容器")
            value = cli.logs(container.id, tail, endpoint) if kind == "logs" else cli.inspect_container(container.id, endpoint)
            if kind == "inspect":
                labels = {"id": "容器 ID", "name": "名称", "image": "镜像", "state": "状态",
                          "project": "Compose 项目", "service": "服务", "started_at": "启动时间",
                          "restart_count": "重启次数", "auto_remove": "停止时自动删除", "ports": "端口映射"}
                value = "\n".join(f"{labels[key]}: {display}" for key, display in value.items()
                                   if key in labels and display is not None)
            text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", str(value))
            text = "".join(ch for ch in text if ch in "\n\t" or (ch.isprintable() and ord(ch) != 127))
            status = "success"
        except (DockerError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            text, status = (exc.message if isinstance(exc, DockerError) else "Docker 数据暂不可用"), "error"
        with self._lock:
            if self._cancelled:
                text, status = "", "idle"
            self._result = {"status": status, "kind": kind, "text": text,
                            "target": container.name, "id": container.id, "context": snapshot.context}
            self._transport = None

    def clear(self):
        with self._lock:
            self._cancelled = True
            cli, thread = self._transport, self._thread
            self._result = {"status": "idle", "kind": "", "text": "", "target": ""}
        if cli is not None:
            cli.stop()
        if thread is not None:
            thread.join(timeout=6)

    def stop(self):
        self.clear()
