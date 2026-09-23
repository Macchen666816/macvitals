"""Read-only IOReport capability and two-sample probe.

IOReport is a private/unstable macOS framework. This module deliberately keeps
the probe separate from the production collector and emits only the current
sample plus a delta. It never writes a database or a history file.
"""

import ctypes
import json
import math
import platform
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


CFTypeRef = ctypes.c_void_p
CFIndex = ctypes.c_long


GROUPS = (
    ("CPU Stats", "CPU Core Performance States", "cpu_core_states"),
    ("CPU Stats", "CPU Complex Performance States", "cpu_complex_states"),
    ("Energy Model", None, "energy_model"),
    ("DCP", "swap", "dcp_swap"),
)


def _text(cf, value: Optional[int]) -> str:
    if not value:
        return ""
    buffer = ctypes.create_string_buffer(512)
    if cf.CFStringGetCString(value, buffer, len(buffer), 0x08000100):
        return buffer.value.decode("utf-8", "replace")
    return ""


def _cf_string(cf, value: str) -> int:
    return cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)


def _configure(cf, report):
    cf.CFStringCreateWithCString.argtypes = [CFTypeRef, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = CFTypeRef
    cf.CFStringGetCString.argtypes = [CFTypeRef, ctypes.c_char_p, CFIndex, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFDictionaryGetValue.argtypes = [CFTypeRef, CFTypeRef]
    cf.CFDictionaryGetValue.restype = CFTypeRef
    cf.CFArrayGetCount.argtypes = [CFTypeRef]
    cf.CFArrayGetCount.restype = CFIndex
    cf.CFArrayGetValueAtIndex.argtypes = [CFTypeRef, CFIndex]
    cf.CFArrayGetValueAtIndex.restype = CFTypeRef
    cf.CFRelease.argtypes = [CFTypeRef]
    cf.CFRelease.restype = None

    report.IOReportCopyChannelsInGroup.argtypes = [CFTypeRef, CFTypeRef,
                                                    ctypes.c_uint64, ctypes.c_uint64,
                                                    ctypes.c_uint64]
    report.IOReportCopyChannelsInGroup.restype = CFTypeRef
    report.IOReportCreateSubscription.argtypes = [CFTypeRef, CFTypeRef,
                                                   ctypes.POINTER(CFTypeRef),
                                                   ctypes.c_uint64, CFTypeRef]
    report.IOReportCreateSubscription.restype = CFTypeRef
    report.IOReportCreateSamples.argtypes = [CFTypeRef, CFTypeRef, CFTypeRef]
    report.IOReportCreateSamples.restype = CFTypeRef
    report.IOReportCreateSamplesDelta.argtypes = [CFTypeRef, CFTypeRef, CFTypeRef]
    report.IOReportCreateSamplesDelta.restype = CFTypeRef
    report.IOReportChannelGetGroup.argtypes = [CFTypeRef]
    report.IOReportChannelGetGroup.restype = CFTypeRef
    report.IOReportChannelGetSubGroup.argtypes = [CFTypeRef]
    report.IOReportChannelGetSubGroup.restype = CFTypeRef
    report.IOReportChannelGetChannelName.argtypes = [CFTypeRef]
    report.IOReportChannelGetChannelName.restype = CFTypeRef
    report.IOReportChannelGetUnitLabel.argtypes = [CFTypeRef]
    report.IOReportChannelGetUnitLabel.restype = CFTypeRef
    report.IOReportSimpleGetIntegerValue.argtypes = [CFTypeRef, ctypes.c_int32]
    report.IOReportSimpleGetIntegerValue.restype = ctypes.c_int64
    report.IOReportStateGetCount.argtypes = [CFTypeRef]
    report.IOReportStateGetCount.restype = ctypes.c_int32
    report.IOReportStateGetNameForIndex.argtypes = [CFTypeRef, ctypes.c_int32]
    report.IOReportStateGetNameForIndex.restype = CFTypeRef
    report.IOReportStateGetResidency.argtypes = [CFTypeRef, ctypes.c_int32]
    report.IOReportStateGetResidency.restype = ctypes.c_int64


def _channels_array(cf, dictionary: int) -> int:
    key = _cf_string(cf, "IOReportChannels")
    try:
        return cf.CFDictionaryGetValue(dictionary, key)
    finally:
        if key:
            cf.CFRelease(key)


def _channel_records(cf, report, dictionary: int, limit: int = 80) -> List[Dict[str, Any]]:
    array = _channels_array(cf, dictionary)
    if not array:
        return []
    count = min(int(cf.CFArrayGetCount(array)), limit)
    records = []
    for index in range(count):
        item = cf.CFArrayGetValueAtIndex(array, index)
        record = {
            "group": _text(cf, report.IOReportChannelGetGroup(item)),
            "subgroup": _text(cf, report.IOReportChannelGetSubGroup(item)),
            "channel": _text(cf, report.IOReportChannelGetChannelName(item)),
            "unit": _text(cf, report.IOReportChannelGetUnitLabel(item)),
        }
        states = []
        try:
            state_count = min(max(0, int(report.IOReportStateGetCount(item))), 64)
            for state_index in range(state_count):
                states.append({
                    "name": _text(cf, report.IOReportStateGetNameForIndex(item, state_index)),
                    "residency": int(report.IOReportStateGetResidency(item, state_index)),
                })
        except Exception:
            states = []
        if states:
            record["states"] = states
        try:
            record["value"] = int(report.IOReportSimpleGetIntegerValue(item, 0))
        except Exception:
            pass
        records.append(record)
    return records


def _channel_metadata(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{
        "channel": record.get("channel", ""),
        "unit": record.get("unit", ""),
        "states": [state.get("name", "") for state in record.get("states", [])],
    } for record in records]


def _state_totals(records: List[Dict[str, Any]]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for record in records:
        for state in record.get("states", []):
            name = state.get("name", "")
            value = state.get("residency")
            if name and isinstance(value, int) and value >= 0:
                totals[name] = totals.get(name, 0) + value
    return totals


def _active_residency_pct(totals: Dict[str, int]) -> Optional[float]:
    valid = {name: value for name, value in totals.items() if value >= 0}
    total = sum(valid.values())
    active = sum(value for name, value in valid.items() if name not in ("IDLE", "DOWN", "OFF"))
    return (active * 100.0 / total) if total > 0 else None


def _energy_values(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected = []
    for record in records:
        name = record.get("channel", "")
        if (name.startswith(("ANE", "DRAM", "PCI")) or name.endswith("CPU Energy")
                or name.endswith("GPU Energy")):
            selected.append({"channel": name, "unit": record.get("unit", ""),
                             "value": record.get("value")})
    return selected


def _counter_total(records: List[Dict[str, Any]]) -> int:
    values = [record.get("value") for record in records
              if isinstance(record.get("value"), int) and record["value"] >= 0]
    return sum(values)


def _energy_power(records: List[Dict[str, Any]], elapsed: float) -> List[Dict[str, Any]]:
    if elapsed <= 0:
        return []
    result = []
    for record in _energy_values(records):
        raw = record.get("value")
        unit = str(record.get("unit", "")).strip().lower()
        if not isinstance(raw, int) or raw < 0:
            continue
        scale = {"mj": 1e-3, "uj": 1e-6, "µj": 1e-6, "nj": 1e-9, "pj": 1e-12}.get(unit)
        if scale is None:
            continue
        result.append({"channel": record.get("channel", ""),
                       "power_w": raw * scale / elapsed})
    return result


def _load_frameworks():
    if sys.platform != "darwin":
        raise RuntimeError("IOReport requires macOS")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    errors = []
    report = None
    # macOS releases have exposed the same symbols through different loader
    # paths. Prefer the system dylib used by the installed Stats frameworks;
    # keep the older private-framework path as a compatibility fallback.
    for path in ("/usr/lib/libIOReport.dylib",
                 "/System/Library/PrivateFrameworks/IOReport.framework/IOReport"):
        try:
            report = ctypes.CDLL(path)
            break
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    if report is None:
        raise RuntimeError("; ".join(errors))
    _configure(cf, report)
    return cf, report


def probe(interval: float = 1.0) -> Dict[str, Any]:
    """Collect two short IOReport samples and return a JSON-safe report."""
    report: Dict[str, Any] = {
        "probe": "ioreport",
        "platform": platform.platform(),
        "interval_s": interval,
        "framework": "unavailable",
        "groups": {},
        "notes": [
            "IOReport is a private/unstable macOS framework.",
            "Values are raw counters or residencies; they are not Activity Monitor equivalents.",
            "No history is stored by this probe.",
        ],
    }
    try:
        cf, ioreport = _load_frameworks()
    except Exception as exc:
        report["error"] = str(exc)
        return report

    report["framework"] = "loaded"
    report["probe_started_monotonic"] = time.monotonic()
    active = []
    # Create every subscription before taking the first sample. This keeps all
    # groups inside one comparable measurement window.
    for group_name, subgroup_name, key in GROUPS:
        group_ref = _cf_string(cf, group_name)
        subgroup_ref = _cf_string(cf, subgroup_name) if subgroup_name else None
        channels = None
        try:
            channels = ioreport.IOReportCopyChannelsInGroup(group_ref, subgroup_ref, 0, 0, 0)
            entry: Dict[str, Any] = {
                "group": group_name,
                "subgroup": subgroup_name,
                "available": bool(channels),
            }
            if channels:
                metadata = _channel_records(cf, ioreport, channels)
                entry["channel_count"] = len(metadata)
                entry["channels"] = _channel_metadata(metadata)
                subscription_ref = CFTypeRef()
                subscription = ioreport.IOReportCreateSubscription(
                    None, channels, ctypes.byref(subscription_ref), 0, None
                )
                if subscription_ref.value:
                    cf.CFRelease(subscription_ref.value)
                if subscription:
                    first = ioreport.IOReportCreateSamples(subscription, channels, None)
                    active.append((key, entry, channels, subscription, first))
                else:
                    entry["sampled"] = False
                    cf.CFRelease(channels)
            report["groups"][key] = entry
        except Exception as exc:
            report["groups"][key] = {
                "group": group_name, "subgroup": subgroup_name,
                "available": bool(channels), "error": str(exc),
            }
        finally:
            if subgroup_ref:
                cf.CFRelease(subgroup_ref)
            if group_ref:
                cf.CFRelease(group_ref)

    window_started = time.monotonic()
    time.sleep(interval)
    window_ended = time.monotonic()
    report["sample_window_s"] = window_ended - window_started
    report["probe_ended_monotonic"] = window_ended
    for key, entry, channels, subscription, first in active:
        second = None
        delta = None
        try:
            second = ioreport.IOReportCreateSamples(subscription, channels, None)
            entry["sampled"] = bool(first and second)
            if not (first and second):
                continue
            delta = ioreport.IOReportCreateSamplesDelta(first, second, None)
            first_records = _channel_records(cf, ioreport, first)
            second_records = _channel_records(cf, ioreport, second)
            delta_records = _channel_records(cf, ioreport, delta) if delta else []
            if key in ("cpu_core_states", "cpu_complex_states"):
                state_totals = _state_totals(delta_records)
                entry["delta_state_residency"] = state_totals
                entry["active_residency_pct"] = _active_residency_pct(state_totals)
            elif key == "energy_model":
                entry["energy_channels"] = {
                    "first": _energy_values(first_records),
                    "second": _energy_values(second_records),
                    "delta": _energy_values(delta_records),
                }
                entry["power_estimates_w"] = _energy_power(
                    delta_records, report["sample_window_s"]
                )
            elif key == "dcp_swap":
                entry["counter_total"] = {
                    "first": _counter_total(first_records),
                    "second": _counter_total(second_records),
                    "delta": _counter_total(delta_records),
                }
        except Exception as exc:
            entry["error"] = str(exc)
        finally:
            if first:
                cf.CFRelease(first)
            if second:
                cf.CFRelease(second)
            if delta:
                cf.CFRelease(delta)
            if channels:
                cf.CFRelease(channels)
    return report


def main(argv=None) -> int:
    interval = 1.0
    if argv:
        try:
            interval = float(argv[0])
        except ValueError:
            print(json.dumps({"probe": "ioreport", "error": "invalid interval"}, ensure_ascii=False))
            return 2
    if not math.isfinite(interval) or not 0.2 <= interval <= 5:
        print(json.dumps({"probe": "ioreport", "error": "interval must be between 0.2 and 5 seconds"}, ensure_ascii=False))
        return 2
    print(json.dumps(probe(interval), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
