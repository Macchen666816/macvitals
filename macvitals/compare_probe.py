"""One-shot powermetrics/IOReport comparison for development validation."""

import json
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from queue import Empty, Queue
from typing import Any, Dict, List

try:
    from . import ioreport_probe
    from .macvitals import DataCollector
except ImportError:  # direct `python macvitals/macvitals.py` compatibility
    import ioreport_probe
    from macvitals import DataCollector


def _frames(text: str) -> List[str]:
    chunks = []
    current = []
    for line in text.splitlines(True):
        if line.lstrip().startswith("*** Sample"):
            if current:
                chunks.append("".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _parse_frames(frames: List[str]) -> List[Dict[str, Any]]:
    parsed = []
    for text in frames:
        collector = DataCollector.__new__(DataCollector)
        collector._pm_lock = __import__("threading").Lock()
        collector._pm_data = {}
        collector._pm_last_update = 0.0
        collector._pm_error = ""
        collector._parse_pm_sample(text)
        with collector._pm_lock:
            parsed.append(dict(collector._pm_data))
    return parsed


def _samplers() -> List[str]:
    result = subprocess.run(
        ["powermetrics", "--help"], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=3,
        env={**os.environ, "LC_ALL": "C"},
    )
    supported = set(re.findall(r"^\s+(\w+)\s{2,}", result.stdout, re.MULTILINE))
    return [name for name in ("cpu_power", "gpu_power", "ane_power", "thermal")
            if name in supported]


class _IOSession:
    """Small live IOReport session used only by the synchronized comparison."""

    def __init__(self):
        self.cf, self.report = ioreport_probe._load_frameworks()
        self.groups = {}
        for group_name, subgroup_name, key in ioreport_probe.GROUPS:
            group_ref = ioreport_probe._cf_string(self.cf, group_name)
            subgroup_ref = (ioreport_probe._cf_string(self.cf, subgroup_name)
                            if subgroup_name else None)
            channels = self.report.IOReportCopyChannelsInGroup(
                group_ref, subgroup_ref, 0, 0, 0
            )
            if channels:
                sub_ref = ctypes.c_void_p()
                subscription = self.report.IOReportCreateSubscription(
                    None, channels, ctypes.byref(sub_ref), 0, None
                )
                if sub_ref.value:
                    self.cf.CFRelease(sub_ref.value)
                if subscription:
                    self.groups[key] = (channels, subscription)
                else:
                    self.cf.CFRelease(channels)
            if subgroup_ref:
                self.cf.CFRelease(subgroup_ref)
            self.cf.CFRelease(group_ref)

    def sample(self):
        result = {}
        for key, (channels, subscription) in self.groups.items():
            sample = self.report.IOReportCreateSamples(subscription, channels, None)
            if sample:
                result[key] = ioreport_probe._channel_records(self.cf, self.report, sample)
                self.cf.CFRelease(sample)
        return result

    def close(self):
        for channels, _subscription in self.groups.values():
            self.cf.CFRelease(channels)
        self.groups.clear()


def _delta_records(previous, current):
    old = {record.get("channel"): record for record in previous}
    result = []
    for record in current:
        before = old.get(record.get("channel"), {})
        states = []
        old_states = {state.get("name"): state.get("residency", 0)
                      for state in before.get("states", [])}
        for state in record.get("states", []):
            value = state.get("residency")
            if isinstance(value, int):
                states.append({"name": state.get("name", ""),
                               "residency": max(0, value - old_states.get(state.get("name", ""), 0))})
        value = record.get("value")
        old_value = before.get("value")
        result.append({"channel": record.get("channel", ""),
                       "unit": record.get("unit", ""),
                       "states": states,
                       "value": (max(0, value - old_value)
                                 if isinstance(value, int) and isinstance(old_value, int)
                                 else None)})
    return result


def _frame_reader(stream, queue):
    current = []
    try:
        for line in stream:
            if line.lstrip().startswith("*** Sample"):
                if current:
                    queue.put((time.monotonic(), "".join(current)))
                current = [line]
            elif current:
                current.append(line)
        if current:
            queue.put((time.monotonic(), "".join(current)))
    finally:
        try:
            stream.close()
        except Exception:
            pass


def compare(interval: float = 1.0) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "probe": "powermetrics-ioreport-compare",
        "interval_s": interval,
        "requires_root": True,
        "notes": [
            "This compares definitions and windows, not Activity Monitor values.",
            "IOReport state names require a platform frequency table before becoming MHz.",
            "No history is stored.",
        ],
    }
    if os.geteuid() != 0:
        result["status"] = "unavailable"
        result["error"] = "powermetrics comparison requires root; run with sudo"
        return result

    pm = None
    io_session = None
    reader = None
    try:
        samplers = _samplers()
        if not samplers:
            raise RuntimeError("no supported powermetrics samplers")
        pm = subprocess.Popen(
            ["powermetrics", "--samplers", ",".join(samplers),
             "-i", str(max(500, int(interval * 1000))), "-n", "3",
             "--format", "text", "--buffer-size", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", env={**os.environ, "LC_ALL": "C"},
        )
        frame_queue = Queue()
        reader = threading.Thread(target=_frame_reader, args=(pm.stdout, frame_queue), daemon=True)
        reader.start()
        io_session = _IOSession()
        first_pm_time, first_pm_text = frame_queue.get(timeout=max(10.0, interval * 8))
        io_first_time = time.monotonic()
        io_first = io_session.sample()
        time.sleep(interval)
        io_second_time = time.monotonic()
        io_second = io_session.sample()
        second_pm_time, second_pm_text = frame_queue.get(timeout=max(10.0, interval * 8))
        io_session.close()
        io_session = None
        pm.terminate()
        try:
            pm.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pm.kill()
            pm.wait(timeout=3)
        reader.join(timeout=3)
        reader = None
        frames = _parse_frames([first_pm_text, second_pm_text])
        elapsed = max(0.001, io_second_time - io_first_time)
        result.update({"status": "ok", "samplers": samplers,
                       "powermetrics_frame_count": 2,
                       "powermetrics_frame_window_s": second_pm_time - first_pm_time,
                       "ioreport_window_s": elapsed,
                       "powermetrics": {"first": frames[0], "last": frames[1]}})
        io_cpu_delta = _delta_records(io_first.get("cpu_complex_states", []),
                                      io_second.get("cpu_complex_states", []))
        io_energy_delta = _delta_records(io_first.get("energy_model", []),
                                         io_second.get("energy_model", []))
        state_totals = ioreport_probe._state_totals(io_cpu_delta)
        result["comparison"] = {
            "powermetrics_cluster_freqs": frames[1].get("cluster_freqs", {}),
            "powermetrics_gpu_usage_pct": frames[1].get("gpu_usage"),
            "powermetrics_cpu_power_w": frames[1].get("cpu_power"),
            "powermetrics_gpu_power_w": frames[1].get("gpu_power"),
            "powermetrics_ane_power_w": frames[1].get("ane_power"),
            "io_cpu_state_active_residency_pct": ioreport_probe._active_residency_pct(state_totals),
            "io_energy_power_estimates_w": ioreport_probe._energy_power(io_energy_delta, elapsed),
            "interpretation": "CPU state residency and powermetrics active frequency are related but not numerically interchangeable without the platform frequency table.",
        }
    except Empty:
        result["status"] = "unavailable"
        result["error"] = "powermetrics did not produce two complete frames in time"
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        result["status"] = "unavailable"
        result["error"] = str(exc)
    finally:
        if io_session is not None:
            io_session.close()
        if pm is not None and pm.poll() is None:
            pm.terminate()
            try:
                pm.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pm.kill()
                pm.wait(timeout=3)
        if reader is not None:
            reader.join(timeout=3)
    return result


def main(argv=None) -> int:
    interval = 1.0
    if argv:
        interval = float(argv[0])
    if not 0.5 <= interval <= 5:
        print(json.dumps({"probe": "powermetrics-ioreport-compare",
                          "error": "interval must be between 0.5 and 5 seconds"}, ensure_ascii=False))
        return 2
    print(json.dumps(compare(interval), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
