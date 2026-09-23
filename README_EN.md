# MacVitals

[简体中文](README.md) | [English](README_EN.md)

> SSH-first live system monitor for Apple Silicon Macs.

A terminal-based performance monitor for Apple Silicon Macs. Use it locally or over SSH to inspect CPU, GPU, memory, processes, hardware temperatures, and fan speeds.

## Positioning: Built for Remote Observation over SSH

MacVitals is not a replacement for Stats, Activity Monitor, or other desktop GUI monitors, nor does it try to compete for the same use case. Tools such as Stats are better suited to menu bar monitoring, graphical configuration, persistent operation, and historical trends. MacVitals is designed for something different: when you connect to a Mac at home over SSH, it provides a lightweight, immediate, and auditable terminal interface for quickly understanding the machine's current state.

It maintains the following boundaries:

- No desktop environment or third-party server is required. It is suitable for SSH sessions and temporary use.
- It stores no history, log database, cloud data, or background synchronization. Observation ends when the program exits.
- It keeps a colored, panel-based dashboard and clear page hierarchy without dumping irrelevant raw fields on the user.
- Every metric distinguishes between valid, pending, stale, and unavailable states. Collection failures are never disguised as `0`.
- It prioritizes explaining each metric's source and semantics instead of promising exact agreement with similarly named values in Activity Monitor or Stats.

If you need menu bar icons, graphical settings, automatic launch, or historical charts, a GUI tool such as Stats is the better choice. If you need lightweight, immediate diagnostics over SSH, that is exactly what MacVitals is built for.

MacVitals uses only the Python standard library, curses, and macOS system interfaces, with no third-party Python runtime dependencies. System capabilities and sensor layouts vary by model, so identical metrics cannot be guaranteed across all Apple Silicon devices.

Current version: 1.4.0. Python 3.9 or later is required.

The interface uses a panel-based dashboard: cyan for CPU, purple for GPU, and green for memory, with utilization bars, temperature highlights, and trend graphs. Wide terminals use two columns, narrow terminals use one, and every page supports scrolling.

## Interface Preview

![MacVitals overview](docs/assets/macvitals-overview.png)

<details>
<summary>More views</summary>

### CPU

![MacVitals CPU page](docs/assets/macvitals-cpu.png)

### GPU

![MacVitals GPU page](docs/assets/macvitals-gpu.png)

### Memory

![MacVitals memory page](docs/assets/macvitals-memory.png)

### Sensors

![MacVitals sensors page](docs/assets/macvitals-sensors.png)

</details>

## Running MacVitals

From the project root:

```bash
python3 macvitals/macvitals.py
python3 macvitals/macvitals.py --interval 1
python3 macvitals/macvitals.py --dev       # Development mode: show source and data age
python3 -m macvitals
```

Optional installation with a command-line entry point (a virtual environment is recommended):

```bash
python3 -m pip install .
macvitals
```

Package and editable installations require pip 21.3 or later. Older macOS developer tools may include pip 21.2; in that case, you can still run `python3 macvitals/macvitals.py` directly or update the packaging tools first. For development, use `python3 -m pip install -e .`. The refresh interval can be set from 0.5 to 10 seconds.

Basic metrics such as CPU, memory, and processes generally do not require administrator privileges. Temperatures and fan speeds are read from interfaces such as AppleSMC and do not depend on the powermetrics temperature sampler. Privileged metrics such as CPU/GPU power and frequency require:

```bash
sudo python3 macvitals/macvitals.py
```

When using MacVitals over SSH, run the command on the Mac being monitored. Elevated privileges cannot add support for a sampler that the system does not provide or resolve an unknown sensor mapping.

## Pages and Keyboard Controls

| Key | Action |
|---|---|
| `1-6` | Open Overview, CPU, GPU, Memory, Processes, or Sensors |
| `Left / Right` | Switch pages |
| `Up / Down` or `k / j` | Scroll the current page |
| `PgUp / PgDn`, `Home / End` | Page or jump to the beginning/end |
| `c / m` | Sort the Processes page by CPU or RSS memory in descending order and show the top 15 |
| `p` | Pause/resume monitoring |
| `q` | Quit |

When the terminal is too small, MacVitals displays a size notice. Long pages remain scrollable. The Sensors page uses friendly names only when the mapping is supported by evidence; unknown or unreadable values are never presented as genuine zeros.

Pausing freezes the interface and regular monitoring. The background powermetrics process continues to run.

`--dev` displays source, semantics, and state metadata for the current live snapshot only. It does not write history or logs. Run without this option for the concise production interface.

## Metric Semantics

