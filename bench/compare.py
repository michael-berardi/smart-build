#!/usr/bin/env python3
"""Compare smart-build with another queue wrapper on identical workloads.

Usage: python3 bench/compare.py [--baseline "python3 /path/to/other_queue.py --project {project} --"] [--json]

Each tool gets a fresh state directory. The workloads:
  burst      12 builds of 0.5 s from 12 projects, arriving 50 ms apart, 2 slots
  overhead   20 sequential `true` runs with nothing else queued
  waiting    10 builds waiting 8 s behind a holder: CPU time the waiters burn
  sigterm    SIGTERM to the wrapper while its command has a child: does the child survive?
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shlex
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMART = f"{shlex.quote(sys.executable)} -m smart_build --slots 2 --project {{project}} --"


def tool_env(state: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SMART_BUILD", "BUILD_QUEUE"))}
    env.update(
        PYTHONPATH=str(ROOT / "src"),
        SMART_BUILD_STATE_DIR=str(state / "smart"),
        SMART_BUILD_MEMORY_FLOOR="0",
        LDS_BUILD_QUEUE_STATE_DIR=str(state / "baseline"),
        BUILD_QUEUE_GLOBAL_LIMIT="2",
        BUILD_QUEUE_NO_RETENTION_MARK="1",
    )
    return env


def spawn(prefix: str, project: Path, command: list[str], env: dict, **kwargs) -> subprocess.Popen:
    argv = shlex.split(prefix.format(project=shlex.quote(str(project)))) + command
    return subprocess.Popen(argv, cwd=project, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def stamp_job(log: Path, name: str, seconds: float) -> list[str]:
    now = f"{shlex.quote(sys.executable)} -c 'import time;print(time.time())'"
    return ["sh", "-c", f'echo "start {name} $({now})" >> {log}; sleep {seconds}; echo "end {name} $({now})" >> {log}']


def burst(prefix: str, work: Path, env: dict) -> dict:
    log = work / "burst.log"
    procs, arrival = [], []
    began = time.time()
    for i in range(12):
        project = work / f"burst-{i}"
        project.mkdir()
        arrival.append(f"j{i}")
        procs.append(spawn(prefix, project, stamp_job(log, f"j{i}", 0.5), env))
        time.sleep(0.05)
    for proc in procs:
        proc.wait()
    starts, ends = {}, {}
    for line in log.read_text().splitlines():
        kind, name, value = line.split()
        (starts if kind == "start" else ends)[name] = float(value)
    order = sorted(starts, key=starts.get)
    inversions = sum(1 for a in range(len(order)) for b in range(a + 1, len(order)) if arrival.index(order[a]) > arrival.index(order[b]))
    events = sorted([(t, 1) for t in starts.values()] + [(t, -1) for t in ends.values()], key=lambda e: (e[0], e[1]))
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    # Handoff gap: for each start after the first two, how long after the latest earlier end did it begin.
    gaps = []
    for name in order[2:]:
        earlier = [t for t in ends.values() if t <= starts[name]]
        if earlier:
            gaps.append(starts[name] - max(earlier))
    return {
        "peak": peak,
        "inversions": inversions,
        "handoffMeanMs": round(statistics.mean(gaps) * 1000) if gaps else None,
        "handoffMaxMs": round(max(gaps) * 1000) if gaps else None,
        "makespanS": round(max(ends.values()) - began, 2),
    }


def overhead(prefix: str, work: Path, env: dict) -> dict:
    project = work / "overhead"
    project.mkdir()
    times = []
    for _ in range(20):
        t = time.monotonic()
        spawn(prefix, project, ["true"], env).wait()
        times.append(time.monotonic() - t)
    return {"medianMs": round(statistics.median(times) * 1000), "maxMs": round(max(times) * 1000)}


def waiting_cpu(prefix: str, work: Path, env: dict) -> dict:
    holders = []
    for i in range(2):
        project = work / f"hold-{i}"
        project.mkdir()
        holders.append(spawn(prefix, project, ["sleep", "8"], env))
    time.sleep(1.0)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    waiters = []
    for i in range(10):
        project = work / f"wait-{i}"
        project.mkdir()
        waiters.append(spawn(prefix, project, ["true"], env))
    for proc in holders + waiters:
        proc.wait()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    used = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return {"cpuSecondsAllWaiters": round(used, 2)}


def sigterm(prefix: str, work: Path, env: dict) -> dict:
    project = work / "sigterm"
    project.mkdir()
    pidfile = work / "child.pid"
    proc = spawn(prefix, project, ["sh", "-c", f'sleep 60 & echo $! > {pidfile}; wait'], env)
    deadline = time.monotonic() + 30
    while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int(pidfile.read_text())
    proc.send_signal(signal.SIGTERM)
    proc.wait()
    time.sleep(0.5)
    try:
        os.kill(child, 0)
        survived = True
        os.kill(child, signal.SIGKILL)  # clean up exactly the process this run created
    except ProcessLookupError:
        survived = False
    return {"childSurvived": survived}


def measure(prefix: str) -> dict:
    results = {}
    for name, workload in (("burst", burst), ("overhead", overhead), ("waiting", waiting_cpu), ("sigterm", sigterm)):
        with tempfile.TemporaryDirectory(prefix="smart-build-bench-") as tmp:
            work = Path(tmp).resolve()
            env = tool_env(work / "state")
            results[name] = workload(prefix, work / "w" if (work / "w").mkdir() is None else work, env)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", help="another wrapper; {project} is replaced with the project directory")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = {"smart-build": measure(SMART)}
    if args.baseline:
        report["baseline"] = measure(args.baseline)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for tool, results in report.items():
            print(tool)
            for workload, values in results.items():
                print(f"  {workload:<9} " + ", ".join(f"{k}={v}" for k, v in values.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
