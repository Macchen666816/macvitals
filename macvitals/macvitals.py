#!/usr/bin/env python3
"""
macvitals — Real-time macOS system monitor for Apple Silicon
==============================================================
Python standard library only. SSH-friendly Apple Silicon monitoring.

Dual mode:
  Basic (no sudo):  CPU, memory, disk, network, processes, supported SMC sensors
  With sudo:       + available powermetrics power/frequency measurements

Usage:
  macvitals                       # basic mode
  sudo macvitals                  # full mode (power/freq/temp)
  macvitals --interval 0.5        # custom refresh interval (seconds)

Keyboard shortcuts:
  q       Quit
  p       Pause/resume
  1-7     Switch page (Overview/CPU/GPU/Memory/Apps/Sensors/Docker)
  c/m     Sort processes or containers by CPU/memory
  v       Toggle applications/processes or containers/projects on their pages
  ← →     Previous/next page
"""

__version__ = "1.5.0"
__author__ = "macvitals contributors"

import argparse
import curses
import subprocess
import time
import threading
import re
import os
import sys
import signal
import ctypes
import struct
import copy
import json
import math
import platform
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple

try:
    from .docker_monitor import DockerMonitor, DockerSnapshot, DockerError, DockerReadouts
    from .docker_actions import DockerActions, ActionTarget, LABELS
except ImportError:
    try:  # Direct script: python3 macvitals/macvitals.py
        from docker_monitor import DockerMonitor, DockerSnapshot, DockerError, DockerReadouts
        from docker_actions import DockerActions, ActionTarget, LABELS
    except ImportError:  # File-based developer/test imports from the repo root.
        from macvitals.docker_monitor import DockerMonitor, DockerSnapshot, DockerError, DockerReadouts
        from macvitals.docker_actions import DockerActions, ActionTarget, LABELS

try:
    from .process_monitor import ProcessMonitor, ProcessSnapshot, ProcessInfo, ProcessUnavailable
except ImportError:
    try:
        from process_monitor import ProcessMonitor, ProcessSnapshot, ProcessInfo, ProcessUnavailable
    except ImportError:
        from macvitals.process_monitor import ProcessMonitor, ProcessSnapshot, ProcessInfo, ProcessUnavailable


# ──────────────────────────── Apple SMC ────────────────────────────

class _SMCVersion(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_uint8), ("minor", ctypes.c_uint8),
        ("build", ctypes.c_uint8), ("reserved", ctypes.c_uint8),
        ("release", ctypes.c_uint16),
    ]


class _SMCPowerLimit(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint16), ("length", ctypes.c_uint16),
        ("cpu", ctypes.c_uint32), ("gpu", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
    ]


class _SMCKeyInfo(ctypes.Structure):
    _fields_ = [
        ("data_size", ctypes.c_uint32),
        ("data_type", ctypes.c_uint32),
        ("attributes", ctypes.c_uint8),
    ]


class _SMCKeyData(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint32), ("version", _SMCVersion),
        ("power_limit", _SMCPowerLimit), ("key_info", _SMCKeyInfo),
        ("result", ctypes.c_uint8), ("status", ctypes.c_uint8),
        ("data8", ctypes.c_uint8), ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_uint8 * 32),
    ]


class AppleSMC:
    """最小只读 AppleSMC 客户端，只公开已知且可解释的温度传感器。"""

    # Key identities: https://github.com/exelban/stats/blob/master/Modules/Sensors/values.swift
    # Community mappings are not Apple's hardware specification. Sensor numbers
    # identify measurement sites, not schedulable logical CPUs; never truncate
    # a key list based on core counts or claim this identifies disabled cores.
    CHIP_SENSOR_KEYS = {
        "M1": {
            "performance": ["Tp01", "Tp05", "Tp0D", "Tp0H", "Tp0L", "Tp0P", "Tp0X", "Tp0b"],
            "efficiency": ["Tp09", "Tp0T"],
            "gpu": ["Tg05", "Tg0D", "Tg0L", "Tg0T"],
            "memory": ["Tm02", "Tm06", "Tm08", "Tm09"],
        },
        "M2": {
            "performance": ["Tp01", "Tp05", "Tp09", "Tp0D", "Tp0X", "Tp0b", "Tp0f", "Tp0j"],
            "efficiency": ["Tp1h", "Tp1t", "Tp1p", "Tp1l"],
            "gpu": ["Tg0f", "Tg0j"],
            "memory": [],
        },
        "M3": {
            "performance": [
                "Tf04", "Tf09", "Tf0A", "Tf0B", "Tf0D", "Tf0E",
                "Tf44", "Tf49", "Tf4A", "Tf4B", "Tf4D", "Tf4E",
            ],
            "efficiency": ["Te05", "Te0L", "Te0P", "Te0S"],
            "gpu": ["Tf14", "Tf18", "Tf19", "Tf1A", "Tf24", "Tf28", "Tf29", "Tf2A"],
            "memory": [],
        },
        "M4": {
            "performance": ["Tp01", "Tp05", "Tp09", "Tp0D", "Tp0V", "Tp0Y", "Tp0b", "Tp0e"],
            "efficiency": ["Te05", "Te0S", "Te09", "Te0H"],
            "gpu": ["Tg0G", "Tg0H", "Tg0K", "Tg0L", "Tg0d", "Tg0e", "Tg0j", "Tg0k"],
            "gpu_pro": ["Tg1U", "Tg1k", "Tg0K", "Tg0L", "Tg0d", "Tg0e", "Tg0j", "Tg0k"],
            "memory": ["Tm0p", "Tm1p", "Tm2p"],
        },
        "M5": {
            "super": ["Tp00", "Tp04", "Tp08", "Tp0C", "Tp0G", "Tp0K"],
            "performance": ["Tp0O", "Tp0R", "Tp0U", "Tp0X", "Tp0a", "Tp0d",
                            "Tp0g", "Tp0j", "Tp0m", "Tp0p", "Tp0u", "Tp0y"],
            "efficiency": [],
            "gpu": ["Tg0U", "Tg0X", "Tg0d", "Tg0g", "Tg0j", "Tg1Y", "Tg1c", "Tg1g"],
            "memory": [],
        },
    }

    COMMON_SENSOR_KEYS = [
        ("TH0x", "SSD 闪存"),
        ("TW0P", "Wi-Fi"),
        ("Ts0P", "左掌托"),
        ("Ts1P", "右掌托"),
        ("TaLP", "左侧气流"),
        ("TaRF", "右侧气流"),
    ]

    def __init__(self, chip: str = "", performance_cores: int = 0, efficiency_cores: int = 0):
        self._io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        self._libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        self._bind()
        self._conn = ctypes.c_uint32(0)
        self._key_info = {}
        try:
            self._open()
            keys = self._read_all_keys()
        except Exception:
            self.close()
            raise
        self.temp_sensors = self._temperature_sensors(
            chip, performance_cores, efficiency_cores, set(keys)
        )
        self.fan_keys = sorted(key for key in keys if key.startswith("F") and key.endswith("Ac"))

    @classmethod
    def _temperature_sensors(
        cls, chip: str, performance_cores: int, efficiency_cores: int,
        available: set
    ) -> List[Tuple[str, str]]:
        match = re.search(r"\b(M[1-5])\b", chip.upper())
        profile = cls.CHIP_SENSOR_KEYS.get(match.group(1) if match else "")
        if not profile:
            return [(key, name) for key, name in cls.COMMON_SENSOR_KEYS if key in available]

        def named(keys: List[str], label: str, limit: int = 0) -> List[Tuple[str, str]]:
            return [(key, f"{label} {index}") for index, key in enumerate(keys, 1)
                    if key in available]

        sensors = []
        # Base M5 uses a different topology; don't apply Pro/Max CPU labels.
        known_cpu = match.group(1) != "M5" or any(v in chip.upper() for v in ("PRO", "MAX"))
        if known_cpu:
            sensors += named(profile.get("super", []), "CPU 超级核测点")
            sensors += named(profile["performance"], "CPU 性能核测点")
            sensors += named(profile["efficiency"], "CPU 能效核测点")
        gpu_key = "gpu_pro" if match.group(1) == "M4" and any(
            variant in chip.upper() for variant in ("PRO", "MAX", "ULTRA")
        ) else "gpu"
        sensors += named(profile[gpu_key], "GPU 测点")
        sensors += named(profile["memory"], "内存测点")
        sensors += [(key, name) for key, name in cls.COMMON_SENSOR_KEYS if key in available]
        return sensors

    def _bind(self):
        io = self._io
        io.IOServiceMatching.argtypes = [ctypes.c_char_p]
        io.IOServiceMatching.restype = ctypes.c_void_p
        io.IOServiceGetMatchingServices.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        ]
        io.IOServiceGetMatchingServices.restype = ctypes.c_int
        io.IOIteratorNext.argtypes = [ctypes.c_uint32]
        io.IOIteratorNext.restype = ctypes.c_uint32
        io.IORegistryEntryGetName.argtypes = [ctypes.c_uint32, ctypes.c_char_p]
        io.IORegistryEntryGetName.restype = ctypes.c_int
        io.IOServiceOpen.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32)
        ]
        io.IOServiceOpen.restype = ctypes.c_int
        io.IOServiceClose.argtypes = [ctypes.c_uint32]
        io.IOObjectRelease.argtypes = [ctypes.c_uint32]
        io.IOConnectCallStructMethod.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)
        ]
        io.IOConnectCallStructMethod.restype = ctypes.c_int
        self._libc.mach_task_self.restype = ctypes.c_uint32

    def _open(self):
        iterator = ctypes.c_uint32(0)
        matching = self._io.IOServiceMatching(b"AppleSMC")
        if not matching or self._io.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator)):
            raise RuntimeError("AppleSMC unavailable")
        try:
            while True:
                device = self._io.IOIteratorNext(iterator.value)
                if not device:
                    break
                try:
                    name = ctypes.create_string_buffer(128)
                    self._io.IORegistryEntryGetName(device, name)
                    if name.value == b"AppleSMCKeysEndpoint":
                        result = self._io.IOServiceOpen(
                            device, self._libc.mach_task_self(), 0, ctypes.byref(self._conn)
                        )
                        if result == 0:
                            return
                finally:
                    self._io.IOObjectRelease(device)
        finally:
            self._io.IOObjectRelease(iterator.value)
        raise RuntimeError("AppleSMC connection failed")

    @staticmethod
    def _key_id(key: str) -> int:
        if len(key) != 4:
            raise ValueError("SMC keys must contain four bytes")
        return int.from_bytes(key.encode("latin-1"), "big")

    def _call(self, value: _SMCKeyData) -> _SMCKeyData:
        output = _SMCKeyData()
        output_size = ctypes.c_size_t(ctypes.sizeof(output))
        result = self._io.IOConnectCallStructMethod(
            self._conn.value, 2, ctypes.byref(value), ctypes.sizeof(value),
            ctypes.byref(output), ctypes.byref(output_size)
        )
        if result or output.result:
            raise RuntimeError(f"SMC read failed: {result}/{output.result}")
        return output

    def _info(self, key_id: int) -> _SMCKeyInfo:
        if key_id not in self._key_info:
            output = self._call(_SMCKeyData(key=key_id, data8=9))
            self._key_info[key_id] = output.key_info
        return self._key_info[key_id]

    def _read_raw(self, key: str) -> Tuple[bytes, str]:
        key_id = self._key_id(key)
        info = self._info(key_id)
        output = self._call(_SMCKeyData(key=key_id, key_info=info, data8=5))
        raw = bytes(output.bytes[:info.data_size])
        data_type = info.data_type.to_bytes(4, "big").decode("latin-1")
        return raw, data_type

    def _read_number(self, key: str) -> Optional[float]:
        raw, data_type = self._read_raw(key)
        if data_type == "flt " and len(raw) == 4:
            return float(struct.unpack("<f", raw)[0])
        if data_type == "fpe2" and len(raw) >= 2:
            return float(int.from_bytes(raw[:2], "big") / 4.0)
        if data_type == "sp78" and len(raw) >= 2:
            return float(int.from_bytes(raw[:2], "big", signed=True) / 256.0)
        if data_type == "ui8 " and raw:
            return float(raw[0])
        if data_type == "ui16" and len(raw) >= 2:
            return float(int.from_bytes(raw[:2], "big"))
        if data_type == "ui32" and len(raw) >= 4:
            return float(int.from_bytes(raw[:4], "big"))
        return None

    def _read_all_keys(self) -> List[str]:
        raw, _ = self._read_raw("#KEY")
        count = int.from_bytes(raw[:4], "big")
        keys = []
        for index in range(count):
            try:
                output = self._call(_SMCKeyData(data8=8, data32=index))
                keys.append(output.key.to_bytes(4, "big").decode("latin-1"))
            except Exception:
                continue
        return keys

    def snapshot(self) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
        sensors = []
        fans = []
        for key, name in self.temp_sensors:
            try:
                value = self._read_number(key)
                if value is not None and 0 < value <= 150:
                    sensors.append((name, value))
            except Exception:
                continue
        two_fans = len(self.fan_keys) == 2
        for index, key in enumerate(self.fan_keys):
            try:
                value = self._read_number(key)
                if value is not None and 0 <= value <= 100000:
                    name = ("左风扇", "右风扇")[index] if two_fans else f"风扇 {index + 1}"
                    fans.append((name, value))
            except Exception:
                continue
        return sensors, fans

    def close(self):
        if self._conn.value:
            self._io.IOServiceClose(self._conn.value)
            self._conn.value = 0


# ──────────────────────────── 数据模型 ────────────────────────────

@dataclass
class CpuStats:
    user: float = 0.0
    sys: float = 0.0
    idle: float = 0.0
    load_1m: float = 0.0
    load_5m: float = 0.0
    load_15m: float = 0.0
    per_core: List[float] = field(default_factory=list)
    # sudo only — from powermetrics
    p_freq_mhz: float = 0.0
    e_freq_mhz: float = 0.0
    power_w: float = 0.0
    temp_c: float = 0.0
    cluster_freqs: Dict[str, float] = field(default_factory=dict)


@dataclass
class GpuStats:
    freq_mhz: float = 0.0
    power_w: float = 0.0
    temp_c: float = 0.0
    usage_pct: float = 0.0
    active: bool = False  # GPU 是否活跃


@dataclass
class AneStats:
    power_w: float = 0.0
    freq_mhz: float = 0.0


@dataclass
class MemStats:
    total_gb: float = 0.0
    used_gb: float = 0.0
    cached_gb: float = 0.0
    free_gb: float = 0.0
    swap_used_gb: float = 0.0
    swap_total_gb: float = 0.0
    compressed_gb: float = 0.0
    wired_gb: float = 0.0
    app_memory_gb: float = 0.0
    available_gb: float = 0.0
    pressure_level: str = "Normal"  # Normal / Warning / Critical


