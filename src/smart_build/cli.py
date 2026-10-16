"""Command line: `smart-build [options] -- command...`, `smart-build status`, `smart-build stats`."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import sys
import time
from pathlib import Path

from . import __version__, runner, system
from .queue import (
    TICKET_ID,
    STATE_FOLLOWING,
    STATE_PROJECT,
    STATE_QUEUED,
    STATE_RUNNING,
    Lease,
    QueueError,
    WaitTimeout,
    default_state_dir,
    inside_live_run,
    is_locked,
    live_tickets,
    log_event,
    prepare_state_dir,
    prune_runs,
    read_events,
    read_run_result,
    read_ticket,
    resolve_project,
    run_log_paths,
    write_run_result,
)

EXIT_REFUSED = 2
EXIT_BUSY = 75  # EX_TEMPFAIL: did not get a turn within --max-wait
FOLLOW_POLL = 0.1

USAGE = """\
smart-build [options] [--] command [args...]
       smart-build status [--json]
       smart-build stats [--since DURATION] [--json]"""

EPILOG = """\
environment:
  SMART_BUILD_SLOTS          default for --slots
  SMART_BUILD_MEMORY_FLOOR   default for --memory-floor
  SMART_BUILD_COALESCE=1     turn on --coalesce
  SMART_BUILD_STATE_DIR      state directory (default $XDG_STATE_HOME/smart-build or ~/.local/state/smart-build)

