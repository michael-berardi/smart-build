"""End-to-end tests: real processes, real locks, a private state directory per test."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smart_build import queue, system  # noqa: E402
from smart_build.queue import Lease, QueueError, resolve_project  # noqa: E402


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="smart-build-test-")).resolve()
        self.state = self.tmp / "state"
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SMART_BUILD")}
        env.update(PYTHONPATH=str(ROOT / "src"), SMART_BUILD_STATE_DIR=str(self.state), SMART_BUILD_MEMORY_FLOOR="0")
        env.update(extra)
        return env

    def project(self, name: str) -> Path:
        path = self.work / name
        path.mkdir(exist_ok=True)
        return path

    def start(self, *args: str, cwd: Path | None = None, env: dict | None = None, **kwargs) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "smart_build", *args],
            cwd=cwd or self.work,
            env=env or self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kwargs,
        )
        self.procs.append(proc)
        return proc

    def run_cli(self, *args: str, cwd: Path | None = None, timeout: float = 60, **env) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "smart_build", *args],
            cwd=cwd or self.work,
            env=self.env(**env),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def wait_for(self, predicate, timeout: float = 60.0, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")

    def tickets(self) -> list[queue.Ticket]:
        return queue.live_tickets(self.state) if self.state.exists() else []

    @staticmethod
    def start_order(log: Path) -> list[str]:
        """Names in the order their builds wrote "start" (one slot, so append order is run order; wall clocks in
        a loaded VM can jump between CPUs and misorder timestamps)."""
        return [line.split()[1] for line in log.read_text().splitlines() if line.startswith("start ")]

    def intervals(self, log: Path) -> list[tuple[str, float, float]]:
        """(name, start, end) for every job that wrote `start NAME T` and `end NAME T` lines to log."""
        starts, spans = {}, []
        for line in log.read_text().splitlines():
            kind, name, stamp = line.split()
            if kind == "start":
                starts[name] = float(stamp)
            else:
                spans.append((name, starts[name], float(stamp)))
        return spans

    def gate(self) -> tuple[list[str], Path]:
        """A command that runs until the test touches the returned path; slow machines cannot finish it early."""
        release = self.tmp / f"release-{len(self.procs)}"
        return ["sh", "-c", f'while [ ! -e "{release}" ]; do sleep 0.05; done'], release

    @staticmethod
    def held_job(log: Path, name: str, release: Path) -> list[str]:
        """Like job(), but runs until the test creates release, so slow machines cannot reorder the scenario."""
        script = (
            f'echo "start {name} $(python3 -c "import time;print(time.time())")" >> "{log}"; '
            f'while [ ! -e "{release}" ]; do sleep 0.05; done; '
            f'echo "end {name} $(python3 -c "import time;print(time.time())")" >> "{log}"'
        )
        return ["sh", "-c", script]

    @staticmethod
    def job(log: Path, name: str, seconds: float) -> list[str]:
        script = (
            f'echo "start {name} $(python3 -c "import time;print(time.time())")" >> "{log}"; '
            f"sleep {seconds}; "
            f'echo "end {name} $(python3 -c "import time;print(time.time())")" >> "{log}"'
        )
        return ["sh", "-c", script]


def peak_overlap(spans: list[tuple[str, float, float]]) -> int:
    points = sorted([(s, 1) for _, s, _ in spans] + [(e, -1) for _, _, e in spans], key=lambda p: (p[0], p[1]))
    current = peak = 0
    for _, delta in points:
        current += delta
        peak = max(peak, current)
    return peak


class CapacityTests(Harness):
    def test_never_more_than_slots_and_reaches_the_cap(self):
        log = self.tmp / "log"
        procs = [
            self.start("--slots", "2", "--", *self.job(log, f"j{i}", 0.4), cwd=self.project(f"p{i}"))
            for i in range(8)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0, proc.stderr.read())
        spans = self.intervals(log)
        self.assertEqual(len(spans), 8)
        self.assertEqual(peak_overlap(spans), 2)

    def test_first_come_first_served(self):
        log = self.tmp / "log"
        release = self.tmp / "release"
        blocker = self.start("--slots", "1", "--", *self.held_job(log, "blocker", release), cwd=self.project("b"))
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="blocker to run")
        order = []
        for i in range(5):
            order.append(f"j{i}")
            self.start("--slots", "1", "--", *self.job(log, f"j{i}", 0.05), cwd=self.project(f"p{i}"))
            # Queued for a slot, not just arrived: a build still taking its project lock is not in line yet.
            self.wait_for(lambda n=i + 1: sum(t.state == queue.STATE_QUEUED for t in self.tickets()) >= n,
                          what=f"j{i} to queue")
        release.touch()
        for proc in self.procs:
            proc.wait(timeout=60)
        self.assertEqual(self.start_order(log), ["blocker", *order])
        self.assertEqual(blocker.returncode, 0)

    def test_weight_takes_several_slots(self):
        log = self.tmp / "log"
        heavy = self.start("--slots", "2", "--weight", "2", "--", *self.job(log, "heavy", 0.6), cwd=self.project("h"))
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="heavy to run")
        light = self.start("--slots", "2", "--", *self.job(log, "light", 0.1), cwd=self.project("l"))
        heavy.wait(timeout=30)
        light.wait(timeout=30)
        spans = {name: (s, e) for name, s, e in self.intervals(log)}
        self.assertGreaterEqual(spans["light"][0], spans["heavy"][1])

    def test_weight_above_slots_is_clamped(self):
        result = self.run_cli("--slots", "1", "--weight", "3", "--", "true", cwd=self.project("w"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("weight 3 is more than the 1 slots", result.stderr)


class ProjectTests(Harness):
    def test_same_directory_never_overlaps_whatever_the_label(self):
        log = self.tmp / "log"
        shared = self.project("shared")
        a = self.start("--slots", "4", "--project", "alpha", "--", *self.job(log, "a", 0.5), cwd=shared)
        b = self.start("--slots", "4", "--project", "beta", "--", *self.job(log, "b", 0.5), cwd=shared)
        a.wait(timeout=30)
        b.wait(timeout=30)
        self.assertEqual(peak_overlap(self.intervals(log)), 1)

    def test_subdirectories_of_one_git_work_tree_are_one_project(self):
        repo = self.project("repo")
        (repo / ".git").mkdir()  # all smart-build looks for; a worktree's .git file is tested below
        (repo / "app").mkdir()
        log = self.tmp / "log"
        a = self.start("--slots", "4", "--", *self.job(log, "a", 0.5), cwd=repo)
        b = self.start("--slots", "4", "--", *self.job(log, "b", 0.5), cwd=repo / "app")
        a.wait(timeout=30)
        b.wait(timeout=30)
        self.assertEqual(peak_overlap(self.intervals(log)), 1)

    def test_a_git_worktree_file_marks_the_work_tree(self):
        tree = self.project("worktree")
        (tree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
        (tree / "pkg" / "deep").mkdir(parents=True)
        keys = resolve_project(str(tree / "pkg" / "deep"), None)
        self.assertEqual(keys.toplevel, tree)
        self.assertIn(f"dir:{tree}", keys.keys)

    def test_different_projects_run_together(self):
        log = self.tmp / "log"
        a = self.start("--slots", "4", "--", *self.job(log, "a", 0.8), cwd=self.project("a"))
        b = self.start("--slots", "4", "--", *self.job(log, "b", 0.8), cwd=self.project("b"))
        a.wait(timeout=30)
        b.wait(timeout=30)
        self.assertEqual(peak_overlap(self.intervals(log)), 2)

    def test_builds_of_one_project_start_in_arrival_order(self):
        log = self.tmp / "log"
        shared = self.project("shared")
        release = self.tmp / "release"
        holder = self.start("--slots", "8", "--", *self.held_job(log, "holder", release), cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="holder")
        order = []
        for i in range(4):
            order.append(f"w{i}")
            self.start("--slots", "8", "--", *self.job(log, f"w{i}", 0.05), cwd=shared)
            self.wait_for(lambda n=i + 2: len(self.tickets()) >= n, what=f"w{i} to queue")
            time.sleep(0.3)  # let the earlier waiters back off, which used to let newcomers jump them
        release.touch()
        for proc in self.procs:
            proc.wait(timeout=60)
        self.assertEqual(self.start_order(log), ["holder", *order])
        self.assertEqual(holder.returncode, 0)

    def test_waiting_for_a_project_does_not_hold_a_slot(self):
        log = self.tmp / "log"
        shared = self.project("shared")
        first = self.start("--slots", "2", "--", *self.job(log, "first", 1.0), cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="first to run")
        self.start("--slots", "2", "--", *self.job(log, "second", 0.1), cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_PROJECT for t in self.tickets()), what="second to wait")
        other = self.run_cli("--slots", "2", "--no-wait", "--", "true", cwd=self.project("other"))
        self.assertEqual(other.returncode, 0, other.stderr)
        first.wait(timeout=30)

    def test_missing_cwd_is_refused_with_its_cause(self):
        result = self.run_cli("--cwd", str(self.tmp / "nope"), "--", "true")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--cwd does not exist", result.stderr)


class ProcessTests(Harness):
    def test_exit_status_and_output_pass_through(self):
        result = self.run_cli("--", "sh", "-c", "echo out; echo err >&2; exit 7", cwd=self.project("x"))
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "out\n")
        self.assertIn("err", result.stderr)

    def test_command_not_found_is_127(self):
        result = self.run_cli("--", "definitely-not-a-command-xyz", cwd=self.project("x"))
        self.assertEqual(result.returncode, 127)
        self.assertIn("command not found", result.stderr)

    def test_sigterm_stops_the_whole_tree_before_the_slot_is_released(self):
        pidfile = self.tmp / "grandchild.pid"
        script = f'sleep 300 & echo $! > "{pidfile}"; wait'
        proc = self.start("--slots", "1", "--grace", "1", "--", "sh", "-c", script, cwd=self.project("t"))
        self.wait_for(pidfile.exists, what="grandchild to start")
        self.wait_for(lambda: pidfile.read_text().strip() != "", what="pid file")
        grandchild = int(pidfile.read_text())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), -signal.SIGTERM)  # dies by the signal it was sent, like a shell job
        self.wait_for(lambda: not _alive(grandchild), timeout=5, what="the grandchild to be stopped")
        follow = self.run_cli("--slots", "1", "--no-wait", "--", "true", cwd=self.project("u"))
        self.assertEqual(follow.returncode, 0, follow.stderr)

    def test_term_ignoring_command_is_killed_after_grace(self):
        script = "trap '' TERM; while true; do sleep 0.1; done"
        proc = self.start("--grace", "0.5", "--", "sh", "-c", script, cwd=self.project("t"))
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="command to run")
        time.sleep(0.3)
        began = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), -signal.SIGTERM)
        self.assertLess(time.monotonic() - began, 10)

    def test_timeout_exits_124_and_stops_the_command(self):
        began = time.monotonic()
        result = self.run_cli("--timeout", "0.5", "--grace", "1", "--", "sleep", "30", cwd=self.project("t"))
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - began, 15)
        self.assertIn("timed out", result.stderr)

    def test_sigkilled_wrapper_frees_its_slot_and_its_command_stops(self):
        pidfile = self.tmp / "child.pid"
        script = f'sleep 300 & echo $! > "{pidfile}"; wait'
        proc = self.start("--slots", "1", "--grace", "1", "--", "sh", "-c", script, cwd=self.project("k"))
        self.wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), what="holder to run")
        grandchild = int(pidfile.read_text())
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        try:
            self.wait_for(lambda: not _alive(grandchild), timeout=10, what="the guard to stop the command")
            result = self.run_cli("--slots", "1", "--max-wait", "5", "--", "true", cwd=self.project("n"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.tickets(), [])
        finally:
            if _alive(grandchild):
                os.kill(grandchild, signal.SIGKILL)

    def test_command_killed_by_a_signal_reports_128_plus_it(self):
        for sig in (signal.SIGUSR1, signal.SIGKILL):
            result = self.run_cli("--", "sh", "-c", f"kill -{int(sig)} $$", cwd=self.project("s"))
            self.assertEqual(result.returncode, 128 + sig)
            self.assertNotIn("Traceback", result.stderr)

    def test_command_killed_by_sigint_ends_smart_build_by_sigint(self):
        result = self.run_cli("--", "sh", "-c", "kill -INT $$", cwd=self.project("s"))
        self.assertEqual(result.returncode, -signal.SIGINT)  # so a shell loop around it stops too

    def test_sigpipe_is_default_for_the_command(self):
        began = time.monotonic()
        result = self.run_cli("--", "sh", "-c", "yes | head -1", cwd=self.project("p"), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "y\n")
        self.assertNotIn("Broken pipe", result.stderr)
        self.assertLess(time.monotonic() - began, 10)

    def test_ignored_hangup_stays_ignored_like_nohup(self):
        done = self.tmp / "done"
        proc = subprocess.Popen(
            [sys.executable, "-m", "smart_build", "--", "sh", "-c", f'sleep 1; touch "{done}"'],
            cwd=self.project("n"), env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),
        )
        self.procs.append(proc)
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="command to run")
        time.sleep(0.3)
        proc.send_signal(signal.SIGHUP)
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertTrue(done.exists())

    def test_interrupt_of_the_group_stops_what_the_command_left_behind(self):
        pidfile, shell = self.tmp / "bg.pid", self.tmp / "sh.pid"
        # A non-interactive shell's background job ignores SIGINT, so it outlives a Ctrl-C to the group.
        script = f'echo $$ > "{shell}"; sleep 300 & echo $! > "{pidfile}"; wait'
        proc = self.start("--grace", "1", "--", "sh", "-c", script, cwd=self.project("c"))
        self.wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), what="background job")
        background = int(pidfile.read_text())
        os.killpg(os.getpgid(int(shell.read_text())), signal.SIGINT)  # what a terminal does on Ctrl-C
        self.assertNotEqual(proc.wait(timeout=30), 0)  # how sh reports it varies (dash dies by SIGINT, bash exits 129+)
        self.wait_for(lambda: not _alive(background), timeout=5, what="the background job to be stopped")

    def test_pwd_follows_cwd(self):
        target = self.project("target")
        result = self.run_cli("--cwd", str(target), "--", "sh", "-c", 'echo "$PWD"', cwd=self.work)
        self.assertEqual(result.stdout.strip(), str(target))

    def test_interrupted_while_waiting_leaves_no_ticket(self):
        hold, release = self.gate()
        holder = self.start("--slots", "1", "--", *hold, cwd=self.project("h"))
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="holder")
        waiter = self.start("--slots", "1", "--", "true", cwd=self.project("w"))
        self.wait_for(lambda: len(self.tickets()) == 2, what="waiter to queue")
        waiter.send_signal(signal.SIGTERM)
        self.assertEqual(waiter.wait(timeout=10), -signal.SIGTERM)
        self.assertEqual(len(self.tickets()), 1)
        release.touch()
        holder.wait(timeout=30)

    def test_jobs_auto_sets_unset_parallelism_variables_only(self):
        result = subprocess.run(
            [sys.executable, "-m", "smart_build", "--slots", "2", "--jobs", "auto", "--",
             "sh", "-c", 'echo "$CARGO_BUILD_JOBS $CMAKE_BUILD_PARALLEL_LEVEL $SMART_BUILD_CPU_SHARE"'],
            cwd=self.project("j"),
            env=self.env(CMAKE_BUILD_PARALLEL_LEVEL="3"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        share = str(system.jobs_share(2, 1))
        self.assertEqual(result.stdout.split(), [share, "3", share])


class QueueBehaviourTests(Harness):
    def test_max_wait_gives_up_with_75(self):
        hold, release = self.gate()
        holder = self.start("--slots", "1", "--", *hold, cwd=self.project("h"))
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="holder")
        result = self.run_cli("--slots", "1", "--max-wait", "0.3", "--", "true", cwd=self.project("w"))
        self.assertEqual(result.returncode, 75)
        self.assertIn("gave up", result.stderr)
        release.touch()
        holder.wait(timeout=30)

    def test_nested_call_runs_inside_the_parent_turn(self):
        inner = f"{sys.executable} -m smart_build --slots 1 -- echo inner"
        result = self.run_cli("--slots", "1", "--max-wait", "10", "--", "sh", "-c", inner, cwd=self.project("n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("inner", result.stdout)
        self.assertIn("already inside a smart-build run", result.stderr)

    def test_dry_run_runs_nothing(self):
        marker = self.tmp / "ran"
        result = self.run_cli("--dry-run", "--", "touch", str(marker), cwd=self.project("d"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("dry run", result.stderr)

    def test_status_json_shows_running_and_queued(self):
        hold, release = self.gate()
        holder = self.start("--slots", "1", "--", *hold, cwd=self.project("h"))
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="holder")
        waiter = self.start("--slots", "1", "--", "true", cwd=self.project("w"))
        self.wait_for(lambda: len(self.tickets()) == 2, what="waiter")
        result = self.run_cli("status", "--json", SMART_BUILD_SLOTS="1")
        data = json.loads(result.stdout)
        states = sorted((b["state"], b["position"]) for b in data["builds"])
        self.assertEqual(states, [("queued", 1), ("running", None)])
        time.sleep(1.1)  # stats counts waits of a second or more
        release.touch()
        holder.wait(timeout=30)
        waiter.wait(timeout=30)
        stats = json.loads(self.run_cli("stats", "--json").stdout)
        self.assertEqual(stats["builds"], 2)
        self.assertEqual(stats["waited"], 1)

    def test_bad_environment_value_is_refused_by_name(self):
        result = self.run_cli("--", "true", cwd=self.project("e"), SMART_BUILD_SLOTS="many")
        self.assertEqual(result.returncode, 2)
        self.assertIn("SMART_BUILD_SLOTS", result.stderr)


class CoalesceTests(Harness):
    def test_identical_queued_builds_run_once(self):
        counter = self.tmp / "count"
        shared = self.project("shared")
        hold, release = self.gate()
        holder = self.start("--slots", "4", "--", *hold, cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="holder")
        script = f'echo built >> "{counter}"; echo "shared output"; exit 5'
        leader = self.start("--coalesce", "--slots", "4", "--", "sh", "-c", script, cwd=shared)
        self.wait_for(lambda: len(self.tickets()) == 2, what="leader to queue")
        followers = [self.start("--coalesce", "--slots", "4", "--", "sh", "-c", script, cwd=shared) for _ in range(3)]
        self.wait_for(lambda: sum(t.state == queue.STATE_FOLLOWING for t in self.tickets()) == 3, what="followers")
        release.touch()
        for proc in [holder, leader, *followers]:
            proc.wait(timeout=60)
        self.assertEqual(counter.read_text().count("built"), 1)
        for proc in [leader, *followers]:
            self.assertEqual(proc.returncode, 5)
            self.assertIn("shared output", proc.stdout.read())
        stats = json.loads(self.run_cli("stats", "--json").stdout)
        self.assertEqual(stats["shared"], 3)

    def test_followers_rerun_a_build_whose_caller_interrupted_it(self):
        counter = self.tmp / "count"
        started = self.tmp / "started"
        shared = self.project("shared")
        hold, release = self.gate()
        holder = self.start("--slots", "4", "--", *hold, cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="holder")
        script = f'echo built >> "{counter}"; touch "{started}"; sleep 2'
        leader = self.start("--coalesce", "--grace", "1", "--", "sh", "-c", script, cwd=shared)
        self.wait_for(lambda: len(self.tickets()) == 2, what="leader")
        follower = self.start("--coalesce", "--", "sh", "-c", script, cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_FOLLOWING for t in self.tickets()), what="follower")
        release.touch()
        self.wait_for(started.exists, what="leader to start")
        leader.send_signal(signal.SIGTERM)
        self.assertEqual(leader.wait(timeout=30), -signal.SIGTERM)
        self.assertEqual(follower.wait(timeout=30), 0)
        self.assertEqual(counter.read_text().count("built"), 2)
        holder.wait(timeout=30)

    def test_followers_get_stdout_and_stderr_separately(self):
        shared = self.project("shared")
        hold, release = self.gate()
        holder = self.start("--slots", "4", "--", *hold, cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="holder")
        script = "echo to-out; echo to-err >&2"
        leader = self.start("--coalesce", "--", "sh", "-c", script, cwd=shared)
        self.wait_for(lambda: len(self.tickets()) == 2, what="leader")
        follower = self.start("--coalesce", "--", "sh", "-c", script, cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_FOLLOWING for t in self.tickets()), what="follower")
        release.touch()
        for proc in (holder, leader, follower):
            proc.wait(timeout=30)
        out, err = follower.stdout.read(), follower.stderr.read()
        self.assertEqual(out, "to-out\n")
        self.assertIn("to-err", err)

    def test_a_running_build_is_never_shared(self):
        counter = self.tmp / "count"
        shared = self.project("shared")
        script = f'echo built >> "{counter}"; sleep 0.5'
        first = self.start("--coalesce", "--", "sh", "-c", script, cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="first to run")
        second = self.start("--coalesce", "--", "sh", "-c", script, cwd=shared)
        first.wait(timeout=30)
        second.wait(timeout=30)
        self.assertEqual(counter.read_text().count("built"), 2)

    def test_follower_runs_itself_when_the_leader_vanishes(self):
        counter = self.tmp / "count"
        shared = self.project("shared")
        hold, release = self.gate()
        holder = self.start("--slots", "4", "--", *hold, cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_RUNNING for t in self.tickets()), what="holder")
        script = f'echo built >> "{counter}"'
        leader = self.start("--coalesce", "--", "sh", "-c", script, cwd=shared)
        self.wait_for(lambda: len(self.tickets()) == 2, what="leader")
        follower = self.start("--coalesce", "--", "sh", "-c", script, cwd=shared)
        self.wait_for(lambda: any(t.state == queue.STATE_FOLLOWING for t in self.tickets()), what="follower")
        leader.send_signal(signal.SIGKILL)
        release.touch()
        self.assertEqual(follower.wait(timeout=30), 0)
        self.assertEqual(counter.read_text().count("built"), 1)
        holder.wait(timeout=30)


class LeaseTests(Harness):
    def test_memory_gate_holds_only_while_another_build_runs(self):
        state = queue.prepare_state_dir(self.state)
        first = Lease(state=state, keys=resolve_project(str(self.project("a")), None), command=["a"], slots=4,
                      memory_floor=10, memory_probe=lambda: 1)
        first.acquire()  # nothing else running: never held
        second = Lease(state=state, keys=resolve_project(str(self.project("b")), None), command=["b"], slots=4,
                       memory_floor=10, memory_probe=lambda: 1, max_wait=0.3)
        with self.assertRaises(queue.WaitTimeout):
            second.acquire()
        second.release()
        first.release()

    def test_a_failed_wait_releases_everything(self):
        state = queue.prepare_state_dir(self.state)
        holder = Lease(state=state, keys=resolve_project(str(self.project("a")), None), command=["a"], slots=1)
        holder.acquire()
        keys = resolve_project(str(self.project("b")), None)
        waiter = Lease(state=state, keys=keys, command=["b"], slots=1, max_wait=0.2)
        with self.assertRaises(queue.WaitTimeout):
            waiter.acquire()
        self.assertEqual([t.id for t in queue.live_tickets(state)], [holder.ticket.id])
        fd = queue.try_lock(state / "projects" / queue.project_lock_name(keys.keys[0]))
        self.assertIsNotNone(fd)  # the waiter's project lock was released
        queue.unlock(fd)
        holder.release()

    def test_state_directory_writable_by_others_is_refused(self):
        self.state.mkdir(mode=0o777)
        os.chmod(self.state, 0o777)
        with self.assertRaises(QueueError) as caught:
            queue.prepare_state_dir(self.state)
        self.assertIn("writable by others", str(caught.exception))

    def test_lease_is_a_context_manager_for_embedding(self):
        state = queue.prepare_state_dir(self.state)
        keys = resolve_project(str(self.project("a")), "label")
        with Lease(state=state, keys=keys, command=["x"], slots=1) as lease:
            self.assertEqual(lease.ticket.state, queue.STATE_RUNNING)
            self.assertEqual(lease.ticket.slots, [0])
        self.assertEqual(queue.live_tickets(state), [])

    def test_ticket_numbers_follow_arrival_not_the_clock(self):
        state = queue.prepare_state_dir(self.state)
        ids = [queue.new_ticket_id(state) for _ in range(5)]
        self.assertEqual(ids, sorted(ids))
        (state / "sequence").write_text("garbage")
        keys = resolve_project(str(self.project("a")), None)
        waiting = Lease(state=state, keys=keys, command=["a"], slots=1)
        waiting.open_ticket()
        (state / "sequence").unlink()  # counter lost while a build waits
        later = queue.new_ticket_id(state)
        self.assertGreater(later, waiting.ticket.id)
        waiting.release()

    def test_slots_must_be_positive(self):
        with self.assertRaises(QueueError):
            Lease(state=self.state, keys=resolve_project(str(self.work), None), command=["x"], slots=0)



class TerminalTests(Harness):
    """Smart Build on a real pseudo-terminal, as a person or an agent's PTY would run it."""

    def run_on_tty(self, args: list[str], keystrokes: list[tuple[str, bytes]], timeout: float = 30) -> tuple[int, str]:
        import pty
        import select

        pid, master = pty.fork()
        if pid == 0:  # session leader with the pty as controlling terminal
            os.chdir(self.project("tty"))
            os.execve(sys.executable, [sys.executable, "-m", "smart_build", *args], self.env())
        output, pending, deadline = b"", list(keystrokes), time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if pending and pending[0][0].encode() in output:
                    os.write(master, pending.pop(0)[1])
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    return status, output.decode(errors="replace")
            while time.monotonic() < deadline:  # the terminal closed; wait for smart-build itself
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    return status, output.decode(errors="replace")
                time.sleep(0.05)
            self.fail(f"terminal session did not finish: {output!r}")
        finally:
            os.close(master)
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass

    def test_the_command_can_prompt_on_the_terminal(self):
        status, output = self.run_on_tty(["--", "sh", "-c", 'printf "name? "; read x; echo "got=$x"'],
                                         [("name?", b"hello\n")])
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
        self.assertIn("got=hello", output)

    def test_ctrl_c_reaches_the_command_and_ends_smart_build_by_sigint(self):
        status, output = self.run_on_tty(["--", "sh", "-c", "echo ready; exec sleep 30"], [("ready", b"\x03")])
        self.assertTrue(os.WIFSIGNALED(status), output)
        self.assertEqual(os.WTERMSIG(status), signal.SIGINT)
        self.assertEqual(self.tickets(), [])

    def test_ctrl_z_in_an_orphaned_session_does_not_hang(self):
        # The session leader's job is orphaned, so the kernel drops its SIGTSTP; the build must simply carry on.
        status, output = self.run_on_tty(["--", "sh", "-c", "echo ready; sleep 1; echo finished"], [("ready", b"\x1a")])
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
        self.assertIn("finished", output)

    def test_a_backgrounded_build_leaves_the_terminal_to_the_script(self):
        script = (f'"{sys.executable}" -m smart_build -- sleep 1 & '
                  'printf "name? "; read x; echo "got=$x"; wait')
        status, output = self.run_shell_on_tty(
            script, [("name?", b"hi\n")])
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
        self.assertIn("got=hi", output)

    def test_the_terminal_returns_to_the_script_afterwards(self):
        script = f'"{sys.executable}" -m smart_build -- true; printf "name? "; read x; echo "got=$x"'
        status, output = self.run_shell_on_tty(script, [("name?", b"hi\n")])
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
        self.assertIn("got=hi", output)

    def run_shell_on_tty(self, script: str, keystrokes):
        """Run `sh -c script` as the terminal's session leader (a non-interactive script on a terminal)."""
        import pty
        import select

        pid, master = pty.fork()
        if pid == 0:
            os.chdir(self.project("tty"))
            os.execve("/bin/sh", ["/bin/sh", "-c", script], self.env())
        output, pending, deadline = b"", list(keystrokes), time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                if pending and pending[0][0].encode() in output:
                    os.write(master, pending.pop(0)[1])
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b""
                    output += chunk
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    return status, output.decode(errors="replace")
            self.fail(f"script on the terminal did not finish: {output!r}")
        finally:
            os.close(master)
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass


