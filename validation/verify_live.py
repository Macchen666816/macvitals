"""Bounded, read-only live checks. Run from project root with sudo python3.

Only sanitized measurements go to stdout; no credentials or process names.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from macvitals.macvitals import DataCollector


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo for powermetrics verification")
    c = DataCollector()
    report = {}
    diagnostic = json.loads(subprocess.check_output(
        [sys.executable, "-m", "macvitals", "--diagnose"], text=True, timeout=20))
    assert diagnostic["cpu"]["cluster_freqs"]
    assert diagnostic["gpu"]["freq_mhz"] is not None
    report["diagnostic_powermetrics"] = diagnostic["powermetrics"]
    try:
        raw = subprocess.check_output(
            ["powermetrics", "--samplers", "cpu_power,gpu_power,ane_power,thermal",
             "-i", "1000", "-n", "1", "--format", "text"],
            text=True, timeout=15, env={**os.environ, "LC_ALL": "C"})
        fields = {}
        for line in raw.splitlines():
            if ":" in line:
                key, value = line.strip().split(":", 1)
                fields.setdefault(key, []).append(value.strip())
        number = lambda key: float(fields[key][0].split()[0].rstrip("%"))
        expected = {key.split("-Cluster")[0]: number(key) for key in fields
                    if key.endswith("-Cluster HW active frequency")}
        c._parse_pm_sample(raw)
        c._merge_powermetrics()
        assert c.data.cpu.cluster_freqs == expected
        assert c.data.gpu.freq_mhz == number("GPU HW active frequency")
        assert c.data.gpu.usage_pct == number("GPU HW active residency")
        for key, actual in (("CPU Power", c.data.cpu.power_w),
                            ("GPU Power", c.data.gpu.power_w),
                            ("ANE Power", c.data.ane.power_w)):
            assert actual == number(key) / 1000
        assert c.data.thermal.thermal_pressure == fields["Current pressure level"][0]
        report["same_frame"] = {"clusters_mhz": expected,
            "gpu_mhz": c.data.gpu.freq_mhz, "gpu_active_percent": c.data.gpu.usage_pct,
            "cpu_w": c.data.cpu.power_w, "gpu_w": c.data.gpu.power_w,
            "ane_w": c.data.ane.power_w, "gpu_power_raw_occurrences": fields["GPU Power"],
            "thermal": c.data.thermal.thermal_pressure, "matched": True}
    finally:
        c.stop_powermetrics()
    report["stream"] = []
    for interval in (0.5, 1.0):
        c = DataCollector(interval)
        try:
            c.start_powermetrics()
            deadline = time.monotonic() + 8
            updates = set()
            while time.monotonic() < deadline and len(updates) < 3:
                time.sleep(0.1)
                if c._pm_last_update:
                    updates.add(c._pm_last_update)
            c.collect_all()
            assert len(updates) >= 3 and not c._pm_error
            assert {"cluster_freqs", "gpu_freq", "gpu_usage", "cpu_power",
                    "gpu_power", "ane_power", "thermal_pressure"} <= c.data.pm_metrics
            report["stream"].append({"interval": interval, "complete_updates": len(updates),
                                      "fields": sorted(c.data.pm_metrics)})
        finally:
            c.stop_powermetrics()
        assert c._pm_proc.poll() is not None
    c = DataCollector()
    worker = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        c.collect_all()
        time.sleep(2)
        c.collect_all()
        proc = next(p for p in c.data.processes if p.pid == worker.pid)
        report["single_core_worker"] = {"cpu_percent": proc.cpu_pct,
            "full_core_reference_percent": 100 / c.data.sys_info.total_cores}
        assert 1 < proc.cpu_pct < 8, report["single_core_worker"]
        report["source_status"] = c.data.source_status
        report["temperature_means_c"] = {"cpu": c.data.cpu.temp_c, "gpu": c.data.gpu.temp_c}
        report["fans_rpm"] = c.data.thermal.fans
    finally:
        worker.terminate()
        worker.wait(timeout=3)
        c.stop_powermetrics()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
