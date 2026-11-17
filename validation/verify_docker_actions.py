"""Opt-in control check using only a self-created, isolated disposable container.

Uses a pre-existing local Linux/arm64 image without declared volumes; never
pulls an image, opens a port, mounts host paths, or operates a business container.
Only explicit --create-test-container permits mutation. No results are saved.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from macvitals.docker_monitor import DockerMonitor
from macvitals.docker_actions import DockerActions, ActionTarget


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-test-container", action="store_true")
    args = parser.parse_args()
    if not args.create_test_container:
        parser.error("requires --create-test-container; creates then removes one isolated container")
    cli = DockerMonitor(timeout=15)
    context, endpoint = cli.connection()
    base = ["--host", endpoint]
    token = uuid.uuid4().hex
    name = "macvitals-action-check-" + token
    label = "macvitals.validation=" + token
    image = None
    ids = cli._command(base + ["image", "ls", "--quiet", "--no-trunc"]).split()
    for image_id in list(dict.fromkeys(ids))[:30]:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            continue
        data = json.loads(cli._command(base + ["image", "inspect", "--format",
            '{"volumes":{{json .Config.Volumes}},"os":{{json .Os}},"arch":{{json .Architecture}}}', image_id]))
        if not data["volumes"] and data["os"] == "linux" and data["arch"] == "arm64":
            image = image_id
            break
    if image is None:
        raise SystemExit("No suitable local arm64 image without volumes; no image downloaded or container created")

    cid = None
    actions = DockerActions(context=context)
    report = {"context": context, "actions": []}
    try:
        # No --rm: stop must not implicitly delete the target under test.
        cid = cli._command(base + ["container", "create", "--pull", "never", "--name", name,
            "--label", label, "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", "64m", "--cpus", "0.1",
            "--pids-limit", "16", "--user", "65534:65534", "--workdir", "/",
            "--stop-signal", "SIGTERM", "--stop-timeout", "3", "--entrypoint", "/bin/sh", image,
            "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"]).strip()
        assert re.fullmatch(r"[0-9a-f]{64}", cid)
        for action in ("start", "restart", "stop"):
            sample = DockerMonitor(context=context).collect()
            container = next(c for c in sample.containers if c.id == cid)
            assert container.name == name
            chosen = ActionTarget.capture(sample, container)
            assert actions.submit(chosen, action)
            deadline = time.monotonic() + 85
            while actions.snapshot().status == "running" and time.monotonic() < deadline:
                time.sleep(.1)
            result = actions.snapshot()
            report["actions"].append({"action": action, "status": result.status})
            assert result.status == "success", result.message
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        actions.stop()
        if cid is None:
            # A create request might have reached the engine even if its CLI
            # timed out before returning the ID. Recover only our unique label.
            candidates = cli._command(base + ["container", "ls", "--all", "--quiet", "--no-trunc",
                                               "--filter", "label=" + label]).split()
            if len(candidates) == 1:
                cid = candidates[0]
            elif candidates:
                raise RuntimeError("Ambiguous test label; refusing cleanup")
        # Cleanup only the uniquely named and labeled container we just created.
        # In particular never remove by an unvalidated name or broad filter.
        if cid and re.fullmatch(r"[0-9a-f]{64}", cid):
            identity = json.loads(cli._command(base + ["container", "inspect", "--format",
                '{"name":{{json .Name}},"label":{{json (index .Config.Labels "macvitals.validation")}}}', cid]))
            if identity != {"name": "/" + name, "label": token}:
                raise RuntimeError("Test container identity changed; refusing cleanup")
            cli._command(base + ["container", "rm", "--force", cid])
            remaining = cli._command(base + ["container", "ls", "--all", "--quiet", "--filter", "label=" + label]).strip()
            assert not remaining, "Test container remains; manual inspection required"
            print("test_container_removed: true", flush=True)


if __name__ == "__main__":
    main()
