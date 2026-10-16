"""Run one command in its own process group and make sure the group is gone before the slot is given back.

On a terminal the command gets the foreground, so Ctrl-C, Ctrl-\\ and prompts reach it directly, and Ctrl-Z stops
the whole job the way a shell expects. A SIGTERM, SIGINT or SIGHUP sent to smart-build itself is passed to the whole
group; anything still alive after the grace period is killed. --timeout does the same and exits 124, like timeout(1).
Signals the caller ignores (nohup, `&` in a script) stay ignored, for smart-build and for the command.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# Signals whose inherited "ignore" is passed on to the command (a script's `&` ignores SIGINT and SIGQUIT).
INHERITED = FORWARDED + (signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2)
GUARD = Path(__file__).with_name("_guard.py")
TIMEOUT_EXIT = 124


@dataclass
class Outcome:
    exit_code: int
    duration: float
    timed_out: bool = False
    signal: int | None = None  # the signal that interrupted smart-build itself, if any
    child_signal: int | None = None  # the signal that ended the command, if one did


def ignored_signals() -> set[int]:
    """Signals this process inherited as ignored; they are left that way (nohup, background jobs in scripts)."""
    return {sig for sig in INHERITED if signal.getsignal(sig) == signal.SIG_IGN}


def _controlling_tty() -> int | None:
    """The terminal on stdin when the command should own it: we are in its foreground job, stdout is the same
    terminal (not `| less` or `| tee`), and Ctrl-C is not ignored (not `&` in a script)."""
    if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
        return None
    try:
        if not (os.isatty(0) and os.isatty(1)) or os.tcgetpgrp(0) != os.getpgrp():
            return None
        if os.ttyname(0) != os.ttyname(1):
            return None
    except OSError:
        return None
    return 0


def _foreground_is(tty: int, pgid: int) -> bool:
    try:
        return os.tcgetpgrp(tty) == pgid
    except OSError:
        return False


def _reclaim_terminal(tty: int, pgid: int) -> None:
    """Take the terminal back for our job if the command's group had it. macOS clears the foreground group when that
    group dies, so "had it" includes a foreground group that no longer exists."""
    try:
        current = os.tcgetpgrp(tty)
    except OSError:
        current = None
    if current == os.getpgrp():
        return
    if current is None or current == pgid or current <= 0 or not _group_alive(current):
        _set_foreground(tty, os.getpgrp())


def _set_foreground(tty: int, pgid: int) -> None:
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(tty, pgid)
    except OSError:
        pass
    finally:
        signal.signal(signal.SIGTTOU, previous)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _tee(source: IO[bytes], sink: IO[bytes] | None, log: IO[bytes]) -> None:
    for chunk in iter(lambda: source.read1(65536), b""):
        if sink is not None:
            try:
                sink.write(chunk)
                sink.flush()
            except (OSError, ValueError):
                sink = None
        log.write(chunk)
        log.flush()


def _open_log(path: Path) -> IO[bytes]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float | None = None,
    grace: float = 10.0,
    log_paths: tuple[Path, Path] | None = None,
) -> Outcome:
    """Run command to completion. With log_paths, stdout and stderr are also copied to those two files."""
    started = time.monotonic()
    ignored = ignored_signals()
    tty = _controlling_tty() if log_paths is None else None
    logs = tuple(_open_log(path) for path in log_paths) if log_paths else ()
    pipe = subprocess.PIPE if logs else None

    stopping = threading.Event()
    interrupt: list[int] = []
    timed_out = threading.Event()
    pgid_box: list[int] = []

    def escalate(pgid: int) -> None:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _group_alive(pgid):
                return
            time.sleep(0.05)
        _signal_group(pgid, signal.SIGKILL)

    def begin_stop(sig: int) -> None:
        if stopping.is_set() or not pgid_box:
            return
        stopping.set()
        pgid = pgid_box[0]
        _signal_group(pgid, sig)
        if sig != signal.SIGHUP:
            _signal_group(pgid, signal.SIGCONT)  # a stopped group cannot act on SIGTERM
        threading.Thread(target=escalate, args=(pgid,), daemon=True).start()

    def on_signal(signum, _frame) -> None:
        if not interrupt:
            interrupt.append(signum)
        begin_stop(signum)

    # Handlers go in before the command starts, so a signal in between is not lost.
    previous = {sig: signal.signal(sig, on_signal) for sig in FORWARDED if sig not in ignored}

    popen_kwargs = {}
    if sys.version_info >= (3, 11):
        popen_kwargs["process_group"] = 0
    else:  # pragma: no cover - exercised on 3.10 only
        popen_kwargs["preexec_fn"] = os.setpgrp  # noqa: PLW1509 (no threads are running yet)
    # The guard leads the command's process group and stops it if this process dies without cleaning up.
    watch_read, watch_write = os.pipe()
    ignore_list = ",".join(str(int(sig)) for sig in sorted(ignored))
    guarded = [sys.executable, "-I", "-S", str(GUARD), str(watch_read), repr(grace), ignore_list, "--", *command]
    try:
        process = subprocess.Popen(
            guarded,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL if logs else None,
            stdout=pipe,
            stderr=pipe,
            pass_fds=(watch_read,),
            **popen_kwargs,
        )
    except OSError as exc:
        os.close(watch_read)
        os.close(watch_write)
        for log in logs:
            log.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        print(f"smart-build: cannot start the command guard: {exc.strerror or exc}", file=sys.stderr)
        return Outcome(126, 0.0)
    os.close(watch_read)
    pgid = process.pid
    try:
        os.setpgid(pgid, pgid)  # closes the race with the child's own setpgid; harmless once it has exec'd
    except OSError:
        pass
    if tty is not None and _foreground_is(tty, os.getpgrp()):  # still ours (no Ctrl-Z + bg meanwhile)
        _set_foreground(tty, pgid)
    pgid_box.append(pgid)
    try:
        os.write(watch_write, b"g")  # the guard starts the command only now, once the terminal is its own
    except BrokenPipeError:
        pass  # the guard is already gone; _wait collects its status

    def on_continue(_signum, _frame) -> None:
        # `fg` after `bg`: the shell gave the terminal back to our job; pass it on to the command.
        if tty is not None and _foreground_is(tty, os.getpgrp()):
            _set_foreground(tty, pgid)

    previous_cont = signal.signal(signal.SIGCONT, on_continue) if tty is not None else None
    if interrupt:
        begin_stop(interrupt[0])

    timer = None
    if timeout is not None:
        def on_timeout() -> None:
            timed_out.set()
            print(f"smart-build: timed out after {timeout:g}s; stopping the command", file=sys.stderr)
            begin_stop(signal.SIGTERM)

        timer = threading.Timer(timeout, on_timeout)
        timer.daemon = True
        timer.start()

    tees = []
    for source, sink, log in zip((process.stdout, process.stderr), (sys.stdout.buffer, sys.stderr.buffer), logs):
        thread = threading.Thread(target=_tee, args=(source, sink, log), daemon=True)
        thread.start()
        tees.append(thread)

    try:
        status = _wait(process.pid, pgid, tty)
    finally:
        if timer is not None:
            timer.cancel()
        if previous_cont is not None:
            signal.signal(signal.SIGCONT, previous_cont)
        if tty is not None:
            _reclaim_terminal(tty, pgid)
        if stopping.is_set():
            # Whatever the command left running in its group goes too, within the same grace period.
            deadline = time.monotonic() + grace
            while _group_alive(pgid) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _group_alive(pgid):
                _signal_group(pgid, signal.SIGKILL)
        for thread in tees:
            thread.join(timeout=5)
        for log in logs:
            log.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        os.close(watch_write)

    process.returncode = status
    duration = time.monotonic() - started
    child_signal = -status if status < 0 else None
    exit_code = 128 + child_signal if child_signal else status
    if timed_out.is_set():
        return Outcome(TIMEOUT_EXIT, duration, timed_out=True, child_signal=child_signal)
    if interrupt:
        return Outcome(128 + interrupt[0], duration, signal=interrupt[0], child_signal=child_signal)
    return Outcome(exit_code, duration, child_signal=child_signal)


def _wait(pid: int, pgid: int, tty: int | None) -> int:
    """Wait for pid to exit. Returns its exit status, or -signal. Handles job control like a shell would."""
    while True:
        try:
            _, status = os.waitpid(pid, os.WUNTRACED)
        except ChildProcessError:
            print("smart-build: lost track of the command (already reaped); its exit status is unknown", file=sys.stderr)
            return 1
        if os.WIFSTOPPED(status):
            if tty is None:
                # No terminal, so no job control to take part in: whoever stopped the command resumes it.
                if os.WSTOPSIG(status) in (signal.SIGTTIN, signal.SIGTTOU):
                    print("smart-build: the command stopped because it wants the terminal, which it does not own here "
                          "(output piped or run in the background); Ctrl-C to stop it", file=sys.stderr)
                continue
            # Ctrl-Z (or a background read): stop our whole job, as a shell expects, and resume together.
            if _foreground_is(tty, pgid):
                _set_foreground(tty, os.getpgrp())
            os.killpg(os.getpgrp(), signal.SIGTSTP)  # the kernel drops this for an orphaned job; we just go on
            if _foreground_is(tty, os.getpgrp()):  # resumed with fg, not bg
                _set_foreground(tty, pgid)
            _signal_group(pgid, signal.SIGCONT)
            continue
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return os.WEXITSTATUS(status)