- **CPU:** Total time across all logical CPUs equals 100%, calculated from differences between cumulative counters. On an 18-core machine, one fully utilized core is approximately 5.6% of the whole machine. Heterogeneous cores are not weighted by actual compute capability.
- **Process CPU:** Calculated from the increase in cumulative CPU time between adjacent samples. The first frame establishes a baseline; it is not a lifetime average since the process started.
- **Process memory:** RSS (resident physical memory), not Activity Monitor's memory footprint. Shared pages may be counted for multiple processes, so values cannot be summed directly.
- **Memory:** Actual system pressure is distinguished from category-based estimates. System pressure cannot be inferred solely from the percentage of free memory.
- **Temperature:** The primary CPU/GPU temperature is the average of the corresponding valid probes, not the maximum junction temperature. Probe support depends on the model and available mappings.
- **Fans:** Reported in RPM. A valid `0 RPM` means the fan is stopped; it is distinct from a fanless machine or an unreadable value.
- **Disk:** APFS capacity uses the container-level view to avoid counting only the read-only system volume.
- **Network:** Rates are calculated from differences between cumulative counters for the selected interface. The default route and VPNs affect interface selection, and a change requires a new baseline.
- **Power and frequency:** Provided by powermetrics and dependent on privileges, system capabilities, and valid sampling. Missing data does not mean the component is idle.
- **GPU and total power:** Active residency is the proportion of time active, while active frequency is the frequency during active work. The sum of CPU, GPU, and ANE power is not whole-system power consumption.
- **Cross-tool comparison:** Similarly named metrics in Stats and Activity Monitor are not guaranteed to use the same semantics. Compare the source, statistical window, and definition before deciding whether values should be close; no single tool is the ground truth for every field.

## Testing

You can obtain diagnostic JSON after two samples without opening the interactive interface:

```bash
python3 -m macvitals --diagnose
python3 -m macvitals --ioreport-probe
sudo python3 -m macvitals --compare-probes
sudo python3 validation/load_test.py
```

Diagnostic output omits process names and command lines while including hardware and collection-capability information. Unavailable values are represented as `null`. For privileged diagnostics, run `sudo python3 -m macvitals --diagnose` in your own terminal and enter the password when prompted.

`--ioreport-probe` is a standalone, read-only development probe. It takes two short samples of CPU performance states, Energy Model data, and DCP swap activity, then emits a differential summary. IOReport is an unstable system interface; probe results are used only to compare definitions and sampling windows with `powermetrics`. They are not connected to the production interface and are not stored as history.

`--compare-probes` requires administrator privileges. During the same short sampling period, it starts `powermetrics` and IOReport and emits a one-time comparison JSON document. CPU residency, active frequency, and power retain their respective definitions and are not forced into Activity Monitor's semantics.

```bash
python3 -m unittest discover -s tests -v
```

All 44 automated tests pass. Coverage includes panel colors and column boundaries, development metadata, IOReport summaries and comparison logic, resizing in real pseudo-terminals, navigation across all six pages, sorting, pause, and exit. The primary review machine was an Apple M5 Pro (Mac17,8, 18 cores, 64 GiB, macOS 26.6.2); this does not mean every model has been tested. Privileged validation also covered four load phases: an approximately 9% baseline, single-core load, multi-core load, and recovery, plus graceful degradation after terminating powermetrics. These checks ran entirely in memory and wrote no history or logs. Version 1.3.2 completed same-frame privileged comparisons and continuous sampling at 0.5- and 1-second intervals, and fixed an issue where diagnostics could end before privileged fields arrived.

Privileged validation results are available in the [hardware validation record](validation/2026-09-21-m5-pro-privileged.json). Reproduce them with `sudo python3 validation/verify_live.py` (this includes approximately two seconds of single-core load). P0/P1/S cluster frequencies, GPU residency, CPU/GPU/ANE power, and thermal pressure matched the corresponding raw output from the same frame. A single-core load represented approximately 5.56% of the whole machine. GPU power uses the CPU/GPU/ANE summary reading; the GPU detail section may differ slightly and is not added again. Temperatures are averages of recognized probes, and fan readings come from SMC. This validation is not a physical calibration of the probes or a power-meter verification.

The unprivileged sample is available as [hardware diagnostic JSON](validation/2026-09-21-m5-pro.json), and release changes are documented in the [changelog](CHANGELOG.md). These files record validation at a particular time; live readings will vary.

Wheel installation and the command-line entry point were verified in a temporary virtual environment. Six-page navigation, terminal resizing, sorting, pause, and exit were also verified in real pseudo-terminals using `xterm-256color`, `xterm`, and `vt100`.

## Compatibility and Contributions

MacVitals primarily targets Apple Silicon. The current code has been tested on multiple Apple Silicon Macs, but different chips, Pro/Max/Ultra variants, fan configurations, and macOS versions may expose different sensor keys, powermetrics fields, and core topologies. Ultra-series Macs have not yet been tested locally, so “the code runs” must not be interpreted as certified Ultra compatibility.

If you find a sensor mapping, core topology, or system-command formatting issue on another Mac, Issues and Pull Requests are welcome. Reports should include the macOS version, chip model, sanitized `--diagnose` output, and reproduction steps. Do not upload sudo passwords, process names, personal paths, or other sensitive information. Hardware-mapping changes must include supporting evidence and preserve unknown-value semantics; every change will be reviewed before merging.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines. For security issues, read [SECURITY.md](SECURITY.md) first.

## Project Boundaries

MacVitals is a live observation tool. It does not control fans or modify system thermal policies. The current official distribution is Python source code; it does not provide a `.app` or DMG with an embedded Python runtime. A standalone build may be considered in the future if missing Python installations become a practical deployment obstacle.

See [LICENSE](LICENSE) for licensing terms: free non-commercial use is permitted, commercial monetization is prohibited, and externally distributed modifications must publish their complete source code under the same terms. This is a project-specific source-available license, not an OSI-approved Open Source License.

The complete Chinese project documentation is also synchronized to the author's personal Obsidian vault under `Project/MacVitals`, covering the product definition, architecture and algorithms, testing and release process, compatibility roadmap, and portfolio copy. This synchronization is a documentation snapshot, not an automatic background service.
