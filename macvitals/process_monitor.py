"""Live process snapshots and conservative application-bundle aggregation.

Only macOS' bundled libproc and the Python standard library are used. A bundle
path identifies a process' executable, not every app that may use a shared XPC
service. Unattributed programs remain individual processes. No history is saved.
"""
import ctypes
import errno
import os
import plistlib
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional


class ProcessUnavailable(RuntimeError):
    """The native collector cannot produce a process enumeration."""


class RUsageInfoV0(ctypes.Structure):
    """sys/resource.h: RUSAGE_INFO_V0; do not use guessed byte offsets.

    XNU fill_task_rusage copies ri_user_time/ri_system_time from task_power_info,
    which uses Mach absolute-time units. Convert with mach_timebase_info before
    comparing to wall-clock seconds. ri_proc_start_abstime is only an identity
    marker here; it is never treated as nanoseconds or used as a duration.
    """
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


@dataclass
class ProcessInfo:
    # Preserve the original positional fields for existing users/tests.
    pid: int = 0
    name: str = ""
    cpu_pct: float = 0.0
    mem_pct: float = 0.0
    mem_mb: float = 0.0
    threads: int = 0
    cpu_available: bool = False
    mem_available: bool = True
    rss_mb: Optional[float] = None
    executable_path: str = ""
    app_key: str = ""
    app_name: str = ""
    app_path: str = ""
    read_status: str = "ok"
    app_attribution: str = ""


@dataclass
class AppInfo:
    key: str = ""
    name: str = ""
    path: str = ""
    processes: List[ProcessInfo] = field(default_factory=list)
    cpu_pct: float = 0.0
    cpu_available: bool = False
    cpu_complete: bool = False
    mem_mb: float = 0.0
    mem_available: bool = False
    mem_complete: bool = False
    mem_pct: float = 0.0


@dataclass
class ProcessSnapshot:
    status: str = "unavailable"
    processes: List[ProcessInfo] = field(default_factory=list)
    apps: List[AppInfo] = field(default_factory=list)
    enumerated_count: int = 0
    readable_count: int = 0
    denied_count: int = 0
    exited_count: int = 0
    error_count: int = 0
    path_unavailable_count: int = 0
    collection_seconds: float = 0.0
    sampled_at: float = 0.0
    memory_kind: str = "footprint"
    responsibility_available: bool = False


def _text(value, limit=160):
    return "".join(c for c in str(value or "") if c.isprintable())[:limit]


def app_bundle(path):
    """Full outer bundle path; a display name is never an aggregation key."""
    if not path.startswith("/"):
        return ""
    normalized = os.path.normpath(path)
    components = normalized.split("/")
    for index, component in enumerate(components):
        if component.lower().endswith(".app"):
            return "/".join(components[:index + 1])
    return ""


def aggregate_apps(processes, ram_gb=0.0):
    """Aggregate all explicitly identified bundle members before UI limiting."""
    groups = {}
    for process in processes:
        if not process.app_key:
            continue
        if process.app_key not in groups:
            groups[process.app_key] = AppInfo(
                key=process.app_key, name=process.app_name,
                path=process.app_path,
            )
        groups[process.app_key].processes.append(process)
    for group in groups.values():
        group.cpu_available = any(p.cpu_available for p in group.processes)
        group.cpu_complete = all(p.cpu_available for p in group.processes)
        group.cpu_pct = sum(p.cpu_pct for p in group.processes if p.cpu_available)
        group.mem_available = any(p.mem_available for p in group.processes)
        group.mem_complete = all(p.mem_available for p in group.processes)
        group.mem_mb = sum(p.mem_mb for p in group.processes if p.mem_available)
        group.mem_pct = group.mem_mb * 100 / (ram_gb * 1024) if ram_gb > 0 else 0.0
    return list(groups.values())