class SafetyTests(Harness):
    def test_without_ps_or_path_the_guard_stops_only_its_own_group(self):
        pidfile = self.tmp / "bg.pid"
        bystander = subprocess.Popen(["sleep", "60"])  # same user, not part of the build
        self.addCleanup(lambda: (bystander.kill(), bystander.wait()))
        env = self.env(PATH="/nonexistent")
        proc = self.start("--grace", "1", "--", "/bin/sh", "-c", f'/bin/sleep 300 & echo $! > "{pidfile}"; wait',
                          cwd=self.project("np"), env=env)
        self.wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), what="background job")
        background = int(pidfile.read_text())
        proc.send_signal(signal.SIGKILL)  # the guard alone must clean up, without ps on PATH
        proc.wait(timeout=10)
        self.wait_for(lambda: not _alive(background), timeout=15, what="the guard to stop the build")
        self.assertIsNone(bystander.poll(), "a process outside the build was signalled")

    def test_an_earlier_waiter_never_deadlocks_on_a_key_a_later_build_holds(self):
        state = queue.prepare_state_dir(self.state)
        repo = self.project("repo")
        (repo / ".git").mkdir()
        (repo / "app").mkdir()
        keys = resolve_project(str(repo / "app"), None)
        first, second = (state / "projects" / queue.project_lock_name(k) for k in keys.keys)
        blocker = queue.try_lock(second)  # someone holds the second key, so the later build stops there
        # An earlier build, waiting for both keys (as after a --coalesce fallback).
        earlier_id = f"{0:020d}-1-abcdef"  # ahead of every numbered ticket
        earlier_lock = queue.try_lock(state / "tickets" / f"{earlier_id}.lock")
        later = Lease(state=state, keys=keys, command=["x"], slots=1, max_wait=5)
        later.open_ticket()
        thread = __import__("threading").Thread(target=lambda: self._acquire_quietly(later))
        thread.start()
        time.sleep(0.5)  # the later build now holds the first key and waits for the second
        (state / "tickets" / f"{earlier_id}.json").write_text(json.dumps({
            "id": earlier_id, "pid": os.getpid(), "created": 0, "state": queue.STATE_PROJECT, "project": "p",
            "cwd": str(repo / "app"), "command": ["x"], "keys": list(keys.keys)}))
        self.wait_for(lambda: self._free(first), timeout=5, what="the later build to let go of the first key")
        os.unlink(state / "tickets" / f"{earlier_id}.json")
        queue.unlock(earlier_lock)
        queue.unlock(blocker)
        thread.join(timeout=10)
        self.assertEqual(later.ticket.state, queue.STATE_RUNNING)
        later.release()

    def _acquire_quietly(self, lease):
        try:
            lease.acquire()
        except queue.QueueError:
            pass

    @staticmethod
    def _free(path):
        fd = queue.try_lock(path)
        if fd is None:
            return False
        queue.unlock(fd)
        return True


def _alive(pid: int) -> bool:
    """Running, not just unreaped: in a container nothing may reap an orphan's zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if os.path.isdir("/proc/self"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                return handle.read().rsplit(b")", 1)[1].split()[0] != b"Z"
        except (OSError, IndexError):
            return False  # exited in between
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


if __name__ == "__main__":
    unittest.main()