@dataclass
class DiskStats:
    read_mb_s: float = 0.0
    write_mb_s: float = 0.0
    read_ops: float = 0.0
    write_ops: float = 0.0
    # 磁盘容量
    root_total_gb: float = 0.0
    root_used_gb: float = 0.0
    root_avail_gb: float = 0.0
    root_pct: float = 0.0


@dataclass
class NetStats:
    interface: str = ""
    in_mb_s: float = 0.0
    out_mb_s: float = 0.0
    in_packets: int = 0
    out_packets: int = 0


@dataclass
class ThermalStats:
    cpu_warning: bool = False
    cpu_nominal: bool = True
    gpu_warning: bool = False
    scheduler_limit: bool = False
    thermal_pressure: str = "Nominal"  # Nominal / Moderate / Heavy / Trapping
    sensors: List[Tuple[str, float]] = field(default_factory=list)
    fans: List[Tuple[str, float]] = field(default_factory=list)


@dataclass
class PowerStats:
    """总功率汇总"""
    cpu_w: float = 0.0
    gpu_w: float = 0.0
    ane_w: float = 0.0
    total_w: float = 0.0  # CPU + GPU + ANE


@dataclass
class SystemInfo:
    """静态系统信息"""
    chip: str = ""
    cpu_cores_p: int = 0
    cpu_cores_e: int = 0
    total_cores: int = 0
    ram_gb: float = 0.0
    model: str = ""
    model_id: str = ""
    core_groups: List[Tuple[str, int]] = field(default_factory=list)
    cpu_cores_s: int = 0


@dataclass
class MonitorData:
    cpu: CpuStats = field(default_factory=CpuStats)
    gpu: GpuStats = field(default_factory=GpuStats)
    ane: AneStats = field(default_factory=AneStats)
    mem: MemStats = field(default_factory=MemStats)
    disk: DiskStats = field(default_factory=DiskStats)
    net: NetStats = field(default_factory=NetStats)
    docker: DockerSnapshot = field(default_factory=DockerSnapshot)
    thermal: ThermalStats = field(default_factory=ThermalStats)
    power: PowerStats = field(default_factory=PowerStats)
    processes: List[ProcessInfo] = field(default_factory=list)
    process_sample: ProcessSnapshot = field(default_factory=ProcessSnapshot)
    sys_info: SystemInfo = field(default_factory=SystemInfo)
    has_sudo: bool = False
    uptime_s: float = 0.0
    refresh_interval: float = 1.0
    source_status: Dict[str, str] = field(default_factory=dict)
    # Current-snapshot provenance only; this is not a history/log store.
    source_meta: Dict[str, Dict[str, object]] = field(default_factory=dict)
    pm_status: str = "未启用（需要 sudo）"
    pm_metrics: set = field(default_factory=set)
    sampled_at: float = 0.0
    collection_seconds: float = 0.0
    developer_mode: bool = False

    # 历史数据用于图表
    cpu_history: deque = field(default_factory=lambda: deque(maxlen=120))
    cpu_power_history: deque = field(default_factory=lambda: deque(maxlen=120))
    gpu_power_history: deque = field(default_factory=lambda: deque(maxlen=120))
    gpu_freq_history: deque = field(default_factory=lambda: deque(maxlen=120))
    net_in_history: deque = field(default_factory=lambda: deque(maxlen=120))
    net_out_history: deque = field(default_factory=lambda: deque(maxlen=120))
    cpu_temp_history: deque = field(default_factory=lambda: deque(maxlen=120))
    gpu_temp_history: deque = field(default_factory=lambda: deque(maxlen=120))
    disk_read_history: deque = field(default_factory=lambda: deque(maxlen=120))
    disk_write_history: deque = field(default_factory=lambda: deque(maxlen=120))


# ──────────────────────────── 数据采集器 ────────────────────────────