class _Libproc:
    def __init__(self):
        if sys.platform != "darwin":
            raise ProcessUnavailable("libproc requires macOS")
        try:
            self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.lib.proc_listallpids.restype = ctypes.c_int
            self.lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            self.lib.proc_pidpath.restype = ctypes.c_int
            self.lib.proc_name.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            self.lib.proc_name.restype = ctypes.c_int
            self.lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            self.lib.proc_pid_rusage.restype = ctypes.c_int
            system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            class TimebaseInfo(ctypes.Structure):
                _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]
            system.mach_timebase_info.argtypes = [ctypes.POINTER(TimebaseInfo)]
            system.mach_timebase_info.restype = ctypes.c_int
            info = TimebaseInfo()
            if system.mach_timebase_info(ctypes.byref(info)) != 0 or not info.numer or not info.denom:
                raise ProcessUnavailable("Mach timebase is unavailable")
            self.timebase = (int(info.numer), int(info.denom))
        except (OSError, AttributeError) as exc:
            raise ProcessUnavailable("libproc is unavailable") from exc
        # Optional runtime enhancement. This symbol is shipped by macOS but is
        # not a stable public libproc API; its absence must never break sampling.
        self._responsibility = None
        try:
            quarantine = ctypes.CDLL("/usr/lib/system/libquarantine.dylib")
            function = quarantine.responsibility_get_pid_responsible_for_pid
            function.argtypes = [ctypes.c_int]
            function.restype = ctypes.c_int
            self._responsibility = function
        except (OSError, AttributeError):
            pass
        self.responsibility_available = self._responsibility is not None

    def responsible_pid(self, pid):
        if self._responsibility is None:
            return 0
        try:
            result = self._responsibility(pid)
            return result if result > 0 else 0
        except (OSError, ValueError, TypeError):
            return 0

    def pids(self):
        ctypes.set_errno(0)
        count = self.lib.proc_listallpids(None, 0)
        if count <= 0:
            raise ProcessUnavailable("cannot enumerate processes")
        # The process set may grow between the size query and the real call.
        # Leave spare slots and retry when a returned list fills the buffer.
        capacity = count + max(128, count // 4)
        for _ in range(6):
            if capacity > 1048576:
                raise ProcessUnavailable("process enumeration exceeded its bound")
            buffer = (ctypes.c_int * capacity)()
            n = self.lib.proc_listallpids(buffer, ctypes.sizeof(buffer))
            if n <= 0:
                raise ProcessUnavailable("cannot enumerate processes")
            if n < capacity:
                return list(dict.fromkeys(pid for pid in buffer[:n] if pid > 0))
            capacity = max(capacity * 2, n + 128)
        raise ProcessUnavailable("process enumeration kept changing")

    def path(self, pid):
        buffer = ctypes.create_string_buffer(4096)
        result = self.lib.proc_pidpath(pid, buffer, len(buffer))
        return buffer.value.decode("utf-8", "replace") if result > 0 else ""

    def name(self, pid):
        buffer = ctypes.create_string_buffer(1024)
        result = self.lib.proc_name(pid, buffer, len(buffer))
        return buffer.value.decode("utf-8", "replace") if result > 0 else ""

    def usage(self, pid):
        result = RUsageInfoV0()
        ctypes.set_errno(0)
        if self.lib.proc_pid_rusage(pid, 0, ctypes.byref(result)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code) if code else "rusage unavailable")
        return result


