"""Bounded live load validation for the current Mac.

Run from the project root with sudo. The script keeps all samples in memory,
prints a sanitized summary, and never writes a history file or process names.
"""

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from macvitals.macvitals import DataCollector


def _busy_worker(deadline):
    value = 1
    while time.monotonic() < deadline:
        value = (value * 33 + 17) % 1000003
    return value


class WorkerSet:
    def __init__(self, count, duration):
        deadline = time.monotonic() + duration
        self.processes = [multiprocessing.Process(target=_busy_worker, args=(deadline,))
                          for _ in range(count)]

    @property
    def pids(self):
        return {process.pid for process in self.processes if process.pid}

    def start(self):
        for process in self.processes:
            process.start()

    def stop(self):
        for process in self.processes:
            if process.is_alive():
                process.terminate()
        for process in self.processes:
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)


def _sample(collector, worker_pids):
    collector.collect_all()
    data = collector.snapshot()
    worker_cpu = [process.cpu_pct for process in data.processes
                  if process.pid in worker_pids and process.cpu_available]
    return {
        "cpu_pct": (data.cpu.user + data.cpu.sys
                    if data.source_status.get("cpu") == "ok" else None),
        "worker_cpu_pct": sum(worker_cpu) if worker_cpu else None,
        "cpu_power_w": data.cpu.power_w if "cpu_power" in data.pm_metrics else None,
        "gpu_power_w": data.gpu.power_w if "gpu_power" in data.pm_metrics else None,
        "gpu_usage_pct": data.gpu.usage_pct if "gpu_usage" in data.pm_metrics else None,
        "cpu_temp_c": data.cpu.temp_c or None,
        "gpu_temp_c": data.gpu.temp_c or None,
        "fans_rpm": [rpm for _name, rpm in data.thermal.fans],
        "pm_fields": sorted(data.pm_metrics),
        "source_status": dict(data.source_status),
    }


def _phase(collector, name, duration, interval, worker_pids=None):
    worker_pids = worker_pids or set()
    samples = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        started = time.monotonic()
        samples.append(_sample(collector, worker_pids))
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(min(remaining, max(0, deadline - time.monotonic())))
    values = [sample["cpu_pct"] for sample in samples if sample["cpu_pct"] is not None]
    worker_values = [sample["worker_cpu_pct"] for sample in samples
                     if sample["worker_cpu_pct"] is not None]
    powers = [sample["cpu_power_w"] for sample in samples
              if sample["cpu_power_w"] is not None]
    temperatures = [sample["cpu_temp_c"] for sample in samples
                    if sample["cpu_temp_c"] is not None]
    return {
        "name": name,
        "sample_count": len(samples),
        "cpu_pct": {
            "min": min(values) if values else None,
            "mean": statistics.fmean(values) if values else None,
            "max": max(values) if values else None,
        },
        "worker_cpu_pct": {
            "mean": statistics.fmean(worker_values) if worker_values else None,
            "max": max(worker_values) if worker_values else None,
        },
        "cpu_power_w": {
            "mean": statistics.fmean(powers) if powers else None,
            "max": max(powers) if powers else None,
        },
        "cpu_temp_c": {
            "first": temperatures[0] if temperatures else None,
            "last": temperatures[-1] if temperatures else None,
            "max": max(temperatures) if temperatures else None,
        },
        "last": samples[-1] if samples else {},
    }


def run(args):
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo for powermetrics load validation")
    collector = DataCollector(interval=args.interval)
    report = {
        "validation": "bounded-load",
        "version": getattr(__import__("macvitals.macvitals", fromlist=["__version__"]), "__version__"),
        "interval_s": args.interval,
        "phase_duration_s": args.duration,
        "logical_cores": collector.data.sys_info.total_cores,
        "multi_workers": args.multi_workers,
        "notes": [
            "Baseline is allowed to be non-zero; checks use changes and recovery.",
            "All samples stay in memory and process names are omitted.",
        ],
        "phases": [],
    }
    try:
        collector.start_powermetrics()
        time.sleep(max(1.5, args.interval * 3))
        report["phases"].append(_phase(collector, "baseline", args.duration, args.interval))

        single = WorkerSet(1, args.duration + 2)
        single.start()
        try:
            time.sleep(0.4)
            report["phases"].append(_phase(collector, "single_core", args.duration,
                                            args.interval, single.pids))
        finally:
            single.stop()

        multi = WorkerSet(args.multi_workers, args.duration + 2)
        multi.start()
        try:
            time.sleep(0.4)
            report["phases"].append(_phase(collector, "multi_core", args.duration,
                                            args.interval, multi.pids))
        finally:
            multi.stop()

        report["phases"].append(_phase(collector, "recovery", args.duration,
                                        args.interval))
        baseline, single_phase, multi_phase, recovery = report["phases"]
        baseline_mean = baseline["cpu_pct"]["mean"]
        multi_peak = multi_phase["cpu_pct"]["max"]
        recovery_mean = recovery["cpu_pct"]["mean"]
        expected_single = 100 / max(1, report["logical_cores"])
        single_max = single_phase["worker_cpu_pct"]["max"]
        report["checks"] = {
            "cpu_values_in_range": all(
                phase["cpu_pct"]["min"] is None or
                (0 <= phase["cpu_pct"]["min"] <= 100 and 0 <= phase["cpu_pct"]["max"] <= 100)
                for phase in report["phases"]
            ),
            "single_worker_matches_one_core_scale": (
                single_max is not None and 0.4 * expected_single <= single_max <= 2.5 * expected_single
            ),
            "multi_core_rises_above_baseline": (
                baseline_mean is not None and multi_peak is not None and multi_peak >= baseline_mean + 25
            ),
            "recovery_moves_below_multi_peak": (
                recovery_mean is not None and multi_peak is not None and recovery_mean < multi_peak - 10
            ),
            "powermetrics_stream_available": bool(
                any("cpu_power" in phase["last"].get("pm_fields", []) for phase in report["phases"])
            ),
        }
        report["references"] = {
            "expected_single_worker_pct": expected_single,
            "baseline_cpu_mean_pct": baseline_mean,
            "multi_core_peak_pct": multi_peak,
            "recovery_cpu_mean_pct": recovery_mean,
        }
        report["passed"] = all(report["checks"].values())
    finally:
        collector.stop_powermetrics()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded MacVitals load validation")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--duration", type=float, default=8.0,
                        help="seconds per phase; bounded and default 8")
    parser.add_argument("--multi-workers", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    if not 0.5 <= args.interval <= 2 or not 3 <= args.duration <= 30:
        parser.error("interval must be 0.5-2 and duration must be 3-30 seconds")
    if not 1 <= args.multi_workers <= (os.cpu_count() or 1):
        parser.error("multi-workers must be within the logical CPU count")
    print(json.dumps(run(args), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