class DataCollector:
    def __init__(self, interval: float = 1.0, developer_mode: bool = False,
                 docker_enabled: bool = True, docker_context: Optional[str] = None):
        self.data = MonitorData()
        self.data.refresh_interval = interval
        self.data.developer_mode = developer_mode
        self.data.has_sudo = os.geteuid() == 0
        self._collect_sys_info()
        self._pm_proc = None
        self._pm_thread = None
        self._pm_data = {}
        self._pm_last_update = 0.0
        self._pm_lock = threading.Lock()
        self._aux_sensors = []
        self._aux_fans = []
        try:
            self._smc = AppleSMC(
                self.data.sys_info.chip,
                self.data.sys_info.cpu_cores_p,
                self.data.sys_info.cpu_cores_e,
            )
        except Exception:
            self._smc = None
        self._prev_net = None
        self._prev_net_time = None
        self._prev_disk = None
        self._prev_disk_time = None
        try:
            self._process_reader = ProcessMonitor()
        except ProcessUnavailable:
            self._process_reader = None
        self._primary_interface = None
        self._interface_checked_at = 0.0
        self._running = True
        self._paused = False
        self._collect_lock = threading.RLock()
        self._snapshot_lock = threading.Lock()
        self._docker = DockerMonitor(enabled=docker_enabled, context=docker_context)
        self.data.docker = self._docker.snapshot()
        self._published = copy.deepcopy(self.data)
        self._stop_event = threading.Event()
        self._bg_thread = None

    def _collect_sys_info(self):
        def sysctl(key, default=""):
            try:
                return subprocess.check_output(
                    ["sysctl", "-n", key], stderr=subprocess.DEVNULL, timeout=2
                ).decode().strip()
            except (OSError, subprocess.SubprocessError):
                return default

        def number(key, default=0):
            try:
                return int(sysctl(key))
            except ValueError:
                return default

        info = self.data.sys_info
        info.chip = sysctl("machdep.cpu.brand_string", "Unknown")
        info.total_cores = number("hw.ncpu", os.cpu_count() or 0)
        info.ram_gb = number("hw.memsize") / (1024 ** 3)
        info.model_id = sysctl("hw.model", "Mac")
        info.model = info.model_id
        labels = {"Super": "超级核心", "Performance": "性能核心", "Efficiency": "能效核心"}
        for index in range(number("hw.nperflevels")):
            name = sysctl(f"hw.perflevel{index}.name", f"核心组 {index + 1}")
            count = number(f"hw.perflevel{index}.logicalcpu")
            if count <= 0:
                continue
            info.core_groups.append((labels.get(name, name), count))
            if name == "Super":
                info.cpu_cores_s += count
            elif name == "Performance":
                info.cpu_cores_p += count
            elif name == "Efficiency":
                info.cpu_cores_e += count

    def snapshot(self):
        """UI only reads complete, detached samples; collection never holds this lock for I/O."""
        with self._snapshot_lock:
            data = copy.deepcopy(self._published)
        # Docker has its own slower I/O worker. Neither a stalled daemon nor
        # CLI startup can hold up the host collector or the render thread.
        if hasattr(self, "_docker"):
            data.docker = self._docker.snapshot()
            data.source_status["docker"] = data.docker.status
            age = max(0.0, time.monotonic() - data.docker.sampled_at) if data.docker.sampled_at else None
            data.source_meta["docker"] = {
                "source": "Docker CLI (local engine)", "status": data.docker.status,
                "meaning": "容器 CPU 100%=1 虚拟核；内存为 CLI 缓存扣减口径",
                "comparison": "引擎/容器口径，不等同于当前 Mac 整机资源",
                "age_s": round(age, 3) if age is not None else None,
            }
        return data

    def start(self):
        """启动后台采集"""
        self.start_powermetrics()
        self._docker.start()
        self._bg_thread = threading.Thread(target=self._bg_collect_loop, daemon=True)
        self._bg_thread.start()

    def _bg_collect_loop(self):
        """后台持续采集数据（不阻塞 UI，精确计时）"""
        while self._running:
            if self._paused:
                time.sleep(0.1)
                continue
            t0 = time.monotonic()
            try:
                self.collect_all()
            except Exception as exc:
                self._collection_error = str(exc)
                for source in self.data.source_status:
                    self.data.source_status[source] = 'unavailable'
                self.data.cpu.temp_c = self.data.gpu.temp_c = 0.0
                self.data.thermal.sensors, self.data.thermal.fans = [], []
                self.data.pm_metrics.clear()
                with self._snapshot_lock:
                    self._published = copy.deepcopy(self.data)
            # 精确计时：扣除采集耗时，保持稳定节奏
            elapsed = time.monotonic() - t0
            target = max(0.3, self.data.refresh_interval)
            sleep_time = max(0.05, target - elapsed)
            self._stop_event.wait(sleep_time)

    def set_paused(self, paused: bool):
        """暂停或恢复后台采集。"""
        self._paused = paused
        if hasattr(self, "_docker"):
            self._docker.set_paused(paused)

    def start_powermetrics(self):
        """Probe supported samplers; privilege alone does not imply available data."""
        if not self.data.has_sudo:
            return
        self._pm_error = ""
        try:
            help_result = subprocess.run(
                ["powermetrics", "--help"], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, timeout=3,
                env={**os.environ, "LC_ALL": "C"},
            )
            supported = set(re.findall(r"^\s+(\w+)\s{2,}", help_result.stdout, re.MULTILINE))
            samplers = [s for s in ("cpu_power", "gpu_power", "ane_power", "thermal") if s in supported]
            if not samplers:
                raise RuntimeError("未发现可用采样器")
            self._pm_proc = subprocess.Popen(
                ["powermetrics", "--samplers", ",".join(samplers),
                 "-i", str(max(500, int(self.data.refresh_interval * 1000))),
                 "-n", "-1", "--format", "text", "--buffer-size", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
                text=True, errors="replace", env={**os.environ, "LC_ALL": "C"},
            )
            self.data.pm_status = "等待首个完整采样"
            self._pm_thread = threading.Thread(target=self._read_powermetrics, daemon=True)
            self._pm_thread.start()
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            self._pm_error = str(exc)
            self.data.pm_status = "采集不可用"

    def _read_powermetrics(self):
        """A new sample header commits the previous frame; never parse arbitrary chunks."""
        lines = []
        size = 0
        in_sample = False
        try:
            for line in self._pm_proc.stdout:
                if not self._running:
                    break
                if line.lstrip().startswith("*** Sample"):
                    if in_sample and lines:
                        self._parse_pm_sample("".join(lines))
                    lines = [line]
                    size = len(line)
                    in_sample = True
                elif in_sample:
                    lines.append(line)
                    size += len(line)
                    if size > 1024 * 1024:
                        # Malformed/unbounded frame: discard, don't publish partial metrics.
                        lines, size, in_sample = [], 0, False
                elif re.search(r'error|failed|unrecognized|must be|requires|permission', line, re.IGNORECASE):
                    self._pm_error = line.strip()[:300]
            if self._running:
                self._pm_error = self._pm_error or "powermetrics 已退出"
        except (OSError, ValueError) as exc:
            self._pm_error = str(exc)
        # EOF may be a truncated sample; only the next header proves completion.

    def _parse_pm_sample(self, text: str):
        """Parse complete frames, preserving OS cluster identities and units."""
        new_data = {}

        # Preserve cluster identities reported by the OS. Do not infer P/E/S
        # membership from logical CPU indexes; M5 Pro uses Super + Performance.
        clusters = {}
        for match in re.finditer(
            r"^\s*([\w-]+)[ -]Cluster\s+HW\s+active\s+frequency:\s*([\d.]+)\s*MHz",
            text, re.MULTILINE | re.IGNORECASE
        ):
            frequency = float(match.group(2))
            if math.isfinite(frequency) and 0 <= frequency <= 10000:
                clusters[match.group(1)] = frequency
        if clusters:
            new_data["cluster_freqs"] = clusters
        # Legacy attributes retained for consumers; no fabricated per-core fallback.
        if "P" in clusters:
            new_data["p_freq"] = clusters["P"]
        if "E" in clusters:
            new_data["e_freq"] = clusters["E"]

        # ── CPU Power ──
        m = re.search(r'CPU\s+Power:\s*([\d.]+)\s*mW', text, re.IGNORECASE)
        if m:
            new_data['cpu_power'] = float(m.group(1)) / 1000.0
        else:
            m = re.search(r'CPU\s+Power:\s*([\d.]+)\s*W', text, re.IGNORECASE)
            if m:
                new_data['cpu_power'] = float(m.group(1))

        # ── GPU 频率 ──
        # 精确匹配 "GPU HW active frequency: XXX MHz"
        m = re.search(r'GPU\s+HW\s+active\s+frequency:\s*(\d+)\s*MHz', text)
        if m:
            freq = float(m.group(1))
            if 0 <= freq <= 5000:
                new_data['gpu_freq'] = freq
        # fallback: 旧版 macOS 格式
        if 'gpu_freq' not in new_data:
            m = re.search(r'GPUFreq:\s*(\d+)', text, re.IGNORECASE)
            if m:
                freq = float(m.group(1))
                if 0 <= freq <= 5000:
                    new_data['gpu_freq'] = freq

        # ── GPU Power ──
        m = re.search(r'GPU\s+Power:\s*([\d.]+)\s*mW', text, re.IGNORECASE)
        if m:
            new_data['gpu_power'] = float(m.group(1)) / 1000.0
        else:
            m = re.search(r'GPU\s+Power:\s*([\d.]+)\s*W', text, re.IGNORECASE)
            if m:
                new_data['gpu_power'] = float(m.group(1))

        # Active residency is utilization; an idle GPU may still report frequency.
        m = re.search(r'GPU\s+HW\s+active\s+residency:\s*([\d.]+)%', text, re.IGNORECASE)
        if m and 0 <= float(m.group(1)) <= 100:
            new_data['gpu_usage'] = float(m.group(1))
            new_data['gpu_active'] = new_data['gpu_usage'] > 0

        # ── ANE Power ──
        m = re.search(r'ANE\s+Power:\s*([\d.]+)\s*mW', text, re.IGNORECASE)
        if m:
            new_data['ane_power'] = float(m.group(1)) / 1000.0
        else:
            m = re.search(r'ANE\s+Power:\s*([\d.]+)\s*W', text, re.IGNORECASE)
            if m:
                new_data['ane_power'] = float(m.group(1))

        # ── 温度 ──
        sensors = {}
        for m in re.finditer(
            r'^\s*([^:\n]+?)\s+temperature:\s*(-?[\d.]+)\s*(?:°\s*)?C\b',
            text, re.IGNORECASE | re.MULTILINE
        ):
            name = re.sub(r'\s+', ' ', m.group(1)).strip()
            temp = float(m.group(2))
            if -20 <= temp <= 130:
                sensors[name] = temp
        new_data['sensors'] = list(sensors.items())

        cpu_temps = [temp for name, temp in sensors.items() if 'cpu' in name.lower()]
        gpu_temps = [temp for name, temp in sensors.items() if 'gpu' in name.lower()]
        if cpu_temps:
            new_data['cpu_temp'] = sum(cpu_temps) / len(cpu_temps)
        if gpu_temps:
            new_data['gpu_temp'] = sum(gpu_temps) / len(gpu_temps)

        # SMC 在不同机型上可能输出 Fan、Fan 0 speed 或 Left fan 等名称。
        fans = {}
        for m in re.finditer(
            r'^\s*([^:\n]*fan[^:\n]*?):\s*([\d.]+)\s*rpm\b',
            text, re.IGNORECASE | re.MULTILINE
        ):
            name = re.sub(r'\s+', ' ', m.group(1)).strip()
            fans[name] = float(m.group(2))
        new_data['fans'] = list(fans.items())

        # ── 热压力 ──
        m = re.search(r'(?:Thermal\s+Pressure|Current\s+pressure\s+level):\s*(\w+)', text, re.IGNORECASE)
        if m:
            new_data['thermal_pressure'] = m.group(1).capitalize()

        with self._pm_lock:
            # 每个 sample 是完整快照，不能 update；否则 GPU 休眠后会保留旧频率和活跃状态。
            self._pm_data = new_data
            self._pm_last_update = time.monotonic() if any(
                key not in ('sensors', 'fans') for key in new_data
            ) else 0.0
            if self._pm_last_update:
                self._pm_error = ""

    def stop_powermetrics(self):
        self._running = False
        self._stop_event.set()
        if hasattr(self, "_docker"):
            self._docker.stop()
        if self._pm_proc and self._pm_proc.poll() is None:
            self._pm_proc.terminate()
            try:
                self._pm_proc.wait(timeout=2)
            except Exception:
                self._pm_proc.kill()
                self._pm_proc.wait(timeout=2)
        if self._pm_thread:
            self._pm_thread.join(timeout=3)
        if self._bg_thread:
            self._bg_thread.join(timeout=3)
        # Do not close the IOKit connection while an in-flight read uses it.
        if self._collect_lock.acquire(timeout=3):
            try:
                if self._smc:
                    self._smc.close()
            finally:
                self._collect_lock.release()
        if self._pm_proc and self._pm_proc.stdout:
            self._pm_proc.stdout.close()

    def collect_all(self):
        """收集所有可用的监控数据"""
        with self._collect_lock:
            now = time.monotonic()

            self._collect_cpu()
            self._collect_memory()
            self._collect_disk()
            self._collect_network()
            self._collect_thermal()
            self._collect_aux_sensors()
            if not self.data.has_sudo:
                self.data.thermal.sensors = list(self._aux_sensors)
                self.data.thermal.fans = list(self._aux_fans)
            self._collect_processes()
            self._collect_uptime()
            self._collect_disk_capacity()

            self._merge_powermetrics()

            # Provenance describes only the latest live snapshot. It is
            # intentionally not persisted, buffered, or appended over time.
            self._update_source_meta(now)

            # 计算总功率
            self.data.power.cpu_w = self.data.cpu.power_w
            self.data.power.gpu_w = self.data.gpu.power_w
            self.data.power.ane_w = self.data.ane.power_w
            self.data.power.total_w = (
                self.data.cpu.power_w + self.data.gpu.power_w + self.data.ane.power_w
            )

            # 记录历史
            total_cpu = 100.0 - self.data.cpu.idle
            if self.data.source_status.get('cpu') == 'ok':
                self.data.cpu_history.append(total_cpu)
            if 'cpu_power' in self.data.pm_metrics:
                self.data.cpu_power_history.append(self.data.cpu.power_w)
            if 'gpu_power' in self.data.pm_metrics:
                self.data.gpu_power_history.append(self.data.gpu.power_w)
            if 'gpu_freq' in self.data.pm_metrics:
                self.data.gpu_freq_history.append(self.data.gpu.freq_mhz)
            if self.data.cpu.temp_c > 0:
                self.data.cpu_temp_history.append(self.data.cpu.temp_c)
            if self.data.gpu.temp_c > 0:
                self.data.gpu_temp_history.append(self.data.gpu.temp_c)
            if self.data.source_status.get('network') == 'ok':
                self.data.net_in_history.append(self.data.net.in_mb_s)
                self.data.net_out_history.append(self.data.net.out_mb_s)
            if self.data.source_status.get('disk') == 'ok':
                self.data.disk_read_history.append(self.data.disk.read_mb_s)
                self.data.disk_write_history.append(self.data.disk.write_mb_s)
            self.data.sampled_at = time.time()
            self.data.collection_seconds = time.monotonic() - now
            with self._snapshot_lock:
                self._published = copy.deepcopy(self.data)

    def _update_source_meta(self, started_at: float):
        thermal_definition = (
            ("powermetrics thermal", "系统热压力", "不是温度，也不是活动监视器的同名字段")
            if "thermal_pressure" in getattr(self.data, "pm_metrics", set())
            else ("pmset -g therm", "系统散热警告/限制状态", "不是当前温度，也不是活动监视器的同名字段")
        )
        definitions = {
            "cpu": ("Mach host_statistics", "整机 CPU ticks 差分", "与活动监视器同为系统 CPU 观测，但窗口和聚合口径可能不同"),
            "memory": ("vm_stat + hw.memsize", "内存分类估算", "不等同于活动监视器的 Memory Used 或 App Memory"),
            "memory_pressure": ("sysctl kern.memorystatus_vm_pressure_level", "内核内存压力等级", "不是由剩余内存百分比推导"),
            "swap": ("sysctl vm.swapusage", "交换空间计数", "与活动监视器 Swap Used 的更新时间可能不同"),
            "disk": ("IOKit ioreg", "块设备读写计数差分", "不是单个应用或目录的 I/O"),
            "disk_capacity": ("df/APFS filesystem", "启动磁盘共享空间", "按 APFS 共享空间统计，不是单一系统卷"),
            "network": ("netstat + route", "接口累计字节差分", "是所选接口流量，不是应用净流量"),
            "thermal": thermal_definition,
            "sensors": ("AppleSMC + powermetrics", "已识别温度测点与风扇", "测点平均不等于最高结温；不同工具测点可能不同"),
            "processes": (("libproc rusage + executable bundle" if self.data.process_sample.memory_kind == "footprint"
                           else "ps cumulative CPU time + RSS"),
                          "进程窗口 CPU；应用按可确认归属聚合",
                          "footprint 不是整机已用 RAM；RSS 降级时不提供应用聚合"),
            "uptime": ("sysctl kern.boottime", "系统启动时间", "系统级时间，不依赖 Stats 或活动监视器"),
        }
        finished_at = time.monotonic()
        current = {}
        for key, status in self.data.source_status.items():
            source, meaning, comparison = definitions.get(
                key, ("internal collector", "实时采集字段", "不要与其他工具同名字段直接等同")
            )
            # A collection pass is the freshness boundary for native readers;
            # powermetrics gets its actual frame age below.
            age = max(0.0, finished_at - started_at)
            current[key] = {
                "source": source,
                "meaning": meaning,
                "comparison": comparison,
                "status": status,
                "age_s": round(age, 3),
            }
        pm_last_update = getattr(self, "_pm_last_update", 0.0)
        if self.data.has_sudo or pm_last_update:
            pm_age = (finished_at - pm_last_update) if pm_last_update else None
            current["powermetrics"] = {
                "source": "powermetrics complete sample",
                "meaning": "功率、频率、GPU 驻留和热压力可用字段",
                "comparison": "功率为系统估算；GPU 驻留、频率和温度不等于活动监视器同名指标",
                "status": "ok" if self.data.pm_metrics else "warming",
                "age_s": round(max(0.0, pm_age), 3) if pm_age is not None else None,
            }
        self.data.source_meta = current

    def _collect_cpu(self):
        """对系统累计 CPU tick 做相邻采样差分。"""
        try:
            lib = getattr(self, '_cpu_lib', None)
            if lib is None:
                lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
                lib.mach_host_self.restype = ctypes.c_uint
                lib.host_statistics.argtypes = [ctypes.c_uint, ctypes.c_int,
                                                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
                lib.mach_task_self.restype = ctypes.c_uint
                lib.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
                self._cpu_lib = lib
            ticks = (ctypes.c_uint32 * 4)()
            count = ctypes.c_uint(4)
            host = lib.mach_host_self()
            try:
                if lib.host_statistics(host, 3, ticks, ctypes.byref(count)) != 0:
                    raise OSError('host_statistics failed')
            finally:
                lib.mach_port_deallocate(lib.mach_task_self(), host)
            current = tuple(ticks)
            previous = getattr(self, '_prev_cpu_ticks', None)
            self._prev_cpu_ticks = current
            self.data.cpu.load_1m, self.data.cpu.load_5m, self.data.cpu.load_15m = os.getloadavg()
            self.data.source_status['cpu'] = 'warming'
            if previous is not None:
                delta = [(a - b) & 0xffffffff for a, b in zip(current, previous)]
                total = sum(delta)
                if total:
                    self.data.cpu.user = (delta[0] + delta[3]) * 100 / total
                    self.data.cpu.sys = delta[1] * 100 / total
                    self.data.cpu.idle = delta[2] * 100 / total
                    self.data.source_status['cpu'] = 'ok'
        except Exception:
            self._prev_cpu_ticks = None
            self.data.source_status['cpu'] = 'unavailable'

    def _collect_memory(self):
        try:
            out = subprocess.check_output(
                ["vm_stat"],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            page_size_match = re.search(r'page size of (\d+) bytes', out)
            if not page_size_match:
                raise ValueError('Missing VM page size')
            page_size = int(page_size_match.group(1))
            pages = {}
            for line in out.split("\n"):
                m = re.match(r'([^:]+):\s+([\d,]+)', line.strip())
                if m:
                    key = m.group(1).strip()
                    val = int(m.group(2).replace(",", ""))
                    pages[key] = val

            if not {'Pages free', 'Pages wired down', 'Pages occupied by compressor', 'Anonymous pages', 'File-backed pages'} <= pages.keys():
                raise ValueError('Incomplete VM counters')

            free = pages.get("Pages free", 0) + pages.get("Pages speculative", 0)
            active = pages.get("Pages active", 0)
            inactive = pages.get("Pages inactive", 0)
            wired = pages.get("Pages wired down", 0)
            compressed = pages.get("Pages occupied by compressor", 0)
            purgeable = pages.get("Pages purgeable", 0)
            file_backed = pages.get("File-backed pages", inactive + purgeable)
            anonymous = pages.get("Anonymous pages", max(0, active - purgeable))

            total = self.data.sys_info.ram_gb
            if total <= 0:
                raise ValueError('Unknown physical memory size')
            to_gb = page_size / (1024**3)

            self.data.mem.total_gb = total
            self.data.mem.wired_gb = wired * to_gb
            self.data.mem.compressed_gb = compressed * to_gb
            # vm_stat 的 active/inactive 不是“应用/缓存”的互斥分类。
            # anonymous 与 file-backed 更接近用户理解中的 App 内存和文件缓存。
            self.data.mem.app_memory_gb = anonymous * to_gb
            self.data.mem.cached_gb = file_backed * to_gb
            self.data.mem.free_gb = free * to_gb
            self.data.mem.used_gb = min(
                total,
                self.data.mem.app_memory_gb
                + self.data.mem.wired_gb
                + self.data.mem.compressed_gb
            )
            self.data.mem.available_gb = max(0.0, total - self.data.mem.used_gb)
            self.data.source_status['memory'] = 'ok'
        except Exception:
            self.data.source_status['memory'] = 'unavailable'

        # Swap via sysctl
        try:
            out = subprocess.check_output(
                ["sysctl", "vm.swapusage"],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            m = re.search(r'total = ([\d.]+)M.*used = ([\d.]+)M', out)
            if m:
                self.data.mem.swap_total_gb = float(m.group(1)) / 1024
                self.data.mem.swap_used_gb = float(m.group(2)) / 1024
                self.data.source_status['swap'] = 'ok'
            else:
                raise ValueError('Missing swap usage')
        except Exception:
            self.data.source_status['swap'] = 'unavailable'

        # Memory pressure level
        try:
            level = int(subprocess.check_output(
                ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode().strip())
            self.data.mem.pressure_level = {1: 'Normal', 2: 'Warning', 4: 'Critical'}.get(level, 'Unknown')
            self.data.source_status['memory_pressure'] = 'ok' if level in (1, 2, 4) else 'unavailable'
        except Exception:
            self.data.mem.pressure_level = 'Unknown'
            self.data.source_status['memory_pressure'] = 'unavailable'

    def _collect_disk(self, now: Optional[float] = None):
        """读取 IOBlockStorageDriver 原始累计计数并计算真实读写速率。"""
        try:
            out = subprocess.check_output(
                # Limit depth to the driver itself: descendants include APFS and
                # partition Statistics, which would count the same I/O again.
                ["ioreg", "-l", "-r", "-c", "IOBlockStorageDriver", "-d", "1"],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            now = time.monotonic() if now is None else now
            read_bytes = write_bytes = read_ops = write_ops = 0
            stat_rows = re.findall(r'"Statistics"\s*=\s*\{([^}]*)\}', out)
            if not stat_rows:
                raise ValueError('No disk counters')
            devices = tuple(re.findall(r'<class IOBlockStorageDriver, id ([^,]+),', out))
            for stats in stat_rows:
                values = dict(re.findall(r'"([^"]+)"\s*=\s*(\d+)', stats))
                if not {'Bytes (Read)', 'Bytes (Write)', 'Operations (Read)', 'Operations (Write)'} <= values.keys():
                    raise ValueError('Incomplete disk counters')
                read_bytes += int(values.get("Bytes (Read)", 0))
                write_bytes += int(values.get("Bytes (Write)", 0))
                read_ops += int(values.get("Operations (Read)", 0))
                write_ops += int(values.get("Operations (Write)", 0))

            current = (read_bytes, write_bytes, read_ops, write_ops)
            self.data.disk.read_mb_s = self.data.disk.write_mb_s = 0.0
            self.data.disk.read_ops = self.data.disk.write_ops = 0.0
            self.data.source_status['disk'] = 'warming'
            if (self._prev_disk is not None and self._prev_disk_time is not None
                    and devices == getattr(self, '_prev_disk_devices', devices)):
                dt = now - self._prev_disk_time
                if dt > 0 and all(a >= b for a, b in zip(current, self._prev_disk)):
                    self.data.disk.read_mb_s = (read_bytes - self._prev_disk[0]) / dt / (1024**2)
                    self.data.disk.write_mb_s = (write_bytes - self._prev_disk[1]) / dt / (1024**2)
                    self.data.disk.read_ops = (read_ops - self._prev_disk[2]) / dt
                    self.data.disk.write_ops = (write_ops - self._prev_disk[3]) / dt
                    self.data.source_status['disk'] = 'ok'
            self._prev_disk = current
            self._prev_disk_time = now
            self._prev_disk_devices = devices
        except Exception:
            self._prev_disk = None
            self.data.disk.read_mb_s = self.data.disk.write_mb_s = 0.0
            self.data.disk.read_ops = self.data.disk.write_ops = 0.0
            self.data.source_status['disk'] = 'unavailable'

    def _collect_disk_capacity(self):
        """APFS 共享空间：总容量减可用空间，包含同容器其他卷与快照。"""
        try:
            path = '/System/Volumes/Data' if os.path.exists('/System/Volumes/Data') else '/'
            out = subprocess.check_output(
                ["df", "-k", path],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            lines = out.strip().split("\n")
            parts = lines[-1].split()
            total, available = int(parts[1]), int(parts[3])
            if total <= 0 or not 0 <= available <= total:
                raise ValueError('Invalid disk capacity')
            self.data.disk.root_total_gb = total / 1024**2
            self.data.disk.root_used_gb = (total - available) / 1024**2
            self.data.disk.root_avail_gb = available / 1024**2
            self.data.disk.root_pct = (total - available) * 100 / total
            self.data.source_status['disk_capacity'] = 'ok'
        except Exception:
            self.data.source_status['disk_capacity'] = 'unavailable'

    def _parse_df_size(self, size_str: str) -> float:
        """解析 df -h 输出的大小为 GB（支持 G/Gi 后缀）"""
        try:
            s = size_str.rstrip('i')  # 处理 Gi/Ti/Mi 等二进制单位后缀
            if s.endswith('T'):
                return float(s[:-1]) * 1024
            elif s.endswith('G'):
                return float(s[:-1])
            elif s.endswith('M'):
                return float(s[:-1]) / 1024
            elif s.endswith('K'):
                return float(s[:-1]) / (1024 * 1024)
            return float(s)
        except Exception:
            return 0.0

    def _collect_network(self, now: Optional[float] = None):
        try:
            out = subprocess.check_output(
                ["netstat", "-ibn"],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            now = time.monotonic() if now is None else now
            interfaces = {}
            for line in out.split("\n"):
                parts = line.split()
                # 只读取每个接口唯一的 <Link#...> 行，避免 IPv4/IPv6 重复累计。
                if len(parts) >= 10 and parts[2].startswith("<Link#"):
                    try:
                        counts = parts[-7:]
                        interfaces[parts[0].rstrip('*')] = (
                            int(counts[2]), int(counts[5]), int(counts[0]), int(counts[3])
                        )
                    except (ValueError, IndexError):
                        continue

            candidates = [name for name in interfaces if name.startswith("en")]
            # 优先使用默认路由的实际出口；外接网卡、VPN 或 Wi-Fi 切换时不能写死 en0。
            old_interface = self._primary_interface
            if now - self._interface_checked_at >= 5.0 or self._primary_interface not in interfaces:
                self._interface_checked_at = now
                self._primary_interface = None
                try:
                    route_out = subprocess.check_output(
                        ["route", "-n", "get", "default"],
                        stderr=subprocess.DEVNULL, timeout=2
                    ).decode()
                    m = re.search(r'^\s*interface:\s*(\S+)', route_out, re.MULTILINE)
                    if m and m.group(1) in interfaces:
                        self._primary_interface = m.group(1)
                except Exception:
                    pass
            if self._primary_interface not in interfaces:
                active = [name for name in candidates if interfaces[name][0] or interfaces[name][1]]
                self._primary_interface = ("en0" if "en0" in active else (active[0] if active else None))
            if not self._primary_interface:
                raise ValueError('No network interface')

            self.data.net.interface = self._primary_interface
            if old_interface != self._primary_interface:
                self._prev_net = None
            self.data.net.in_mb_s = self.data.net.out_mb_s = 0.0
            self.data.source_status['network'] = 'warming'

            total_in, total_out, in_packets, out_packets = interfaces[self._primary_interface]
            if self._prev_net is not None and self._prev_net_time is not None:
                dt = now - self._prev_net_time
                if dt > 0 and total_in >= self._prev_net[0] and total_out >= self._prev_net[1]:
                    self.data.net.in_mb_s = (total_in - self._prev_net[0]) / dt / (1024**2)
                    self.data.net.out_mb_s = (total_out - self._prev_net[1]) / dt / (1024**2)
                    self.data.source_status['network'] = 'ok'
            self.data.net.in_packets = in_packets
            self.data.net.out_packets = out_packets
            self._prev_net = (total_in, total_out)
            self._prev_net_time = now
        except Exception:
            self._prev_net = None
            self.data.net.in_mb_s = self.data.net.out_mb_s = 0.0
            self.data.source_status['network'] = 'unavailable'

    def _collect_thermal(self):
        try:
            out = subprocess.check_output(
                ["pmset", "-g", "therm"],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            values = {k: int(v) for k, v in re.findall(r'(CPU_\w+)\s*=\s*(\d+)', out)}
            self.data.thermal.cpu_warning = values.get('CPU_Warning_Level', 0) > 0
            self.data.thermal.scheduler_limit = values.get('CPU_Scheduler_Limit', 100) < 100
            self.data.thermal.cpu_nominal = bool(values) and not (
                self.data.thermal.cpu_warning or self.data.thermal.scheduler_limit
                or values.get('CPU_Speed_Limit', 100) < 100)
            # pmset reports historical warnings/limits, not current thermal pressure.
            self.data.thermal.thermal_pressure = 'Unknown'
            self.data.source_status['thermal'] = 'unavailable'
        except Exception:
            self.data.thermal.thermal_pressure = 'Unknown'
            self.data.source_status['thermal'] = 'unavailable'

    def _collect_aux_sensors(self):
        """通过 AppleSMC 读取 CPU/GPU 温度与风扇，并补充电池温度。"""
        sensors = []
        fans = []
        if self._smc:
            try:
                sensors, fans = self._smc.snapshot()
            except Exception:
                pass

        try:
            out = subprocess.check_output(
                ["ioreg", "-r", "-c", "AppleSmartBattery", "-l", "-w0"],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            m = re.search(r'^\s*"Temperature"\s*=\s*(\d+)', out, re.MULTILINE)
            if m:
                temp = float(m.group(1)) / 100.0
                if -20 <= temp <= 100:
                    sensors.append(("电池", temp))
        except Exception:
            pass
        self._aux_sensors = sensors
        self._aux_fans = fans
        cpu_values = [value for name, value in sensors if name.startswith("CPU ")]
        gpu_values = [value for name, value in sensors if name.startswith("GPU ")]
        self.data.cpu.temp_c = sum(cpu_values) / len(cpu_values) if cpu_values else 0.0
        self.data.gpu.temp_c = sum(gpu_values) / len(gpu_values) if gpu_values else 0.0
        self.data.thermal.sensors = list(sensors)
        self.data.thermal.fans = list(fans)
        self.data.source_status['sensors'] = 'ok' if sensors or fans else 'unavailable'

    def _collect_processes(self):
        reader = getattr(self, '_process_reader', None)
        if reader is not None:
            try:
                sample = reader.collect(total_cores=self.data.sys_info.total_cores,
                                        ram_gb=self.data.sys_info.ram_gb)
                self.data.process_sample = sample
                self.data.processes = sample.processes
                self.data.source_status['processes'] = sample.status
                # A later fallback must not bridge an old ps CPU baseline.
                self._prev_process_times = {}
                self._prev_process_time = None
                return
            except ProcessUnavailable:
                pass
        self._collect_processes_ps()

    def _collect_processes_ps(self):
        """Fallback only: never mix RSS into an application's footprint total."""
        try:
            out = subprocess.check_output(
                ["ps", "-ww", "-Ao", "pid=,lstart=,time=,rss=,comm="],
                stderr=subprocess.DEVNULL, timeout=3,
                env={**os.environ, "LC_ALL": "C"}
            ).decode()
            now = time.monotonic()
            previous = getattr(self, '_prev_process_times', {})
            previous_time = getattr(self, '_prev_process_time', None)
            elapsed = now - previous_time if previous_time is not None else 0
            current = {}
            procs = []
            for line in out.split("\n"):
                parts = line.split(None, 8)
                if len(parts) >= 9:
                    try:
                        pid = int(parts[0])
                        identity = (pid, ' '.join(parts[1:6]))
                        duration = parts[6]
                        days = 0
                        if '-' in duration:
                            day_text, duration = duration.split('-', 1)
                            days = int(day_text)
                        seconds = 0.0
                        for component in duration.split(':'):
                            seconds = seconds * 60 + float(component)
                        seconds += days * 86400
                        current[identity] = seconds
                        old = previous.get(identity)
                        cpu = 0.0
                        if old is not None and elapsed > 0 and seconds >= old:
                            cpu = min(100.0, (seconds - old) * 100 / elapsed
                                      / max(1, self.data.sys_info.total_cores))
                        rss_mb = float(parts[7]) / 1024
                        ram_mb = self.data.sys_info.ram_gb * 1024
                        procs.append(ProcessInfo(
                            pid=pid, cpu_pct=cpu,
                            cpu_available=old is not None and elapsed > 0 and seconds >= old,
                            mem_pct=rss_mb * 100 / ram_mb if ram_mb > 0 else 0.0,
                            mem_mb=rss_mb,
                            name=os.path.basename(parts[8])[:80]
                        ))
                    except (ValueError, IndexError):
                        pass
            if not procs:
                raise ValueError('No readable process rows')
            self._prev_process_times = current
            self._prev_process_time = now
            self.data.processes = sorted(procs, key=lambda p: p.cpu_pct, reverse=True)
            self.data.source_status['processes'] = 'ok' if previous and elapsed > 0 else 'warming'
        except Exception:
            self._prev_process_times = {}
            self._prev_process_time = None
            self.data.processes = []
            self.data.source_status['processes'] = 'unavailable'
        self.data.process_sample = ProcessSnapshot(
            status=self.data.source_status['processes'], processes=self.data.processes,
            memory_kind='rss', enumerated_count=len(self.data.processes),
            readable_count=len(self.data.processes))

    def _collect_uptime(self):
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "kern.boottime"],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            m = re.search(r'sec = (\d+)', out)
            if m:
                boot_time = int(m.group(1))
                self.data.uptime_s = time.time() - boot_time
                self.data.source_status['uptime'] = 'ok'
            else:
                raise ValueError('Missing boot time')
        except Exception:
            self.data.source_status['uptime'] = 'unavailable'

    def _merge_powermetrics(self):
        """Missing fields remain unavailable, even when running as root."""
        with self._pm_lock:
            pm = dict(self._pm_data)
            age = time.monotonic() - self._pm_last_update if self._pm_last_update else float("inf")
        proc = getattr(self, "_pm_proc", None)
        exited = proc is not None and proc.poll() is not None
        fresh = age <= max(4.0, self.data.refresh_interval * 4) and not exited
        self.data.pm_metrics = set(pm) - {"sensors", "fans"} if fresh else set()
        if not fresh:
            pm = {}
        if exited or getattr(self, "_pm_error", ""):
            self.data.pm_status = "采集不可用（运行 --diagnose 查看原因）"
        elif fresh:
            self.data.pm_status = "正在采集（仅显示可用字段）"
        elif self.data.has_sudo:
            self.data.pm_status = "等待采样或数据已过期"
        else:
            self.data.pm_status = "未启用（功率/频率需要 sudo）"

        self.data.cpu.p_freq_mhz = pm.get("p_freq", 0.0)
        self.data.cpu.e_freq_mhz = pm.get("e_freq", 0.0)
        self.data.cpu.cluster_freqs = pm.get("cluster_freqs", {})
        self.data.cpu.power_w = pm.get("cpu_power", 0.0)
        self.data.gpu.freq_mhz = pm.get("gpu_freq", 0.0)
        self.data.gpu.power_w = pm.get("gpu_power", 0.0)
        self.data.gpu.usage_pct = pm.get("gpu_usage", 0.0)
        self.data.gpu.active = pm.get("gpu_active", False)
        self.data.ane.power_w = pm.get("ane_power", 0.0)
        sensors = list(self._aux_sensors)
        for prefix, stats, key in (
            ("CPU ", self.data.cpu, "cpu_temp"), ("GPU ", self.data.gpu, "gpu_temp")
        ):
            values = [value for name, value in sensors if name.startswith(prefix)]
            stats.temp_c = sum(values) / len(values) if values else pm.get(key, 0.0)
            if not values and key in pm:
                sensors.append((prefix + "温度", pm[key]))
        self.data.thermal.sensors = sensors
        self.data.thermal.fans = list(self._aux_fans) or list(pm.get("fans", []))
        self.data.thermal.thermal_pressure = pm.get("thermal_pressure", "Unknown")
        self.data.source_status["thermal"] = "ok" if "thermal_pressure" in pm else "unavailable"
        self.data.source_status["sensors"] = "ok" if sensors or self.data.thermal.fans else "unavailable"


# ──────────────────────────── UI 渲染器 ────────────────────────────

class MonitorUI:
    """Cell-width aware, scrollable terminal dashboard; missing data stays explicit."""

    COLOR_TITLE = 1
    COLOR_HEADER = 2
    COLOR_VALUE = 3
    COLOR_WARNING = 4
    COLOR_GOOD = 5
    COLOR_DIM = 6
    COLOR_ACCENT = 7
    COLOR_CHART = 8
    COLOR_CHART2 = 9
    COLOR_GPU = 10
    COLOR_TEMP = 11
    PAGE_OVERVIEW, PAGE_CPU, PAGE_GPU, PAGE_MEMORY, PAGE_PROCESSES, PAGE_SENSORS, PAGE_DOCKER = range(7)
    PAGE_NAMES = ["总览", "CPU", "GPU", "内存", "应用/进程", "传感器", "Docker"]
    PROCESS_DISPLAY_LIMIT = 15

    def __init__(self, stdscr: curses.window, data: MonitorData, docker_actions=None):
        self.stdscr, self.data = stdscr, data
        self.page = 0
        self.paused = False
        self.process_sort = "cpu"
        self.process_view = "apps"
        self.app_selected_key = ""
        self.app_expanded = False
        self.docker_sort = "cpu"
        self.docker_grouped = True
        self.docker_view = "containers"
        self.docker_project_selected = ""
        self.docker_project_expanded = False
        self.docker_detail_expanded = False
        self.docker_help_expanded = False
        self.docker_selected_id = ""
        self.docker_actions = docker_actions
        self.docker_readouts = DockerReadouts(context=getattr(docker_actions, "context", None)) if docker_actions is not None else None
        self._docker_readout_notice = ""
        self._docker_dialog = None
        self._docker_dialog_fits = False
        self._docker_notice = ""
        self.sensor_scroll = 0
        self._scroll = {}
        self._init_colors()

    def _init_colors(self):
        self._colors = False
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            background = curses.COLOR_BLACK
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                pass
            colors = [curses.COLOR_CYAN, curses.COLOR_YELLOW, curses.COLOR_WHITE,
                      curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_WHITE,
                      curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_GREEN,
                      curses.COLOR_MAGENTA, curses.COLOR_RED]
            for number, foreground in enumerate(colors, 1):
                if number < curses.COLOR_PAIRS:
                    curses.init_pair(number, foreground, background)
            self._colors = True
        except curses.error:
            pass

    @staticmethod
    def _cell_width(character):
        import unicodedata
        if unicodedata.combining(character):
            return 0
        if unicodedata.category(character).startswith("C"):
            return 0
        return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1

    @classmethod
    def _wrap(cls, text, width):
        import unicodedata
        if width <= 0:
            return [""]
        rows, current, used = [], "", 0
        for character in str(text):
            if character == "\n":
                rows.append(current)
                current, used = "", 0
                continue
            if unicodedata.category(character).startswith("C"):
                continue
            size = cls._cell_width(character)
            if used + size > width:
                rows.append(current)
                current, used = "", 0
            if size <= width:
                current += character
                used += size
        rows.append(current)
        return rows

    def _set(self, y, x, text, color=None, bold=False):
        try:
            h, w = self.stdscr.getmaxyx()
            if not (0 <= y < h and 0 <= x < w):
                return
            width = w - x - (1 if y == h - 1 else 0)
            if width <= 0:
                return
            attr = curses.A_BOLD if bold else 0
            if getattr(self, "_colors", False) and color is not None and color < curses.COLOR_PAIRS:
                attr |= curses.color_pair(color)
            clipped = self._wrap(text, width)[0]
            if clipped:
                self.stdscr.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def handle_key(self, key):
        if getattr(self, "_docker_dialog", None) is not None:
            return self._handle_docker_dialog(key)
        if key in (ord("q"), ord("Q")):
            return False
        if not hasattr(self, "_scroll"):
            self._scroll = {}
        if key in (ord("p"), ord("P")):
            self.paused = not self.paused
        if ord("1") <= key < ord("1") + len(self.PAGE_NAMES):
            self.page = key - ord("1")
        if key == curses.KEY_LEFT:
            self.page = (self.page - 1) % len(self.PAGE_NAMES)
        if key == curses.KEY_RIGHT:
            self.page = (self.page + 1) % len(self.PAGE_NAMES)
        if self.page == self.PAGE_PROCESSES and key in (ord("c"), ord("C"), ord("m"), ord("M")):
            self.process_sort = "memory" if chr(key).lower() == "m" else "cpu"
            self._scroll[self.page] = 0
        if self.page == self.PAGE_PROCESSES:
            if key in (ord("v"), ord("V")):
                self.process_view = "processes" if self.process_view == "apps" else "apps"
                self._scroll[self.page] = 0
            if self.process_view == "apps":
                if key in (ord("n"), ord("N"), ord("b"), ord("B")):
                    apps = self._process_apps()
                    if apps:
                        selected = self._selected_app(apps)
                        index = next(i for i, app in enumerate(apps) if app.key == selected.key)
                        direction = 1 if chr(key).lower() == "n" else -1
                        self.app_selected_key = apps[(index + direction) % len(apps)].key
                        self._scroll[self.page] = 0
                if key in (10, 13, curses.KEY_ENTER):
                    if self._selected_app(self._process_apps()) is not None:
                        self.app_expanded = not self.app_expanded
                        self._scroll[self.page] = 0
                if key == 27:
                    self.app_expanded = False
                    self._scroll[self.page] = 0
        if self.page == self.PAGE_DOCKER:
            if key in (ord("v"), ord("V")):
                self.docker_view = "projects" if self.docker_view == "containers" else "containers"
                self.docker_project_selected = ""
                self.docker_project_expanded = False
                self._docker_notice = ""
                self._scroll[self.page] = 0
            if self.docker_view == "projects" and key in (ord("n"), ord("N"), ord("b"), ord("B")):
                projects = self._docker_projects()
                if projects:
                    names = [name for name, _ in projects]
                    current = self.docker_project_selected if self.docker_project_selected in names else names[0]
                    index = names.index(current)
                    direction = 1 if chr(key).lower() == "n" else -1
                    self.docker_project_selected = names[(index + direction) % len(names)]
                    self.docker_project_expanded = True
            if self.docker_view == "projects" and key in (10, 13, curses.KEY_ENTER):
                projects = self._docker_projects()
                if projects and not self.docker_project_selected:
                    self.docker_project_selected = projects[0][0]
                    self.docker_project_expanded = True
                elif self.docker_project_selected:
                    self.docker_project_expanded = not self.docker_project_expanded
            if self.docker_view == "projects" and key == 27:
                self.docker_project_selected = ""
                self.docker_project_expanded = False
            if key in (ord("o"), ord("O")):
                if self.docker_view == "projects":
                    self._docker_notice = "项目视图当前只读；操作仍需回到单容器视图逐个选择"
                else:
                    self._open_docker_actions()
            if key in (ord("l"), ord("L"), ord("i"), ord("I")):
                if self.docker_view == "projects":
                    self._docker_notice = "请切回容器视图选择具体容器"
                else:
                    self._open_docker_readout("logs" if chr(key).lower() == "l" else "inspect")
            if key in (ord("x"), ord("X")) and self.docker_readouts is not None:
                self.docker_readouts.clear()
            if key in (ord("d"), ord("D")) and self.docker_view == "containers":
                self.docker_detail_expanded = not self.docker_detail_expanded
                self._scroll[self.page] = 0
            if key in (ord("h"), ord("H"), ord("?")):
                self.docker_help_expanded = not self.docker_help_expanded
                self._scroll[self.page] = 0
            if self.docker_view == "containers" and key in (ord("c"), ord("C"), ord("m"), ord("M")):
                self.docker_sort = "memory" if chr(key).lower() == "m" else "cpu"
                self._scroll[self.page] = 0
            if self.docker_view == "containers" and key in (ord("g"), ord("G")):
                self.docker_grouped = not self.docker_grouped
                self._scroll[self.page] = 0
            if self.docker_view == "containers" and key in (ord("n"), ord("N"), ord("b"), ord("B")):
                containers = self._docker_containers()
                if containers:
                    selected = self._docker_selected(containers)
                    index = next(i for i, item in enumerate(containers) if item.id == selected.id)
                    direction = 1 if chr(key).lower() == "n" else -1
                    self.docker_selected_id = containers[(index + direction) % len(containers)].id
                    # The detail panel follows the list; keep selection shortcuts useful
                    # without taking away the existing j/k page scrolling controls.
                    self._scroll[self.page] = 10 ** 9
        offset = self.sensor_scroll if self.page == self.PAGE_SENSORS else self._scroll.get(self.page, 0)
        step = max(1, self.stdscr.getmaxyx()[0] - 5)
        if key in (curses.KEY_DOWN, ord("j"), ord("J")):
            offset += 1
        if key in (curses.KEY_UP, ord("k"), ord("K")):
            offset -= 1
        if key == curses.KEY_NPAGE:
            offset += step
        if key == curses.KEY_PPAGE:
            offset -= step
        if key == curses.KEY_HOME:
            offset = 0
        if key == curses.KEY_END:
            offset = 10 ** 9
        self._scroll[self.page] = max(0, offset)
        if self.page == self.PAGE_SENSORS:
            self.sensor_scroll = max(0, offset)
        return True

    def _open_docker_actions(self):
        self._scroll[self.PAGE_DOCKER] = 0
        if self.paused:
            self._docker_notice = "请先恢复采样，再打开容器操作"
            return
        if self.docker_actions is None:
            self._docker_notice = "操作入口尚未连接"
            return
        if self.docker_actions.snapshot().status == "running":
            self._docker_notice = "已有操作正在执行，请勿重复提交"
            return
        selected = self._docker_selected(self._docker_containers())
        if selected is None:
            self._docker_notice = "请等待容器数据并选择一个容器"
            return
        try:
            target = ActionTarget.capture(self.data.docker, selected)
            if not target.allowed():
                self._docker_notice = ("该容器会在停止时自动删除，首期不提供停止或重启" if target.auto_remove
                                       else "当前容器状态不支持操作，请等待状态稳定")
                return
        except DockerError as exc:
            self._docker_notice = exc.message
            return
        self._docker_notice = ""
        self._docker_dialog = {"target": target, "action": None, "confirm": False}
        self._docker_dialog_fits = False

    def _open_docker_readout(self, kind):
        if self.docker_readouts is None:
            self._docker_readout_notice = "只读详情入口尚未连接"
            return
        selected = self._docker_selected(self._docker_containers())
        if selected is None:
            self._docker_readout_notice = "请先等待并选择一个容器"
            return
        if not self.docker_readouts.submit(kind, selected, self.data.docker):
            self._docker_readout_notice = "已有 Docker 只读请求正在读取"
        else:
            self._docker_readout_notice = "已提交只读请求；结果不会保存"

    def _handle_docker_dialog(self, key):
        dialog = self._docker_dialog
        if key in (27, ord("q"), ord("Q")):
            self._docker_dialog = None
            self._scroll[self.PAGE_DOCKER] = 0
            return True
        if (not self._docker_dialog_fits or
                self.stdscr.getmaxyx() != getattr(self, "_docker_dialog_size", None)):
            return True  # Never execute an unseen/truncated confirmation.
        if dialog["action"] is None:
            action = {ord("s"): "start", ord("t"): "stop", ord("r"): "restart",
                      ord("p"): "pause", ord("u"): "unpause"}.get(key)
            if action in dialog["target"].allowed():
                dialog["action"] = action
                dialog["confirm"] = False
                self._docker_dialog_fits = False
            return True
        if key in (9, curses.KEY_LEFT, curses.KEY_RIGHT):
            dialog["confirm"] = not dialog["confirm"]
        if key in (10, 13, curses.KEY_ENTER):
            if dialog["confirm"]:
                if self.paused or getattr(self.data.docker, "status", "") != "ok":
                    self._docker_notice = "采样暂停或连接已失效；未执行，请刷新后重新选择"
                elif not self.docker_actions.submit(dialog["target"], dialog["action"]):
                    self._docker_notice = "未提交：已有操作正在执行或目标状态不支持"
            else:
                self._docker_notice = "已取消，未执行容器操作"
            self._docker_dialog = None
            self._scroll[self.PAGE_DOCKER] = 0
        return True

    def _render_docker_dialog(self):
        dialog = self._docker_dialog
        target, action = dialog["target"], dialog["action"]
        h, w = self.stdscr.getmaxyx()
        self._docker_dialog_size = (h, w)
        rows = ["容器操作 / " + (LABELS[action] + "确认" if action else "选择操作"),
                "容器: " + target.name, "Compose: " + (target.project or "独立容器"),
                "ID: " + target.id, "连接: " + target.context,
                "只操作此容器，不操作整个 Compose 项目。"]
        if action:
            if action == "stop":
                rows += ["将中断此容器服务；使用 Docker 配置的正常停止流程。",
                         "宽限期结束后 Docker 可能强制终止进程。"]
            elif action == "restart":
                rows += ["将重启此容器；不会自动重启关联服务。"]
            elif action == "pause":
                rows += ["将冻结此容器内进程；网络连接可能暂时无响应。"]
            elif action == "unpause":
                rows += ["将恢复此容器内已冻结的进程。"]
            else:
                rows += ["启动此容器，不自动启动关联服务。"]
            rows += ["超时不自动重试；退出程序不会撤销已提交的操作。",
                     "命令完成不代表应用或健康检查已就绪。",
                     "Tab / ←→ 切换，Enter 选择；Esc 取消",
                     ("取消    [确认执行]" if dialog["confirm"] else "[取消]    确认执行")]
        else:
            rows += [" | ".join({"start": "s 启动", "stop": "t 停止", "restart": "r 重启",
                                  "pause": "p 暂停", "unpause": "u 恢复"}[a]
                                for a in target.allowed()), "Esc / q 取消"]
        wrapped = [line for row in rows for line in self._wrap(row, max(1, w - 2))]
        self._docker_dialog_fits = w >= 24 and len(wrapped) <= h - 1
        if not self._docker_dialog_fits:
            self._set(0, 0, "请放大终端以完整查看操作确认", self.COLOR_WARNING)
            self._set(min(1, h - 1), 0, "操作已锁定；Esc / q 取消", self.COLOR_DIM)
        else:
            for index, row in enumerate(wrapped):
                self._set(index, 1, row, self.COLOR_WARNING if action else self.COLOR_ACCENT, index == 0)
        self.stdscr.refresh()

    def _ok(self, source):
        return getattr(self.data, "source_status", {}).get(source) == "ok"

    def _value(self, source, value, suffix="", precision=1):
        return f"{value:.{precision}f}{suffix}" if self._ok(source) else "--"

    def _pm(self, key, value, suffix, precision=1):
        return f"{value:.{precision}f}{suffix}" if key in getattr(self.data, "pm_metrics", set()) else "--"

    def _developer_sources(self):
        """Compact live provenance for development builds only."""
        meta = getattr(self.data, "source_meta", {})
        if not getattr(self.data, "developer_mode", False) or not meta:
            return None
        keys = {
            self.PAGE_OVERVIEW: ("cpu", "memory", "network", "powermetrics"),
            self.PAGE_CPU: ("cpu", "thermal", "powermetrics"),
            self.PAGE_GPU: ("powermetrics", "sensors"),
            self.PAGE_MEMORY: ("memory", "memory_pressure", "swap"),
            self.PAGE_PROCESSES: ("processes",),
            self.PAGE_SENSORS: ("sensors", "thermal"),
            self.PAGE_DOCKER: ("docker",),
        }.get(self.page, ())
        parts = []
        for key in keys:
            item = meta.get(key)
            if not item:
                continue
            status = item.get("status", "?")
            age = item.get("age_s")
            age_text = "--" if age is None else f"{float(age):.1f}s"
            source = str(item.get("source", "?"))
            parts.append(f"{key}={source}/{status}/{age_text}")
        return "DEV | " + " | ".join(parts) if parts else "DEV | 来源元数据等待采样"

    def _temperature(self, value):
        return f"{value:.1f}°C" if value > 0 else "--"

    def _trend(self, values, unit):
        if not values:
            return "趋势: 等待采样"
        values = list(values)[-40:]
        low, high = min(values), max(values)
        chars = "▁▂▃▄▅▆▇█"
        chart = "".join(chars[min(7, int((v - low) / (high - low) * 7))] if high > low else "▄" for v in values)
        return f"趋势 {chart}  范围 {low:.1f}–{high:.1f}{unit}"

    def _cpu_lines(self):
        d = self.data
        lines = [
            "CPU",
            "使用率: " + self._value("cpu", d.cpu.user + d.cpu.sys, "%"),
            "用户: " + self._value("cpu", d.cpu.user, "%") + "  系统: " + self._value("cpu", d.cpu.sys, "%"),
            "负载 1/5/15 分钟: " + (" / ".join(f"{v:.2f}" for v in (d.cpu.load_1m, d.cpu.load_5m, d.cpu.load_15m)) if self._ok("cpu") else "--"),
            "温度（已识别探头平均）: " + self._temperature(d.cpu.temp_c),
            "CPU 功率: " + self._pm("cpu_power", d.cpu.power_w, " W"),
        ]
        groups = getattr(d.sys_info, "core_groups", [])
        lines.append("核心配置: " + (" / ".join(f"{name} {count} 核" for name, count in groups) if groups else "未知"))
        lines.append(f"逻辑 CPU: {d.sys_info.total_cores or '--'}")
        frequencies = getattr(d.cpu, "cluster_freqs", {})
        if "cluster_freqs" in getattr(d, "pm_metrics", set()) and frequencies:
            lines += [f"{name} 活跃频率: {value:.0f} MHz" for name, value in frequencies.items()]
        else:
            lines.append("集群活跃频率: --")
        lines.append("热压力: " + (d.thermal.thermal_pressure if self._ok("thermal") else "--"))
        if self._ok("cpu"):
            lines.append(self._trend(d.cpu_history, "%"))
        if d.cpu.temp_c > 0:
            lines.append(self._trend(d.cpu_temp_history, "°C"))
        return lines

    def _gpu_lines(self):
        d = self.data
        return [
            "GPU",
            "活跃驻留率: " + self._pm("gpu_usage", d.gpu.usage_pct, "%"),
            "活跃频率: " + self._pm("gpu_freq", d.gpu.freq_mhz, " MHz", 0),
            "GPU 功率: " + self._pm("gpu_power", d.gpu.power_w, " W", 2),
            "温度（已识别探头平均）: " + self._temperature(d.gpu.temp_c),
            "ANE 功率: " + self._pm("ane_power", d.ane.power_w, " W", 2),
            "活跃驻留率表示采样期间 GPU 处于活跃状态的时间比例。",
            "活跃频率为工作时频率，不代表空闲时仍以此频率运行。",
        ] + ([self._trend(d.gpu_temp_history, "°C")] if d.gpu.temp_c > 0 else [])

    def _memory_lines(self):
        d = self.data
        return [
            "内存（GiB，1 GiB = 1024³ 字节）",
            "占用估算: " + self._value("memory", d.mem.used_gb, " GiB") + " / " + (f"{d.mem.total_gb:.1f} GiB" if d.mem.total_gb > 0 else "--"),
            "匿名页: " + self._value("memory", d.mem.app_memory_gb, " GiB"),
            "固定内存: " + self._value("memory", d.mem.wired_gb, " GiB"),
            "压缩器占用: " + self._value("memory", d.mem.compressed_gb, " GiB"),
            "文件缓存: " + self._value("memory", d.mem.cached_gb, " GiB"),
            "空闲/推测页: " + self._value("memory", d.mem.free_gb, " GiB"),
            "可回收/空闲估算: " + self._value("memory", d.mem.available_gb, " GiB"),
            "内存压力: " + (d.mem.pressure_level if self._ok("memory_pressure") else "--"),
            "Swap 已用: " + self._value("swap", d.mem.swap_used_gb, " GiB", 2),
            "匿名页并非活动监视器的 App 内存；可用量为估算。",
        ]

    def _disk_lines(self):
        d = self.data
        return [
            "磁盘 I/O",
            "读取: " + self._value("disk", d.disk.read_mb_s, " MiB/s") + "  写入: " + self._value("disk", d.disk.write_mb_s, " MiB/s"),
            "读 IOPS: " + self._value("disk", d.disk.read_ops, "", 0) + "  写 IOPS: " + self._value("disk", d.disk.write_ops, "", 0),
            "启动磁盘共享空间占用: " + self._value("disk_capacity", d.disk.root_used_gb, " GiB") + " / " + self._value("disk_capacity", d.disk.root_total_gb, " GiB"),
            "可用空间: " + self._value("disk_capacity", d.disk.root_avail_gb, " GiB"),
        ]

    @staticmethod
    def _docker_bytes(value):
        if value is None:
            return "--"
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if abs(value) < 1024 or unit == "TiB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024

    def _docker_metric(self, container, field):
        snapshot = getattr(self.data, "docker", None)
        if snapshot is None or snapshot.status != "ok" or container.state not in ("running", "paused"):
            return None
        return getattr(container, field, None)

    @staticmethod
    def _docker_started(container):
        from datetime import datetime, timezone
        try:
            # Docker timestamps carry nanoseconds; Python 3.9 accepts only
            # microseconds here. Preserve the timezone while trimming precision.
            timestamp = re.sub(r"(\.\d{6})\d+", r"\1", container.started_at)
            started = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if started.year < 1970 or started.tzinfo is None:
                return "--"
            label = started.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            if container.state == "running":
                seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
                label += f"（已运行 {seconds // 86400}天 {seconds % 86400 // 3600}时 {seconds % 3600 // 60}分）"
            return label
        except (ValueError, TypeError, AttributeError, OverflowError):
            return "--"

    def _docker_state(self, container):
        snapshot = getattr(self.data, "docker", None)
        if snapshot is None or snapshot.status != "ok":
            return "状态待更新"
        state = {"running": "运行中", "paused": "已暂停", "exited": "已停止",
                 "dead": "已终止", "created": "未启动", "restarting": "重启中",
                 "removing": "移除中"}.get(container.state, "未知状态")
        if container.state == "running":
            health = {"healthy": "健康", "unhealthy": "健康检查失败", "starting": "健康检查中"}.get(
                container.health)
            if health:
                state += " · " + health
        return state

    def _docker_containers(self):
        snapshot = getattr(self.data, "docker", None)
        containers = list(getattr(snapshot, "containers", []))
        field = "memory_bytes" if self.docker_sort == "memory" else "cpu_pct"

        def order(container):
            value = self._docker_metric(container, field)
            return (value is None, -(value or 0), container.name.casefold(), container.id)

        containers.sort(key=order)
        if self.docker_grouped:
            # Stable sorting preserves resource order inside each Compose project.
            containers.sort(key=lambda item: (not bool(item.project), (item.project or "").casefold()))
        return containers

    def _docker_selected(self, containers):
        if not containers:
            self.docker_selected_id = ""
            return None
        selected = next((item for item in containers if item.id == self.docker_selected_id), containers[0])
        self.docker_selected_id = selected.id
        return selected

    def _docker_summary(self):
        snapshot = getattr(self.data, "docker", None)
        if snapshot is None or snapshot.status not in ("ok", "stale"):
            return None
        if snapshot.status == "stale":
            return ["连接中断，等待重新采样", "7 Docker 查看连接状态"]
        containers = snapshot.containers
        running = sum(item.state == "running" for item in containers)
        paused = sum(item.state == "paused" for item in containers)
        stopped = sum(item.state in ("exited", "dead", "created") for item in containers)
        failures = sum(item.state == "running" and item.health == "unhealthy" for item in containers)
        return [f"{snapshot.engine or 'Docker'} | {len(containers)} 个容器",
                f"运行 {running}  暂停 {paused}  停止/未启动 {stopped}",
                f"运行中健康检查失败 {failures} | 7 查看详情"]

    def _docker_command_reference(self):
        """Compact, non-executing command guide based on the user's cheat sheet.

        MacVitals intentionally does not run project-wide or destructive commands
        from a dashboard. This keeps the SSH tool useful as a reference without
        turning a monitoring view into an accidental deployment console.
        """
        return [
            ("常用 Docker 命令 / 仅供复制", self.COLOR_DIM, [
                "只读: docker ps -a  |  docker images  |  docker stats --no-stream",
                "空间: docker system df -v  |  引擎: docker info",
                "Compose 检查: docker compose ps  |  docker compose config --services",
                "Compose 更新/启停请在项目目录执行；MacVitals 不会运行这些命令。",
                "down、prune、rm、rmi、volume prune 和 exec 可能中断服务或删除数据。",
            ])
        ]

    def _docker_projects(self):
        projects = {}
        for container in getattr(getattr(self.data, "docker", None), "containers", []):
            if container.project:
                projects.setdefault(container.project, []).append(container)
        return sorted(projects.items(), key=lambda row: row[0].casefold())

    def _docker_project_blocks(self):
        projects = self._docker_projects()
        if not projects:
            return [("Compose 项目", self.COLOR_HEADER, [
                "没有发现带 Compose 项目标记的现有容器。",
                "没有容器的服务无法从 Docker 容器标签中发现。",
                "项目视图只读，不会扫描磁盘或执行 Compose 配置。",
            ])]
        names = [name for name, _ in projects]
        if self.docker_project_selected not in names:
            self.docker_project_selected = names[0]
        rows = []
        for name, members in projects:
            service_count = len({item.service for item in members if item.service})
            running = sum(item.state == "running" for item in members)
            stopped = sum(item.state in ("exited", "created") for item in members)
            other = len(members) - running - stopped
            rows.append(f"{'>' if name == self.docker_project_selected else ' '} {name} | 服务 {service_count} | 容器 {len(members)} | 运行 {running} / 停止 {stopped} / 其他 {other}")
        blocks = [("Compose 项目 / 只读", self.COLOR_HEADER,
                   ["v 返回容器 | n/b 选择项目 | Enter 展开/收起 | Esc 清除选择"] + rows)]
        if self.docker_project_selected and self.docker_project_expanded:
            chosen = dict(projects).get(self.docker_project_selected, [])
            services = {}
            for item in chosen:
                services.setdefault(item.service or "未标记服务", []).append(item)
            for service, members in sorted(services.items(), key=lambda row: row[0].casefold()):
                running = [item for item in members if item.state == "running"]
                cpu_values = [item.cpu_pct for item in running if item.cpu_pct is not None]
                mem_values = [item.memory_bytes for item in running if item.memory_bytes is not None]
                cpu = f"{sum(cpu_values):.1f}%" if cpu_values else "--"
                memory = self._docker_bytes(sum(mem_values)) if mem_values else "--"
                incomplete = len(cpu_values) != len(running) or len(mem_values) != len(running)
                note = "（读数不完整）" if incomplete else ""
                rows = [f"{item.name} | {self._docker_state(item)} | {item.health or '无健康检查'}"
                        for item in members]
                blocks.append((f"服务 / {service} / {len(members)} 个已有容器", self.COLOR_GOOD,
                               [f"CPU 合计 {cpu} | 内存合计 {memory}{note}"] + rows))
        return blocks

    def _docker_blocks(self):
        snapshot = getattr(self.data, "docker", None)
        if snapshot is None:
            return [("Docker", self.COLOR_CHART, ["等待 Docker 采集器", "Docker 是可选功能，现有硬件监测不受影响。"])]
        if snapshot.status not in ("ok", "stale"):
            return [("Docker / 连接状态", self.COLOR_CHART,
                     [snapshot.message or "等待 Docker 连接", "Docker 是可选功能，现有硬件监测不受影响。"])]
        if self.docker_view == "projects":
            blocks = [("Docker / 引擎", self.COLOR_CHART,
                       [f"{snapshot.engine or 'Docker'} | {snapshot.context or '--'} | 项目只读"])] + self._docker_project_blocks()
            if self._docker_notice:
                blocks.insert(1, ("提示", self.COLOR_WARNING, [self._docker_notice]))
            return blocks
        valid = snapshot.status == "ok"
        cpus = str(snapshot.engine_cpus) if valid and snapshot.engine_cpus is not None else "--"
        memory = self._docker_bytes(snapshot.engine_memory_bytes if valid else None)
        status = "观察中 · 操作需确认" if valid else "连接中断；旧读数已失效"
        connection = [f"{snapshot.engine or 'Docker'} · {snapshot.context or '--'} · {status}",
                      f"{len(snapshot.containers)} 个容器 | {cpus} 虚拟 CPU / {memory} 引擎内存",
                      "Docker CPU 100% ≈ 1 个虚拟核；内存为引擎口径，不是 Mac 整机容量。"]
        blocks = [("Docker / 引擎", self.COLOR_CHART, connection)]
        if self._docker_notice:
            blocks.append(("操作提示", self.COLOR_WARNING, [self._docker_notice]))
        if self._docker_readout_notice:
            blocks.append(("只读请求", self.COLOR_DIM, [self._docker_readout_notice]))
        if self.docker_readouts is not None:
            readout = self.docker_readouts.snapshot()
            if readout["status"] != "idle":
                blocks.append(("临时 " + ("日志" if readout["kind"] == "logs" else "安全 inspect"),
                               self.COLOR_DIM, ["目标: " + readout.get("target", "--") +
                                                " | context: " + readout.get("context", "--") + " | x 清除"] +
                               self._wrap(readout["text"], 100)[:120]))
        result = self.docker_actions.snapshot() if self.docker_actions is not None else None
        if result is not None and result.status != "idle":
            blocks.append(("容器操作 / " + LABELS.get(result.action, ""), self.COLOR_WARNING,
                           ["目标: " + result.target_name, result.message,
                            "退出不会撤销已提交操作；不会自动重试。"] ))
        containers = self._docker_containers()
        selected = self._docker_selected(containers)
        sort_name = "内存" if self.docker_sort == "memory" else "CPU"
        mode = "Compose 分组" if self.docker_grouped else "全部容器排名"
        blocks.append((f"容器 / {mode} / {sort_name}降序", self.COLOR_HEADER,
                       ["v 项目 | n/b 选容器 | c/m 排序 | o 操作 | d 详情 | l 日志 | i inspect | h 帮助",
                       "序号  容器名称 | 状态 | CPU | 内存用量 / 引擎或容器上限"] +
                       (["当前没有容器"] if valid and not containers else [])))
        groups = []
        for index, container in enumerate(containers, 1):
            group = container.project or "独立容器"
            if not self.docker_grouped:
                group = "全部容器"
            if not groups or groups[-1][0] != group:
                groups.append((group, []))
            cpu = self._docker_metric(container, "cpu_pct")
            cpu_text = "--" if cpu is None else f"{cpu:.1f}%"
            memory = self._docker_bytes(self._docker_metric(container, "memory_bytes"))
            limit = self._docker_bytes(self._docker_metric(container, "memory_limit_bytes"))
            marker = ">" if container.id == selected.id else " "
            groups[-1][1].append(f"{marker}{index:2d} {container.name} | {self._docker_state(container)} | {cpu_text} | {memory} / {limit}")
        blocks.extend((group, self.COLOR_GOOD, rows) for group, rows in groups)
        if selected and self.docker_detail_expanded:
            position = next(i for i, item in enumerate(containers, 1) if item.id == selected.id)
            health = "--"
            if valid and selected.state == "running":
                health = {"healthy": "健康", "unhealthy": "检查失败", "starting": "检查中"}.get(
                    selected.health, "未配置健康检查" if not selected.health else "未知状态")
            restart = str(selected.restart_count) if valid and selected.restart_count is not None else "--"
            pids = self._docker_metric(selected, "pids")
            blocks.append((f"容器详情 / {position}/{len(containers)} / {selected.name}", self.COLOR_ACCENT, [
                "状态: " + self._docker_state(selected) + " | 健康检查: " + health,
                f"Compose: {selected.project or '--'} / 服务: {selected.service or '--'}",
                "镜像: " + (selected.image or "--"),
                "端口映射: " + (selected.ports or "无映射"),
                "最近启动: " + (self._docker_started(selected) if valid else "--"),
                "重启次数: " + restart + " | 进程/线程数: " + (str(pids) if pids is not None else "--"),
                "网络累计 接收 / 发送: " + self._docker_bytes(self._docker_metric(selected, "network_rx_bytes")) +
                " / " + self._docker_bytes(self._docker_metric(selected, "network_tx_bytes")),
                "块设备累计 读 / 写: " + self._docker_bytes(self._docker_metric(selected, "block_read_bytes")) +
                " / " + self._docker_bytes(self._docker_metric(selected, "block_write_bytes")),
                "累计 I/O 不是每秒速率；PIDs 可能包含线程。",
                "操作面板: s 启动 / t 停止 / r 重启 / p 暂停 / u 恢复（均需确认）。",
            ]))
        if self.docker_help_expanded:
            blocks.extend(self._docker_command_reference())
        return blocks

    def _process_order(self, items):
        by_memory = self.process_sort == "memory"
        def rank(item):
            available = item.mem_available if by_memory else item.cpu_available
            value = item.mem_mb if by_memory else item.cpu_pct
            return (not available, -value if available else 0, item.name.casefold(),
                    str(getattr(item, "key", getattr(item, "pid", ""))))
        return sorted(items, key=rank)

    def _process_apps(self):
        if self.data.source_status.get("processes") not in ("ok", "warming", "partial"):
            return []
        return self._process_order(self.data.process_sample.apps)[:self.PROCESS_DISPLAY_LIMIT]

    def _selected_app(self, apps):
        if not apps:
            self.app_selected_key = ""
            return None
        selected = next((app for app in apps if app.key == self.app_selected_key), apps[0])
        self.app_selected_key = selected.key
        return selected

    @staticmethod
    def _app_metric(app, memory=False):
        available = app.mem_available if memory else app.cpu_available
        complete = app.mem_complete if memory else app.cpu_complete
        if not available:
            return "--"
        value = app.mem_mb if memory else app.cpu_pct
        return ("" if complete else "≥") + f"{value:.1f}" + (" MiB" if memory else "%")

    def _process_rows(self, processes):
        rows = []
        for process in self._process_order(processes)[:self.PROCESS_DISPLAY_LIMIT]:
            cpu = f"{process.cpu_pct:.1f}%" if process.cpu_available else "--"
            memory = f"{process.mem_mb:.1f}" if process.mem_available else "--"
            pct = f"{process.mem_pct:.1f}%" if process.mem_available and self.data.sys_info.ram_gb > 0 else "--"
            rows.append(f"{process.pid:7d} {cpu:>7} {pct:>7} {memory:>9}  {process.name}")
        return rows

    def _process_coverage(self):
        sample = self.data.process_sample
        if sample.memory_kind == "rss":
            return ""
        missing = sample.enumerated_count - sample.readable_count
        if missing > 0:
            return "部分进程本轮未读取"
        return ""

    def _process_blocks(self):
        sample = self.data.process_sample
        order = "内存" if self.process_sort == "memory" else "CPU"
        if self.process_view == "processes":
            kind = "内存" if sample.memory_kind == "footprint" else "RSS"
            rows = [f"    PID    CPU%    MEM%  {kind}(MiB)  进程"]
            if self.data.source_status.get("processes") in ("ok", "warming", "partial"):
                rows += self._process_rows(self.data.processes)
            else:
                rows += ["等待进程数据"]
            coverage = self._process_coverage()
            if coverage:
                rows.append(coverage)
            return [(f"进程 | {order}降序 · 前 15", self.COLOR_HEADER, rows)]

        apps = self._process_apps()
        selected = self._selected_app(apps)
        rows = [" CPU%（整机）   内存（MiB）  进程数  应用"]
        for app in apps:
            mark = "▶" if selected is not None and app.key == selected.key else " "
            rows.append(f"{mark} {self._app_metric(app):>9} {self._app_metric(app, True):>13}"
                        f" {len(app.processes):6d}  {app.name}")
        if not apps:
            if sample.memory_kind == "rss":
                rows.append("应用聚合暂不可用，按 v 查看 RSS 进程列表。")
            elif self.data.source_status.get("processes") not in ("ok", "warming", "partial"):
                rows.append("等待有效应用样本")
            else:
                rows.append("未识别到应用；按 v 查看系统服务与独立进程。")
        blocks = [(f"应用 | {order}降序 · 前 15", self.COLOR_CHART, rows)]
        if self.app_expanded and selected is not None:
            members = [f"位置: {selected.path}",
                       f"成员（{order}降序，前 {min(15, len(selected.processes))}/{len(selected.processes)}）",
                       "    PID    CPU%    MEM%  内存(MiB)  进程"]
            members += self._process_rows(selected.processes)
            blocks.append(("应用详情 / " + selected.name, self.COLOR_GOOD, members))
        if any((app.cpu_available and not app.cpu_complete) or
               (app.mem_available and not app.mem_complete) for app in apps):
            rows.append("≥ 为当前可读成员的合计")
        coverage = self._process_coverage()
        if coverage:
            rows.append(coverage)
        return blocks

    def _lines(self):
        d = self.data
        if self.page == self.PAGE_CPU:
            return self._cpu_lines()
        if self.page == self.PAGE_GPU:
            return self._gpu_lines()
        if self.page == self.PAGE_MEMORY:
            return self._memory_lines() + [""] + self._disk_lines()
        if self.page == self.PAGE_DOCKER:
            return [line for title, _, lines in self._docker_blocks() for line in [title] + lines]
        if self.page == self.PAGE_PROCESSES:
            return [line for title, _, rows in self._process_blocks() for line in [title] + rows]
        if self.page == self.PAGE_SENSORS:
            lines = [
                "传感器",
                "CPU 已识别探头平均: " + self._temperature(d.cpu.temp_c),
                "GPU 已识别探头平均: " + self._temperature(d.gpu.temp_c),
                "",
                "风扇转速",
            ]
            lines += [f"{name}: {rpm:.0f} RPM" for name, rpm in d.thermal.fans] or ["未读取到风扇数据（不据此判定是否有风扇）"]
            lines += ["", "温度探头"]
            lines += [f"{name}: {temperature:.1f}°C" for name, temperature in d.thermal.sensors] or ["此机型暂无可识别的温度探头"]
            return lines + ["", "测点编号不对应逻辑核心 ID；测点平均不等于最高核心温度。"]
        pm = getattr(d, "pm_metrics", set())
        power = f"{d.cpu.power_w + d.gpu.power_w + d.ane.power_w:.2f} W" if {"cpu_power", "gpu_power", "ane_power"} <= pm else "--"
        uptime = f"{int(d.uptime_s // 86400)}天 {int(d.uptime_s % 86400 // 3600)}时" if self._ok("uptime") else "--"
        # Keep the main metrics together on a normal SSH terminal; detail lives on other pages.
        fans = " / ".join(f"{name} {rpm:.0f} RPM" for name, rpm in d.thermal.fans) or "--"
        return [
            "CPU | 使用率 " + self._value("cpu", d.cpu.user + d.cpu.sys, "%") + "  测点平均温度 " + self._temperature(d.cpu.temp_c),
            "      功率 " + self._pm("cpu_power", d.cpu.power_w, " W") + "  1分钟负载 " + self._value("cpu", d.cpu.load_1m, "", 2),
            "GPU | 活跃驻留率 " + self._pm("gpu_usage", d.gpu.usage_pct, "%") + "  测点平均温度 " + self._temperature(d.gpu.temp_c),
            "      功率 " + self._pm("gpu_power", d.gpu.power_w, " W") + "  活跃频率 " + self._pm("gpu_freq", d.gpu.freq_mhz, " MHz", 0),
            "",
            "内存 | 占用估算 " + self._value("memory", d.mem.used_gb, " GiB") + " / " + (f"{d.mem.total_gb:.1f} GiB" if d.mem.total_gb > 0 else "--"),
            "       压力 " + (d.mem.pressure_level if self._ok("memory_pressure") else "--") + "  Swap " + self._value("swap", d.mem.swap_used_gb, " GiB", 2),
            "",
            "网络 | " + (getattr(d.net, "interface", "") or "接口未知"),
            "       下载 " + self._value("network", d.net.in_mb_s, " MiB/s") + "  上传 " + self._value("network", d.net.out_mb_s, " MiB/s"),
            "磁盘 | 读 " + self._value("disk", d.disk.read_mb_s, " MiB/s") + "  写 " + self._value("disk", d.disk.write_mb_s, " MiB/s"),
            "启动磁盘共享空间 | 已用 " + self._value("disk_capacity", d.disk.root_used_gb, " GiB") + "  可用 " + self._value("disk_capacity", d.disk.root_avail_gb, " GiB"),
            "",
            "CPU + GPU + ANE 功率: " + power + "（非整机功耗）",
            "风扇 | " + fans,
            "热压力: " + (d.thermal.thermal_pressure if self._ok("thermal") else "--") + f"  已识别温度测点: {len(d.thermal.sensors)}",
            "开机时间: " + uptime,
        ]

    def _meter(self, value, available, width=16):
        if not available:
            return "使用率  --"
        filled = round(width * max(0, min(value, 100)) / 100)
        return "█" * filled + "░" * (width - filled) + f"  {value:.1f}%"

    def _blocks(self):
        """Semantic panels: layout changes with width, metric meaning never changes."""
        d = self.data
        pm = d.pm_metrics
        cpu_pct = d.cpu.user + d.cpu.sys
        mem_pct = d.mem.used_gb / d.mem.total_gb * 100 if d.mem.total_gb else 0
        if self.page == self.PAGE_OVERVIEW:
            power = (f"{d.cpu.power_w + d.gpu.power_w + d.ane.power_w:.2f} W"
                     if {"cpu_power", "gpu_power", "ane_power"} <= pm else "--")
            blocks = [
                ("CPU", self.COLOR_CHART, [
                    self._meter(cpu_pct, self._ok("cpu")),
                    "温度(测点平均) " + self._temperature(d.cpu.temp_c),
                    "功率 " + self._pm("cpu_power", d.cpu.power_w, " W") +
                    "  负载 " + self._value("cpu", d.cpu.load_1m, "", 2),
                    self._trend(d.cpu_history, "%") if self._ok("cpu") else "趋势: 等待采样",
                ]),
                ("GPU", self.COLOR_GPU, [
                    "活跃驻留率 " + self._pm("gpu_usage", d.gpu.usage_pct, "%"),
                    "温度(测点平均) " + self._temperature(d.gpu.temp_c),
                    "功率 " + self._pm("gpu_power", d.gpu.power_w, " W") +
                    "  频率 " + self._pm("gpu_freq", d.gpu.freq_mhz, " MHz", 0),
                    self._trend(d.gpu_temp_history, "°C") if d.gpu.temp_c > 0 else "趋势: 等待采样",
                ]),
                ("内存", self.COLOR_GOOD, [
                    self._meter(mem_pct, self._ok("memory")),
                    "占用估算 " + self._value("memory", d.mem.used_gb, " GiB") +
                    " / " + (f"{d.mem.total_gb:.0f} GiB" if d.mem.total_gb else "--"),
                    "压力 " + (d.mem.pressure_level if self._ok("memory_pressure") else "--") +
                    "  Swap " + self._value("swap", d.mem.swap_used_gb, " GiB", 2),
                ]),
                ("网络", self.COLOR_CHART, [
                    "接口 " + (d.net.interface or "--"),
                    "↓ 下载 " + self._value("network", d.net.in_mb_s, " MiB/s", 2),
                    "↑ 上传 " + self._value("network", d.net.out_mb_s, " MiB/s", 2),
                ]),
                ("磁盘", self.COLOR_HEADER, [
                    "读 " + self._value("disk", d.disk.read_mb_s, " MiB/s", 2) +
                    "  写 " + self._value("disk", d.disk.write_mb_s, " MiB/s", 2),
                    "共享占用 " + self._value("disk_capacity", d.disk.root_used_gb, " GiB"),
                    "可用 " + self._value("disk_capacity", d.disk.root_avail_gb, " GiB"),
                ]),
                ("系统 / 散热", self.COLOR_ACCENT, [
                    "CPU+GPU+ANE " + power + "（非整机）",
                    "风扇 | " + (" / ".join(f"{name} {rpm:.0f} RPM" for name, rpm in d.thermal.fans) or "--"),
                    "热压力 " + (d.thermal.thermal_pressure if self._ok("thermal") else "--"),
                    "开机时间: " + (f"{int(d.uptime_s // 86400)}天 {int(d.uptime_s % 86400 // 3600)}时" if self._ok("uptime") else "--"),
                ]),
            ]
            docker_summary = self._docker_summary()
            if docker_summary is not None:
                blocks.append(("Docker", self.COLOR_CHART, docker_summary))
            return blocks
        if self.page == self.PAGE_CPU:
            lines = self._cpu_lines()
            return [("CPU / 实时负载", self.COLOR_CHART,
                     [self._meter(cpu_pct, self._ok("cpu"))] + lines[2:6]),
                    ("核心 / 集群", self.COLOR_HEADER, [s for s in lines[6:] if not s.startswith("趋势")]),
                    ("使用率趋势", self.COLOR_CHART, [self._trend(d.cpu_history, "%")] if self._ok("cpu") else ["等待有效采样"]),
                    ("温度趋势", self.COLOR_TEMP, [self._trend(d.cpu_temp_history, "°C")] if d.cpu.temp_c > 0 else ["温度: --"])]
        if self.page == self.PAGE_GPU:
            return [("GPU / 实时状态", self.COLOR_GPU, self._gpu_lines()[1:6]),
                    ("温度趋势", self.COLOR_TEMP, [self._trend(d.gpu_temp_history, "°C")] if d.gpu.temp_c > 0 else ["温度: --"]),
                    ("功率趋势", self.COLOR_GPU, [self._trend(d.gpu_power_history, "W")] if "gpu_power" in pm else ["功率: --"]),
                    ("指标说明", self.COLOR_DIM, self._gpu_lines()[6:8])]
        if self.page == self.PAGE_MEMORY:
            lines = self._memory_lines()
            return [("内存 / 占用估算", self.COLOR_GOOD,
                     [self._meter(mem_pct, self._ok("memory"))] + lines[1:5]),
                    ("缓存 / 压力", self.COLOR_HEADER, lines[5:10]),
                    ("启动磁盘 / 共享空间", self.COLOR_CHART, self._disk_lines()[1:]),
                    ("指标说明", self.COLOR_DIM, [lines[0], lines[-1]])]
        if self.page == self.PAGE_PROCESSES:
            return self._process_blocks()
        if self.page == self.PAGE_DOCKER:
            return self._docker_blocks()
        groups = [
            ("CPU 温度测点", self.COLOR_CHART, [(n, v) for n, v in d.thermal.sensors if n.startswith("CPU ")]),
            ("GPU 温度测点", self.COLOR_GPU, [(n, v) for n, v in d.thermal.sensors if n.startswith("GPU ")]),
            ("机身 / 外围温度", self.COLOR_HEADER, [(n, v) for n, v in d.thermal.sensors if not n.startswith(("CPU ", "GPU "))]),
        ]
        blocks = [(title, color, [f"{name}: {value:.1f}°C" for name, value in values] or ["暂无可识别读数"])
                  for title, color, values in groups]
        blocks.insert(0, ("温度 / 散热概览", self.COLOR_TEMP, [
            "CPU 测点平均: " + self._temperature(d.cpu.temp_c),
            "GPU 测点平均: " + self._temperature(d.gpu.temp_c)] +
            [f"{name}: {rpm:.0f} RPM" for name, rpm in d.thermal.fans]))
        blocks.append(("读数说明", self.COLOR_DIM, ["测点编号不对应逻辑核心 ID；测点平均不等于最高核心温度。",
                                                     "未读到风扇数据，不代表该机型没有风扇。" ]))
        return blocks

    def _panel_rows(self, title, color, lines, width):
        """One panel's styled rows, clipped to its own column before composition."""
        rows = [(row, color, True) for row in self._wrap("━━ " + title + " ━━", width)]
        rows.append(("─" * width, self.COLOR_DIM, False))
        for line in lines:
            tone, bold = self.COLOR_VALUE, False
            if "趋势" in line or "█" in line or "░" in line:
                tone = color
            if "温度" in line or "°C" in line:
                tone = self.COLOR_TEMP
            if "--" in line or "说明" in title or "等待" in line:
                tone = self.COLOR_DIM
            if any(marker in line for marker in ("Warning", "Critical", "Heavy", "健康检查失败", "健康检查: 检查失败")):
                tone, bold = self.COLOR_WARNING, True
            rows.extend((part, tone, bold) for part in self._wrap(line, width))
        return rows

    def _layout(self, width):
        """Compose virtual rows; scroll once after arranging whole panels."""
        columns = 2 if width >= 88 and self.page not in (self.PAGE_PROCESSES, self.PAGE_DOCKER) else 1
        panel_width = (width - 5) // 2 if columns == 2 else width - 2
        blocks = self._blocks()
        rows = []
        for index in range(0, len(blocks), columns):
            panels = [self._panel_rows(*block, panel_width) for block in blocks[index:index + columns]]
            for row in range(max(map(len, panels))):
                spans = []
                for col, panel in enumerate(panels):
                    if row < len(panel):
                        text, color, bold = panel[row]
                        spans.append((1 + col * (panel_width + 3), text, color, bold))
                rows.append(spans)
            if index + columns < len(blocks):
                rows.append([])
        return rows

    def render(self):
        try:
            self.stdscr.erase()
            h, w = self.stdscr.getmaxyx()
            if getattr(self, "_docker_dialog", None) is not None:
                self._render_docker_dialog()
                return
            if h < 5 or w < 24:
                self._set(0, 0, "终端过小", self.COLOR_WARNING)
                if h > 2:
                    self._set(1, 0, "请放大到至少 24x5")
                    self._set(h - 1, 0, "q 退出")
                self.stdscr.refresh()
                return
            if not hasattr(self, "_scroll"):
                self._scroll = {}
            info = self.data.sys_info
            paused = " [已暂停]" if self.paused else ""
            self._set(0, 0, f"MacVitals | {self.PAGE_NAMES[self.page]}{paused} | {info.chip}", self.COLOR_TITLE, True)
            self._set(1, 0, "功率采集: " + getattr(self.data, "pm_status", "未启用"), self.COLOR_DIM)
            dev_line = self._developer_sources()
            if dev_line:
                self._set(2, 0, dev_line, self.COLOR_DIM)
            lines = self._layout(w)
            top_rows = 5 if dev_line else 4
            capacity = h - top_rows
            offset = self.sensor_scroll if self.page == self.PAGE_SENSORS else self._scroll.get(self.page, 0)
            offset = min(max(0, offset), max(0, len(lines) - capacity))
            self._scroll[self.page] = offset
            if self.page == self.PAGE_SENSORS:
                self.sensor_scroll = offset
            for row, spans in enumerate(lines[offset:offset + capacity], top_rows - 2):
                for x, text, color, bold in spans:
                    self._set(row, x, text, color, bold)
            self._set(h - 2, 0, f"{offset + 1}-{min(offset + capacity, len(lines))}/{len(lines)} | ↑↓/j k 滚动 | 1总览 2CPU 3GPU 4内存 5应用/进程 6传感器 7Docker", self.COLOR_DIM)
            help_text = "q退出 p暂停 ←→切页 | PgUp/PgDn翻页"
            if self.page == self.PAGE_PROCESSES:
                if self.process_view == "apps":
                    help_text = "v进程  c/m排序  n/b选择  Enter展开  Esc收起  p暂停  q退出"
                else:
                    help_text = "v应用  c/m排序  p暂停  q退出"
            elif self.page == self.PAGE_DOCKER:
                help_text += " | c CPU / m 内存"
                help_text += " | g分组 n/b详情 o操作"
            self._set(h - 1, 0, help_text, self.COLOR_DIM)
            self.stdscr.refresh()
        except curses.error:
            pass


# ──────────────────────────── 主程序 ────────────────────────────

_args = None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="macvitals",
        description="macvitals — Real-time macOS system monitor for Apple Silicon"
    )
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds (0.5–10, default: 1.0)"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}"
    )
    parser.add_argument("--diagnose", action="store_true",
                        help="Collect two samples and print JSON without opening the TUI")
    parser.add_argument("--ioreport-probe", action="store_true",
                        help="Run a read-only IOReport two-sample probe and print JSON")
    parser.add_argument("--compare-probes", action="store_true",
                        help="Run a root-only powermetrics/IOReport comparison and print JSON")
    parser.add_argument("--dev", action="store_true",
                        help="Development UI: show live data sources and freshness (no history is stored)")
    docker_options = parser.add_mutually_exclusive_group()
    docker_options.add_argument("--no-docker", action="store_true",
                                help="Disable optional Docker monitoring")
    docker_options.add_argument("--docker-context", metavar="NAME",
                                help="Observe a named local Docker context (Unix socket only)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or not 0.5 <= args.interval <= 10:
        parser.error("--interval must be finite and between 0.5 and 10 seconds")
    if args.docker_context is not None and (
            not args.docker_context.strip() or args.docker_context.startswith("-") or
            len(args.docker_context) > 256 or not args.docker_context.isprintable()):
        parser.error("--docker-context must be a valid nonempty context name")
    return args


def run_curses(stdscr: curses.window):
    """Main curses UI loop"""
    interval = _args.interval if hasattr(_args, 'interval') else 1.0
    interval = max(0.5, min(interval, 10.0))

    # 初始化 curses
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    stdscr.nodelay(1)
    # UI 刷新间隔：快速轮询按键，不阻塞
    stdscr.timeout(100)  # 100ms 检查一次按键

    # 初始化数据采集器（后台线程采集，不阻塞 UI）
    collector = DataCollector(interval=interval, developer_mode=getattr(_args, "dev", False),
                              docker_enabled=not getattr(_args, "no_docker", False),
                              docker_context=getattr(_args, "docker_context", None))
    actions = DockerActions(context=getattr(_args, "docker_context", None))
    try:
        collector.start()
        ui = MonitorUI(stdscr, collector.snapshot(), docker_actions=actions)
        last_render = 0
        while True:
            # 处理按键
            key = stdscr.getch()
            if key != -1:
                if not ui.handle_key(key):
                    break
                collector.set_paused(ui.paused)
                # 按键后立即刷新，让页面切换和暂停状态立刻可见。
                if not ui.paused:
                    ui.data = collector.snapshot()
                ui.render()
                last_render = time.monotonic()

            # 渲染（按用户设定间隔，不阻塞）
            now = time.monotonic()
            if now - last_render >= interval:
                if not ui.paused:
                    ui.data = collector.snapshot()
                ui.render()
                last_render = now

    except KeyboardInterrupt:
        pass
    finally:
        if ui.docker_readouts is not None:
            ui.docker_readouts.stop()
        actions.stop()
        collector.stop_powermetrics()


def diagnose(interval, developer_mode=False, docker_enabled=True, docker_context=None):
    collector = DataCollector(interval, developer_mode=developer_mode,
                              docker_enabled=docker_enabled, docker_context=docker_context)
    try:
        collector.start_powermetrics()
        collector._docker.start()
        collector.collect_all()
        # Startup overhead plus two headers can exceed 2.2 intervals. Wait for
        # an actual complete frame, with a deadline for unsupported/broken tools.
        time.sleep(interval)
        if collector.data.has_sudo:
            deadline = time.monotonic() + max(5.0, interval * 3)
            while not collector._pm_last_update and time.monotonic() < deadline:
                if collector._pm_proc is None or collector._pm_proc.poll() is not None:
                    break
                time.sleep(0.05)
        collector.collect_all()
        deadline = time.monotonic() + 5.0
        while collector._docker.snapshot().status == "checking" and time.monotonic() < deadline:
            time.sleep(0.05)
        data = collector.snapshot()
        report = {
            "version": __version__, "platform": platform.platform(),
            "system": asdict(data.sys_info), "source_status": data.source_status,
            "source_meta": data.source_meta,
            "powermetrics": {"status": data.pm_status, "fields": sorted(data.pm_metrics),
                             "error": getattr(collector, "_pm_error", "")},
            "cpu": asdict(data.cpu), "gpu": asdict(data.gpu), "ane": asdict(data.ane),
            "memory": asdict(data.mem), "disk": asdict(data.disk),
            "network": asdict(data.net), "thermal": asdict(data.thermal),
            "process_count": len(data.processes),
            "process_sampling": {
                "status": data.process_sample.status,
                "memory_kind": data.process_sample.memory_kind,
                "enumerated_count": data.process_sample.enumerated_count,
                "readable_count": data.process_sample.readable_count,
                "denied_count": data.process_sample.denied_count,
                "exited_count": data.process_sample.exited_count,
                "error_count": data.process_sample.error_count,
                "path_unavailable_count": data.process_sample.path_unavailable_count,
                "app_count": len(data.process_sample.apps),
                "responsibility_available": data.process_sample.responsibility_available,
                "responsibility_attributed_count": sum(
                    p.app_attribution == "responsibility" for p in data.processes),
                "collection_seconds": data.process_sample.collection_seconds,
            },
            "sampled_at": data.sampled_at, "collection_seconds": data.collection_seconds,
            "developer_mode": data.developer_mode,
            "docker": {
                "status": data.docker.status, "message": data.docker.message,
                "engine": data.docker.engine,
                "engine_cpus": data.docker.engine_cpus,
                "engine_memory_bytes": data.docker.engine_memory_bytes,
                "container_count": len(data.docker.containers) if data.docker.status == "ok" else None,
                "running_count": sum(c.state == "running" for c in data.docker.containers)
                                 if data.docker.status == "ok" else None,
                "collection_seconds": data.docker.collection_seconds,
            },
            "notes": ["Unavailable measurements are null; source_status describes each collector.",
                      "Process/container names, images, ports and command lines are omitted from this diagnostic report."],
        }
        # Diagnostic consumers must not mistake unavailable measurements for zeros.
        for domain, keys, source in (
            ("cpu", ("user", "sys", "idle", "load_1m", "load_5m", "load_15m"), "cpu"),
            ("memory", ("total_gb", "used_gb", "cached_gb", "free_gb", "compressed_gb",
                        "wired_gb", "app_memory_gb", "available_gb"), "memory"),
            ("memory", ("swap_used_gb", "swap_total_gb"), "swap"),
            ("memory", ("pressure_level",), "memory_pressure"),
            ("disk", ("read_mb_s", "write_mb_s", "read_ops", "write_ops"), "disk"),
            ("disk", ("root_total_gb", "root_used_gb", "root_avail_gb", "root_pct"), "disk_capacity"),
            ("network", ("in_mb_s", "out_mb_s", "in_packets", "out_packets"), "network"),
            ("thermal", ("thermal_pressure",), "thermal"),
        ):
            if data.source_status.get(source) != "ok":
                for key in keys:
                    report[domain][key] = None
        for domain, key, metric in (
            ("cpu", "power_w", "cpu_power"), ("cpu", "p_freq_mhz", "p_freq"),
            ("cpu", "e_freq_mhz", "e_freq"), ("cpu", "cluster_freqs", "cluster_freqs"),
            ("gpu", "power_w", "gpu_power"), ("gpu", "freq_mhz", "gpu_freq"),
            ("gpu", "usage_pct", "gpu_usage"), ("gpu", "active", "gpu_usage"),
            ("ane", "power_w", "ane_power"),
        ):
            if metric not in data.pm_metrics:
                report[domain][key] = None
        for domain in ("cpu", "gpu"):
            if report[domain]["temp_c"] <= 0:
                report[domain]["temp_c"] = None
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    finally:
        collector.stop_powermetrics()


def entry_point():
    """pip install entry point"""
    global _args
    _args = parse_args()
    if sys.platform != "darwin":
        raise SystemExit("MacVitals requires macOS.")
    if _args.ioreport_probe:
        try:
            from .ioreport_probe import main as ioreport_main
        except ImportError:
            from ioreport_probe import main as ioreport_main
        raise SystemExit(ioreport_main([str(_args.interval)]))
    if _args.compare_probes:
        try:
            from .compare_probe import main as compare_main
        except ImportError:
            from compare_probe import main as compare_main
        raise SystemExit(compare_main([str(_args.interval)]))
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    if _args.diagnose:
        diagnose(_args.interval, developer_mode=_args.dev,
                 docker_enabled=not _args.no_docker, docker_context=_args.docker_context)
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("请在交互终端运行；SSH 请使用 ssh -t。无终端诊断请加 --diagnose。")
    try:
        curses.wrapper(run_curses)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        raise SystemExit(f"终端不支持当前显示模式: {exc}。可使用 --diagnose。")


if __name__ == "__main__":
    entry_point()