class ProcessMonitor:
    """One previous CPU baseline and the active bundle-name cache, nothing else."""
    def __init__(self, backend=None, clock=None):
        self._backend = backend if backend is not None else _Libproc()
        self._timebase_numer, self._timebase_denom = self._backend.timebase
        self._clock = clock or time.monotonic
        self._previous = {}
        self._previous_time = None
        self._names = {}

    def _app_name(self, path):
        if path in self._names:
            return self._names[path]
        fallback = _text(os.path.basename(path)[:-4]) or "App"
        try:
            plist_path = os.path.join(path, "Contents", "Info.plist")
            # App metadata is optional; do not load arbitrarily large files.
            if os.path.getsize(plist_path) > 1024 * 1024:
                return fallback
            with open(plist_path, "rb") as handle:
                info = plistlib.load(handle)
            if not isinstance(info, dict):
                return fallback
            value = info.get("CFBundleDisplayName") or info.get("CFBundleName")
            return _text(value) if isinstance(value, str) and _text(value).strip() else fallback
        except (OSError, ValueError, TypeError, OverflowError, plistlib.InvalidFileException):
            return fallback

    def collect(self, total_cores=1, ram_gb=0.0):
        begin = self._clock()
        try:
            pids = self._backend.pids()
        except (OSError, ValueError, ProcessUnavailable) as exc:
            self._previous, self._previous_time, self._names = {}, None, {}
            raise ProcessUnavailable("cannot enumerate processes") from exc
        if not pids:
            self._previous, self._previous_time, self._names = {}, None, {}
            raise ProcessUnavailable("no enumerated processes")
        snapshot = ProcessSnapshot(
            enumerated_count=len(pids),
            responsibility_available=bool(getattr(self._backend, "responsibility_available", False)),
        )
        elapsed = begin - self._previous_time if self._previous_time is not None else 0.0
        next_previous, next_names, identities = {}, {}, {}
        ram_mb = ram_gb * 1024
        for pid in pids:
            # Bracket path/name lookup with a process identity check. PID reuse
            # during this very scan must not attach a new task's metrics to an
            # old executable path. The added native call is inexpensive.
            initial_error = None
            try:
                initial_usage = self._backend.usage(pid)
            except OSError as exc:
                initial_usage, initial_error = None, exc
            path = self._backend.path(pid)
            name = self._backend.name(pid)
            bundle = app_bundle(path)
            display = ""
            if bundle:
                if bundle not in next_names:
                    next_names[bundle] = self._app_name(bundle)
                display = next_names[bundle]
            process = ProcessInfo(
                pid=pid, name=_text(name or os.path.basename(path)) or f"进程 {pid}",
                executable_path=path, app_key=bundle, app_name=display,
                app_path=bundle, mem_available=False,
                app_attribution="bundle" if bundle else "",
            )
            if not path:
                snapshot.path_unavailable_count += 1
            try:
                if initial_error is not None:
                    raise initial_error
                usage = self._backend.usage(pid)
                if (usage.ri_proc_start_abstime != initial_usage.ri_proc_start_abstime or
                        bytes(usage.ri_uuid) != bytes(initial_usage.ri_uuid)):
                    process.name = f"进程 {pid}"
                    process.executable_path = ""
                    process.app_key = process.app_name = process.app_path = ""
                    process.app_attribution = ""
                    raise OSError(errno.EAGAIN, "process identity changed while sampling")
                snapshot.readable_count += 1
                process.mem_available = True
                process.mem_mb = usage.ri_phys_footprint / 1048576
                process.rss_mb = usage.ri_resident_size / 1048576
                process.mem_pct = process.mem_mb * 100 / ram_mb if ram_mb > 0 else 0.0
                identity = (pid, int(usage.ri_proc_start_abstime))
                identities[pid] = (int(usage.ri_proc_start_abstime), bytes(usage.ri_uuid))
                cpu_ticks = int(usage.ri_user_time) + int(usage.ri_system_time)
                cpu_ns = cpu_ticks * self._timebase_numer // self._timebase_denom
                next_previous[identity] = cpu_ns
                old_ns = self._previous.get(identity)
                if old_ns is not None and elapsed > 0 and cpu_ns >= old_ns:
                    process.cpu_pct = min(100.0, (cpu_ns - old_ns) / 1e9 * 100
                                          / elapsed / max(1, total_cores))
                    process.cpu_available = True
            except OSError as exc:
                if exc.errno in (errno.EPERM, errno.EACCES):
                    snapshot.denied_count += 1
                    process.read_status = "denied"
                elif exc.errno in (errno.ESRCH, errno.ENOENT):
                    snapshot.exited_count += 1
                    process.read_status = "exited"
                else:
                    snapshot.error_count += 1
                    process.read_status = "changed" if exc.errno == errno.EAGAIN else "error"
            snapshot.processes.append(process)
        self._attribute_xpc(snapshot, identities)
        active_bundles = {process.app_key for process in snapshot.processes if process.app_key}
        next_names = {key: value for key, value in next_names.items() if key in active_bundles}
        self._previous, self._previous_time, self._names = next_previous, begin, next_names
        snapshot.apps = aggregate_apps(snapshot.processes, ram_gb)
        if snapshot.readable_count < snapshot.enumerated_count:
            snapshot.status = "partial"
        elif any(process.cpu_available for process in snapshot.processes):
            snapshot.status = "ok"
        else:
            snapshot.status = "warming"
        snapshot.sampled_at = self._clock()
        snapshot.collection_seconds = max(0.0, snapshot.sampled_at - begin)
        return snapshot

    def _attribute_xpc(self, snapshot, identities):
        """Map only system-hosted XPC executables to an explicit direct App.

        Never follow arbitrary child trees: a terminal's node/python tasks are
        not the terminal App. Both source and target identities are checked
        again so reused PIDs cannot borrow a different App's bundle identity.
        """
        if not snapshot.responsibility_available:
            return
        by_pid = {process.pid: process for process in snapshot.processes}
        for process in snapshot.processes:
            if process.app_key or process.pid not in identities:
                continue
            path = process.executable_path
            system_path = path.startswith(("/System/Library/", "/usr/libexec/")) or re.match(
                r"^/System/Volumes/Preboot/Cryptexes/(?:Incoming/)?(?:OS|App)/System/Library/", path)
            if not system_path:
                continue
            if not any(component.lower().endswith(".xpc") for component in path.split("/")):
                continue
            try:
                pid = self._backend.responsible_pid(process.pid)
                target = by_pid.get(pid)
                if (pid == process.pid or target is None or not target.app_key or
                        target.app_attribution != "bundle" or pid not in identities):
                    continue
                source_usage, target_usage = self._backend.usage(process.pid), self._backend.usage(pid)
                source_id = (int(source_usage.ri_proc_start_abstime), bytes(source_usage.ri_uuid))
                target_id = (int(target_usage.ri_proc_start_abstime), bytes(target_usage.ri_uuid))
                if source_id != identities[process.pid] or target_id != identities[pid]:
                    continue
                process.app_key, process.app_name, process.app_path = target.app_key, target.app_name, target.app_path
                process.app_attribution = "responsibility"
            except (OSError, ValueError, TypeError, AttributeError):
                continue
