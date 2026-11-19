#!/usr/bin/env python3
"""Read-only Linux inventory. No installs, privileged escalation or mutations."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess


def command(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
        return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": type(exc).__name__}


def read(path):
    try:
        return Path(path).read_text()
    except OSError as exc:
        return {"error": type(exc).__name__}


def cgroups(pid):
    membership = read(f"/proc/{pid}/cgroup")
    result = {"membership": membership, "visible_v2_hierarchy": {}}
    if not isinstance(membership, str):
        return result
    for line in membership.splitlines():
        if not line.startswith("0::"):
            continue
        relative = line[3:].lstrip("/")
        if ".." in Path(relative).parts:
            result["warning"] = "namespace hides cgroup path; collect on host too"
            continue
        root = Path("/sys/fs/cgroup")
        current = root / relative
        while True:
            result["visible_v2_hierarchy"][str(current)] = {
                name: read(current / name) for name in (
                    "cpu.max", "cpu.stat", "cpu.pressure", "cpuset.cpus.effective",
                    "cpuset.mems.effective", "memory.current", "memory.max", "memory.high",
                    "memory.events", "memory.stat", "memory.pressure", "memory.swap.current",
                    "memory.swap.max", "io.stat", "io.pressure")}
            if current == root:
                break
            current = current.parent
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ct", type=int, help="optional Proxmox container ID (host only)")
    parser.add_argument("--pid", type=int, help="actual inference worker PID")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.ct is not None and args.ct <= 0) or (args.pid is not None and args.pid <= 0):
        parser.error("IDs must be positive")
    commands = [["uname", "-a"], ["lscpu"], ["lscpu", "-e=CPU,CORE,SOCKET,NODE,ONLINE"],
                ["numactl", "--hardware"], ["free", "-b"], ["swapon", "--show", "--bytes"],
                ["df", "-h", "/"], ["ollama", "--version"]]
    if args.ct:
        commands += [["pct", "config", str(args.ct)], ["pveversion"]]
    result = {"timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "commands": {" ".join(cmd): command(cmd) for cmd in commands},
              "proc": {name: read("/proc/" + name) for name in
                       ("meminfo", "vmstat", "pressure/cpu", "pressure/memory", "pressure/io")},
              "collector_cgroups": cgroups(os.getpid())}
    # Exclude DIMM serial numbers and asset tags while keeping slot mapping.
    dmi = command(["dmidecode", "-t", "memory"])
    if "stdout" in dmi:
        allowed = ("Size:", "Locator:", "Bank Locator:", "Speed:", "Configured Memory Speed:", "Type:")
        dmi["stdout"] = "\n".join(line for line in dmi["stdout"].splitlines()
                                   if line.strip().startswith(allowed))
    result["memory_slots"] = dmi
    if args.pid:
        result["worker"] = {"pid": args.pid, "cgroups": cgroups(args.pid),
                            "proc": {name: read(f"/proc/{args.pid}/{name}")
                                     for name in ("status", "stat", "io", "smaps_rollup")}}
        try:
            result["worker"]["affinity"] = sorted(os.sched_getaffinity(args.pid))
        except OSError as exc:
            result["worker"]["affinity_error"] = type(exc).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, indent=2)
    print(f"Saved {args.output}; unavailable fields are explicitly marked.")


if __name__ == "__main__":
    main()
