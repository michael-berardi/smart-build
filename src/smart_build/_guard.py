"""The guard: the first process of every build's process group. Usage: _guard.py FD GRACE IGNORED -- command...

smart-build keeps the write end of a pipe whose read end is FD. If smart-build dies, even by SIGKILL, the pipe reads
end-of-file and the guard stops its whole process group (SIGTERM, then SIGKILL after GRACE seconds), so no build is
left running after the queue has handed its slot to someone else.

Otherwise the guard waits for the command and exits the way it did. If the group was told to stop (Ctrl-C, Ctrl-\\,
SIGTERM, SIGHUP), whatever the command left behind in the group is stopped too before the guard exits, so the slot is
never released while part of the build still runs. IGNORED is a comma-separated list of signal numbers the caller
ignores (nohup, `&` in a script); they stay ignored for the command.

It runs with `python -I -S` and imports only core modules, so it starts in a few milliseconds.
"""

import os
import select
import signal
import sys
import time

STOP_SIGNALS = (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP)
SURVIVE = STOP_SIGNALS + (signal.SIGUSR1, signal.SIGUSR2)
# Python ignores these at startup; a command must start with the defaults (or `yes | head` never ends).
PYTHON_IGNORES = tuple(getattr(signal, name) for name in ("SIGPIPE", "SIGXFSZ", "SIGXFZ") if hasattr(signal, name))
received = []


def main() -> None:
    watch, grace = int(sys.argv[1]), float(sys.argv[2])
    ignored = {int(n) for n in sys.argv[3].split(",") if n}
    command = sys.argv[5:]
    # A handler, not SIG_IGN: exec resets handlers to the default in the command, but would keep SIG_IGN.
    for sig in SURVIVE:
        if sig in ignored:
            signal.signal(sig, signal.SIG_IGN)
        else:
            signal.signal(sig, lambda number, _frame: received.append(number))
    # smart-build writes one byte once the terminal (if any) belongs to this group; end-of-file means it died first.
    if os.read(watch, 1) != b"g":
        os._exit(1)
    stops = [sig for sig in received if sig in STOP_SIGNALS]
    if stops:  # told to stop before the command started: do not start it
        os._exit(128 + stops[0])
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_write, False)
    signal.set_wakeup_fd(wake_write)
    signal.signal(signal.SIGCHLD, lambda *_: None)

    pid = os.fork()
    if pid == 0:
        os.close(watch)
        os.close(wake_read)
        os.close(wake_write)
        for sig in PYTHON_IGNORES:
            signal.signal(sig, signal.SIG_DFL)
        try:
            os.execvp(command[0], command)
        except FileNotFoundError:
            os.write(2, f"smart-build: command not found: {command[0]}\n".encode())
            os._exit(127)
        except OSError as exc:
            os.write(2, f"smart-build: cannot run {command[0]}: {exc.strerror}\n".encode())
            os._exit(126)

    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        try:
            ready, _, _ = select.select([watch, wake_read], [], [])
        except InterruptedError:
            continue
        if wake_read in ready:
            os.read(wake_read, 4096)
        if watch in ready and not os.read(watch, 1):
            stop_group(grace)  # smart-build is gone; nobody waits for the guard's status any more
            try:
                os.killpg(0, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os._exit(1)

    if any(sig in STOP_SIGNALS for sig in received):
        stop_group(grace, command_done=True)
    if os.WIFSIGNALED(status):
        die_like(os.WTERMSIG(status))
    if signal.SIGINT in received and os.WEXITSTATUS(status) == 128 + signal.SIGINT:
        # The command caught Ctrl-C, cleaned up and exited 130: report it as the interrupt it was, as shells do.
        die_like(signal.SIGINT)
    os._exit(os.WEXITSTATUS(status))


def die_like(number: int) -> None:
    """Exit the way the command did, so smart-build and shells see the same signal."""
    if number not in (signal.SIGKILL, signal.SIGSTOP):
        try:
            import resource  # the guard's own core dump would be noise

            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ImportError, ValueError, OSError):
            pass
        signal.signal(number, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {number})
    os.kill(os.getpid(), number)
    os._exit(128 + number)


def stop_group(grace: float, command_done: bool = False) -> None:
    """SIGTERM the group (the guard survives it), wait up to grace for it to empty, then SIGKILL the rest.

    Only pids read from the kernel as members of this group are ever signalled one by one, never a pid of 0 or less.
    """
    members = others()
    if command_done and members == []:  # None (unknown) still gets the full stop
        return
    for sig in (signal.SIGTERM, signal.SIGCONT):
        try:
            os.killpg(0, sig)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        reap_children()
        if others() == []:
            return
        time.sleep(0.1)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for _ in range(3):
        members = others()
        if members is None:
            os.write(2, b"smart-build: cannot list the build's processes; some may still be running\n")
            return
        if not members:
            return
        for pid in members:
            if pid > 0:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(0.05)
        reap_children()


def reap_children() -> None:
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                return
        except ChildProcessError:
            return


def others() -> list[int] | None:
    """Live pids in the guard's process group other than the guard; None when the system will not say."""
    members = group_members(os.getpgrp())
    if members is None:
        return None
    return [pid for pid in members if pid > 0 and pid != os.getpid()]


def group_members(group: int) -> list[int] | None:
    """Live (not zombie) pids in a process group, asked of the kernel: /proc on Linux, libproc on macOS."""
    if os.path.isdir("/proc/self"):
        members: list[int] = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as handle:
                    fields = handle.read().rsplit(b")", 1)[1].split()
            except (OSError, IndexError):
                continue
            # After the command name: state, ppid, pgrp. A zombie (Z) has already exited.
            if int(fields[2]) == group and fields[0] != b"Z":
                members.append(int(name))
        if group == os.getpgrp() and os.getpid() not in members:
            return None  # a /proc from another pid namespace: it cannot be trusted
        return members
    if sys.platform == "darwin":
        return _darwin_group_members(group)
    return None


def _darwin_group_members(group: int) -> list[int] | None:
    try:
        import ctypes  # rare path; keeps normal starts fast

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return None
    PROC_PGRP_ONLY = 2
    size = 256
    while size <= 65536:
        buffer = (ctypes.c_int * size)()
        used = libproc.proc_listpids(PROC_PGRP_ONLY, group, buffer, ctypes.sizeof(buffer))
        if used <= 0:
            return None  # an error (the guard itself is always in its group, so an empty answer is one too)
        count = used // ctypes.sizeof(ctypes.c_int)
        if count < size:
            pids = [buffer[i] for i in range(count) if buffer[i] > 0]
            if os.getpid() not in pids and group == os.getpgrp():
                return None
            return [pid for pid in pids if not _darwin_zombie(libproc, ctypes, pid)]
        size *= 4
    return None


def _darwin_zombie(libproc, ctypes, pid: int) -> bool:
    """True for an exited (zombie or gone) process. Uses PROC_PIDT_SHORTBSDINFO, which has no same-user check."""
    import errno

    info = (ctypes.c_uint8 * 64)()  # struct proc_bsdshortinfo; pbsi_status is the fourth uint32 (offset 12)
    ctypes.set_errno(0)
    if libproc.proc_pidinfo(pid, 13, 0, info, ctypes.sizeof(info)) <= 0:
        return ctypes.get_errno() == errno.ESRCH  # gone; any other error: assume it still runs
    return int.from_bytes(bytes(info[12:16]), sys.byteorder) == 5  # SZOMB


if __name__ == "__main__":
    main()