exit status: the command's own; 124 timed out; 75 no turn within --max-wait; 2 refused (the message says why);
126/127 the command could not be run; 128+N interrupted by signal N.
"""


# ---------------------------------------------------------------- argument helpers


def parse_duration(text: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*", text)
    if not match:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (examples: 90, 90s, 30m, 2h)")
    value = float(match.group(1))
    return value * {"ms": 0.001, "s": 1, None: 1, "m": 60, "h": 3600}[match.group(2)]


def parse_size(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgGtT]i?[bB]?|[bB])?\s*", text)
    if not match:
        raise argparse.ArgumentTypeError(f"not a size: {text!r} (examples: 0, 512M, 4G)")
    unit = (match.group(2) or "b")[0].lower()
    return int(float(match.group(1)) * {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[unit])


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more: {text}")
    return value


def jobs_value(text: str) -> str:
    if text in ("auto", "off") or (text.isdigit() and int(text) > 0):
        return text
    raise argparse.ArgumentTypeError(f"--jobs takes auto, off or a number, not {text!r}")


def env_value(name: str, parse, fallback):
    raw = os.environ.get(name)
    if raw is None or raw.strip() in ("", "auto"):
        return fallback()
    try:
        return parse(raw)
    except argparse.ArgumentTypeError as exc:
        raise QueueError(f"{name}: {exc}") from None


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smart-build",
        usage=USAGE,
        description=(
            "Run heavy builds one fair turn at a time across the whole machine: a few at once, one per project, "
            "first come first served, and never left running when interrupted."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"smart-build {__version__}")
    parser.add_argument("--project", metavar="NAME", help="extra name to serialize on (the Git work tree and directory always are)")
    parser.add_argument("--cwd", metavar="DIR", help="directory to run the command in (default: the current one)")
    parser.add_argument("--slots", type=positive_int, metavar="N", help="builds that may run at once machine-wide (default: auto)")
    parser.add_argument("--weight", type=positive_int, default=1, metavar="N", help="slots this build takes (default 1)")
    parser.add_argument("--jobs", type=jobs_value, default="off", metavar="auto|N|off",
                        help="set CARGO_BUILD_JOBS, CMAKE_BUILD_PARALLEL_LEVEL and GOFLAGS -p for the command when unset (auto = its CPU share)")
    parser.add_argument("--memory-floor", type=parse_size, metavar="SIZE",
                        help="hold a build while less memory than this is free and another build is running (default: auto, 0 = off)")
    parser.add_argument("--coalesce", action="store_true", default=None,
                        help="if an identical build is already waiting, wait for it and share its output and exit status")
    parser.add_argument("--timeout", type=parse_duration, metavar="DURATION", help="stop the command after this long; exit 124")
    parser.add_argument("--max-wait", type=parse_duration, metavar="DURATION", help="give up (exit 75) if the build has not started by then")
    parser.add_argument("--no-wait", action="store_true", help="same as --max-wait 0")
    parser.add_argument("--grace", type=parse_duration, default=10.0, metavar="DURATION",
                        help="time between SIGTERM and SIGKILL when stopping a command (default 10s)")
    parser.add_argument("--dry-run", action="store_true", help="wait for a turn, report it, and exit without running")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


# ---------------------------------------------------------------- run


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("status", "stats"):
        return report_main(argv[0], argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.print_usage(sys.stderr)
        print("smart-build: no command given", file=sys.stderr)
        return EXIT_REFUSED
    try:
        code = run_main(args, command)
        if DIE_BY:
            die_by(DIE_BY[0])
        return code
    except WaitTimeout as exc:
        print(f"smart-build: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except Interrupted as exc:
        die_by(exc.signum)
        return 128 + exc.signum
    except QueueError as exc:
        print(f"smart-build: {exc}", file=sys.stderr)
        return EXIT_REFUSED


DIE_BY: list[int] = []  # a signal to end with, once the lease is released, so shells see how the build ended


def die_by(signum: int) -> None:
    """End this process by signum (a shell loop stops on a SIGINT death, not on exit status 130)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    signal.signal(signum, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
    os.kill(os.getpid(), signum)


class Interrupted(Exception):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def _raise_interrupted(signum, _frame):
    raise Interrupted(signum)


def run_main(args: argparse.Namespace, command: list[str]) -> int:
    state = prepare_state_dir(default_state_dir())
    say = (lambda message: None) if args.quiet else (lambda message: print(f"smart-build: {message}", file=sys.stderr, flush=True))
    keys = resolve_project(args.cwd, args.project)
    slots = args.slots or env_value("SMART_BUILD_SLOTS", positive_int, system.auto_slots)
    memory_floor = args.memory_floor
    if memory_floor is None:
        memory_floor = env_value("SMART_BUILD_MEMORY_FLOOR", parse_size, system.auto_memory_floor)
    coalesce = args.coalesce if args.coalesce is not None else os.environ.get("SMART_BUILD_COALESCE") == "1"
    max_wait = 0.0 if args.no_wait else args.max_wait

    parent = inside_live_run(state)
    if parent is not None:
        # Already inside a smart-build run (a build script calling smart-build again): its turn covers this one.
        say("already inside a smart-build run; running without queueing again")
        if args.dry_run:
            return 0
        outcome = runner.run(command, cwd=keys.cwd, env={**os.environ, **({"PWD": str(keys.cwd)} if args.cwd else {})},
                             timeout=args.timeout, grace=args.grace)
        record_signal_death(outcome)
        return outcome.exit_code

    for sig in runner.FORWARDED:
        if sig not in runner.ignored_signals():  # nohup and background jobs keep their ignored signals
            signal.signal(sig, _raise_interrupted)
    lease = Lease(
        state=state,
        keys=keys,
        command=command,
        slots=slots,
        weight=args.weight,
        memory_floor=memory_floor,
        coalesce=coalesce,
        max_wait=max_wait,
        report=say,
        memory_probe=system.available_memory,
    )
    if max_wait is not None:
        lease.deadline = time.monotonic() + max_wait  # one clock, whether this caller follows, falls back or queues
    try:
        ticket = lease.open_ticket()
        log_event(state, "queued", ticket=ticket.id, project=keys.label, cwd=str(keys.cwd), command=ticket.preview(200), weight=lease.weight)
        prune_runs(state)
        leader = lease.find_leader()
        if leader is not None:
            result = follow(state, lease, leader, say, args.timeout)
            if result is not None:
                return result
        lease.acquire()
    except Interrupted as exc:
        lease.release()
        log_event(state, "abandoned", ticket=lease.ticket.id if lease.ticket else None, signal=exc.signum)
        DIE_BY.append(exc.signum)
        return 128 + exc.signum
    except BaseException:
        lease.release()
        raise

    ticket = lease.ticket
    try:
        held = ", ".join(str(n + 1) for n in ticket.slots)
        if lease.waited >= 1:
            say(f"starting after {human_duration(lease.waited)} (slot {held} of {slots})")
        log_event(state, "started", ticket=ticket.id, project=keys.label, waited=round(lease.waited, 3), slots=ticket.slots)
        if args.dry_run:
            say(f"dry run: would run {shlex.join(command)} in {keys.cwd} (slot {held} of {slots})")
            log_event(state, "finished", ticket=ticket.id, project=keys.label, exit=0, duration=0, waited=round(lease.waited, 3), dry_run=True)
            return 0
        env = child_environment(ticket.id, state, slots, lease.weight, args.jobs, keys.cwd if args.cwd else None)
        log_paths = run_log_paths(state, ticket.id) if ticket.coalesce_key else None
        outcome = runner.run(command, cwd=keys.cwd, env=env, timeout=args.timeout, grace=args.grace, log_paths=log_paths)
        record_signal_death(outcome)
        if ticket.coalesce_key and outcome.signal is None and not outcome.timed_out:
            # A build its own caller interrupted or timed out says nothing about the build; followers run it again.
            write_run_result(state, ticket.id, outcome.exit_code)
        log_event(
            state,
            "finished",
            ticket=ticket.id,
            project=keys.label,
            exit=outcome.exit_code,
            duration=round(outcome.duration, 3),
            waited=round(lease.waited, 3),
            timed_out=outcome.timed_out,
            signal=outcome.signal,
        )
        return outcome.exit_code
    finally:
        lease.release()


def record_signal_death(outcome: runner.Outcome) -> None:
    """Die the way a shell expects: by the signal that interrupted us, or by SIGINT if Ctrl-C ended the command."""
    if outcome.signal is not None:
        DIE_BY.append(outcome.signal)
    elif outcome.child_signal == signal.SIGINT and not outcome.timed_out:
        DIE_BY.append(signal.SIGINT)


def child_environment(ticket_id: str, state: Path, slots: int, weight: int, jobs: str, cwd: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    if cwd is not None:
        env["PWD"] = str(cwd)  # with --cwd; programs (make's $(PWD)) read it instead of asking the kernel
    share = system.jobs_share(slots, weight)
    env["SMART_BUILD"] = "1"
    env["SMART_BUILD_TICKET"] = ticket_id
    env["SMART_BUILD_STATE_DIR_RESOLVED"] = str(state)
    env["SMART_BUILD_CPU_SHARE"] = str(share)
    if jobs != "off":
        count = str(share if jobs == "auto" else int(jobs))
        env.setdefault("CARGO_BUILD_JOBS", count)
        env.setdefault("CMAKE_BUILD_PARALLEL_LEVEL", count)
        env.setdefault("GOFLAGS", f"-p={count}")
    return env


def follow(state: Path, lease: Lease, leader, say, timeout: float | None) -> int | None:
    """Wait for an identical build that was queued first, replaying its output. None means run it ourselves.

    The follower keeps its own limits: --max-wait until the shared build starts, --timeout once it has.
    """
    lease.follow(leader)
    say(f"an identical build is already queued (pid {leader.pid}); sharing its result")
    log_event(state, "following", ticket=lease.ticket.id, leader=leader.id)
    offsets = [0, 0]
    sinks = (sys.stdout.buffer, sys.stderr.buffer)
    started_at = None
    checked = 0.0

    def drain(leader_id: str) -> None:
        for index, path in enumerate(run_log_paths(state, leader_id)):
            try:
                with open(path, "rb") as handle:
                    handle.seek(offsets[index])
                    while chunk := handle.read(65536):
                        sinks[index].write(chunk)
                        offsets[index] += len(chunk)
                sinks[index].flush()
            except FileNotFoundError:
                pass

    while True:
        result = read_run_result(state, leader.id)
        if result is not None:
            drain(leader.id)
            log_event(state, "finished", ticket=lease.ticket.id, project=lease.keys.label, exit=result,
                      duration=0, waited=round(time.time() - lease.ticket.created, 3), shared_with=leader.id)
            lease.release()
            return result
        if run_log_paths(state, leader.id)[0].exists():
            started_at = started_at or time.monotonic()
            drain(leader.id)
        if time.monotonic() - checked >= 0.5:
            checked = time.monotonic()
            if not is_locked(state / "tickets" / f"{leader.id}.lock"):  # its owner has exited
                if read_run_result(state, leader.id) is not None:
                    continue
                say("the build being shared stopped without a result; running it here instead")
                lease.unfollow()
                return None
            current = read_ticket(state, leader.id)
            if current is not None and current.state == STATE_FOLLOWING and current.leader and \
                    TICKET_ID.fullmatch(current.leader) and not offsets[0] and not offsets[1]:
                # The build we joined joined another one first: share that one's result instead.
                switched = read_ticket(state, current.leader)
                if switched is not None and switched.id == current.leader:
                    leader = switched
                    lease.follow(leader)
        if started_at is None:
            if lease.deadline is not None and time.monotonic() >= lease.deadline:
                raise WaitTimeout(f"gave up after {lease.max_wait:g}s waiting for the shared build to start")
        elif timeout is not None and time.monotonic() - started_at >= timeout:
            drain(leader.id)
            print(f"smart-build: timed out after {timeout:g}s waiting for the shared build", file=sys.stderr)
            lease.release()
            return runner.TIMEOUT_EXIT
        time.sleep(FOLLOW_POLL)


# ---------------------------------------------------------------- status and stats


def report_main(kind: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"smart-build {kind}")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    if kind == "stats":
        parser.add_argument("--since", type=parse_duration, metavar="DURATION", help="only builds that finished this recently (e.g. 24h)")
    args = parser.parse_args(argv)
    try:
        state = prepare_state_dir(default_state_dir())
        if kind == "status":
            return status(state, args.json)
        return stats(state, args.json, args.since)
    except QueueError as exc:
        print(f"smart-build: {exc}", file=sys.stderr)
        return EXIT_REFUSED


def status(state: Path, as_json: bool) -> int:
    tickets = live_tickets(state)
    # Builds carry the slot count they were queued under; with none running, report this caller's default.
    slots = max((t.limit for t in tickets if t.limit), default=0) or env_value("SMART_BUILD_SLOTS", positive_int, system.auto_slots)
    now = time.time()
    available = system.available_memory()
    queued = [t for t in tickets if t.state == STATE_QUEUED]
    data = {
        "version": __version__,
        "stateDir": str(state),
        "slots": slots,
        "cpus": system.cpu_count(),
        "memoryAvailable": available,
        "memoryFloor": env_value("SMART_BUILD_MEMORY_FLOOR", parse_size, system.auto_memory_floor),
        "builds": [
            {
                "state": t.state,
                "position": queued.index(t) + 1 if t in queued else None,
                "pid": t.pid,
                "project": t.project,
                "cwd": t.cwd,
                "command": t.command,
                "weight": t.weight,
                "slots": t.slots,
                "since": t.started if t.state == STATE_RUNNING else t.created,
                "seconds": round(now - (t.started if t.state == STATE_RUNNING and t.started else t.created), 1),
                "leader": t.leader,
            }
            for t in tickets
        ],
    }
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    running = [b for b in data["builds"] if b["state"] == STATE_RUNNING]
    used = sum(b["weight"] for b in running)
    memory = f", {available / 1024**3:.1f} GiB memory free" if available is not None else ""
    print(f"{used} of {slots} slots busy, {len(queued)} queued{memory}")
    labels = {STATE_RUNNING: "running", STATE_QUEUED: "queued", STATE_PROJECT: "project", STATE_FOLLOWING: "sharing"}
    order = {STATE_RUNNING: 0, STATE_QUEUED: 1, STATE_PROJECT: 2, STATE_FOLLOWING: 3}
    for build in sorted(data["builds"], key=lambda b: (order[b["state"]], b["since"])):
        tag = labels[build["state"]]
        if build["position"]:
            tag = f"#{build['position']}"
        text = " ".join(build["command"])
        if len(text) > 60:
            text = text[:59] + "…"
        name = build["project"].rstrip("/").rsplit("/", 1)[-1]
        print(f"  {tag:<8} {human_duration(build['seconds']):>7}  {name:<24} {text}  (pid {build['pid']})")
    return 0


def stats(state: Path, as_json: bool, since: float | None) -> int:
    cutoff = time.time() - since if since else 0
    finished = [e for e in read_events(state) if e.get("event") == "finished" and e.get("time", 0) >= cutoff and not e.get("dry_run")]
    waits = sorted(float(e.get("waited", 0)) for e in finished)
    waited = [w for w in waits if w >= 1]
    shared = sum(1 for e in finished if e.get("shared_with"))
    failed = sum(1 for e in finished if e.get("exit") not in (0, None))
    projects: dict[str, float] = {}
    for event in finished:
        projects[event.get("project", "?")] = projects.get(event.get("project", "?"), 0) + float(event.get("duration", 0))

    def percentile(values: list[float], fraction: float) -> float:
        return values[min(len(values) - 1, int(len(values) * fraction))] if values else 0.0

    data = {
        "builds": len(finished),
        "failed": failed,
        "shared": shared,
        "waited": len(waited),
        "waitMedian": percentile(waited, 0.5),
        "waitP90": percentile(waited, 0.9),
        "waitTotal": sum(waits),
        "busiest": sorted(projects.items(), key=lambda item: -item[1])[:5],
    }
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"{data['builds']} builds, {failed} failed, {shared} shared with an identical build")
    if waited:
        print(
            f"{len(waited)} waited for a turn: median {human_duration(data['waitMedian'])}, "
            f"p90 {human_duration(data['waitP90'])}, {human_duration(data['waitTotal'])} in total"
        )
    else:
        print("none waited for a turn")
    for name, seconds in data["busiest"]:
        print(f"  {human_duration(seconds):>8}  {name.rstrip('/').rsplit('/', 1)[-1]}  ({name})" if "/" in name else f"  {human_duration(seconds):>8}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
