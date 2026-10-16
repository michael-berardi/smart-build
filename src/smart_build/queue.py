"""The machine-wide queue: tickets, slots and project locks, all plain files guarded by flock(2).

Nothing here runs in the background. Every waiting or running build owns a ticket: a lock file it keeps locked for
its whole life, plus a JSON record of what it is doing. A ticket whose lock can be taken belongs to a process that
has died, so it is removed by whoever notices. The kernel drops every lock when a process exits, however it exits,
so a crash can never leave a slot or a project lock held.

Layout of the state directory:

    slots/slot-000.lock ...      one file per slot; a build holds `weight` of them
    projects/<name>-<hash>.lock  one per project key (label, working directory, Git work tree)
    sequence                     the arrival counter that numbers tickets
    tickets/<id>.lock, <id>.json one pair per live invocation; ids sort in arrival order
    runs/<id>.out|.err|.result   shared output and status of a coalesced build, kept for an hour
    events.jsonl                 one line per queued, started and finished build
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator

LOCK_REOPEN_ATTEMPTS = 8
EVENT_LOG_LIMIT = 8 * 1024 * 1024
RUN_RETENTION_SECONDS = 3600
HEAD_POLL = 0.05
BEHIND_POLL = 0.2
PROJECT_POLL_MAX = 0.5
RESCAN = 1.0
RACY_NS = 1_100_000_000  # covers 1 s mtime granularity (HFS+) as well as coarse Linux ticks
MEMORY_CACHE = 1.0  # seconds a memory reading is reused while waiting
HEARTBEAT = 30.0  # seconds between "still waiting" lines, so a silent wait never looks hung

STATE_PROJECT = "waiting-project"  # waiting for its project lock
STATE_QUEUED = "queued"  # holds its project lock, waiting for slots
STATE_RUNNING = "running"
STATE_FOLLOWING = "following"  # waiting for an identical queued build to finish


class QueueError(RuntimeError):
    """A refusal with its cause in the message. Exit status 2."""


class WaitTimeout(QueueError):
    """The build did not get its turn within --max-wait. Exit status 75."""


# ---------------------------------------------------------------- state directory


def default_state_dir() -> Path:
    override = os.environ.get("SMART_BUILD_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "smart-build"


def prepare_state_dir(path: Path) -> Path:
    path = path.expanduser()
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in ("slots", "projects", "tickets", "runs"):
            (path / name).mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise QueueError(f"state directory unusable: {path}: {exc.strerror or exc}") from exc
    info = os.stat(path)
    if info.st_uid != os.getuid():
        raise QueueError(f"state directory belongs to another user: {path}")
    if info.st_mode & 0o022:
        raise QueueError(f"state directory is writable by others (mode {info.st_mode & 0o777:o}): {path}; chmod 700 it")
    if not os.access(path, os.W_OK):
        raise QueueError(f"state directory not writable: {path}")
    return path


# ---------------------------------------------------------------- locks


def _open_lock(path: Path) -> int:
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise QueueError(f"lock file unusable: {path}: {exc.strerror or exc}") from exc


def _still_at(fd: int, path: Path) -> bool:
    """True while fd is still the regular file at path (not unlinked or replaced)."""
    try:
        held, current = os.fstat(fd), os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(current.st_mode) and (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def try_lock(path: Path) -> int | None:
    """Exclusive non-blocking flock on path. The fd, or None while someone else holds it.

    A lock taken on an inode that is no longer the one at its path excludes nobody, so it is dropped and the path
    opened again.
    """
    for _ in range(LOCK_REOPEN_ATTEMPTS):
        fd = _open_lock(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return None
            raise QueueError(f"cannot lock {path}: {exc.strerror or exc}") from exc
        if _still_at(fd, path):
            return fd
        os.close(fd)
    raise QueueError(f"lock file {path} was replaced {LOCK_REOPEN_ATTEMPTS} times while locking it")


def unlock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_locked(path: Path) -> bool:
    """True while some live process holds path's lock. A missing file is not locked."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


# ---------------------------------------------------------------- directories and project keys


def canonical_directory(raw: str, what: str) -> Path:
    """Symlink-free absolute path of an existing directory, spelled the way the file system spells it."""
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise QueueError(f"{what} does not exist: {raw}") from exc
    except (OSError, RuntimeError) as exc:
        raise QueueError(f"{what} cannot be resolved: {raw}: {exc}") from exc
    if not resolved.is_dir():
        raise QueueError(f"{what} is not a directory: {raw}")
    get_path = getattr(fcntl, "F_GETPATH", None)
    if get_path is None:
        return resolved
    # On case-insensitive volumes resolve() keeps the caller's spelling; the kernel path is the one true key.
    fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_CLOEXEC)
    try:
        raw_path = fcntl.fcntl(fd, get_path, b"\0" * 1024)
    except OSError:
        return resolved
    finally:
        os.close(fd)
    kernel = os.fsdecode(raw_path.split(b"\0", 1)[0])
    return Path(kernel) if kernel else resolved


def git_toplevel(directory: Path) -> Path | None:
    """The Git work tree containing directory (the nearest ancestor with a .git entry), or None.

    Found by looking for `.git` rather than running git: it is what git itself does for a work tree, worktree or
    submodule, costs no process, and works where git is not installed.
    """
    for candidate in (directory, *directory.parents):
        marker = candidate / ".git"
        try:
            if marker.is_dir() or marker.is_file():
                return candidate
        except OSError:
            continue
    return None


def project_lock_name(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    base = Path(key).name or "project"
    safe = "".join(c for c in base if c.isalnum() or c in "-_.").strip("-.").lower()[:32] or "project"
    return f"{safe}-{digest}.lock"


@dataclass(frozen=True)
class ProjectKeys:
    cwd: Path
    toplevel: Path | None
    label: str
    explicit: str | None  # the --project value, when one was given
    keys: tuple[str, ...]  # what is locked, deduplicated and sorted by lock file


def resolve_project(cwd: str | None, project: str | None) -> ProjectKeys:
    """Everything one build serializes on: its --project label, its directory and its Git work tree.

    Locking all three means that two builds of the same checkout never overlap, whatever label each caller chose.
    """
    directory = canonical_directory(cwd if cwd else os.getcwd(), "--cwd" if cwd else "working directory")
    top = git_toplevel(directory)
    label = project or str(top or directory)
    wanted = {f"dir:{directory}"}
    if top is not None:
        wanted.add(f"dir:{top}")
    if project:
        wanted.add(f"label:{project}")
    keys = tuple(sorted(wanted, key=project_lock_name))
    return ProjectKeys(cwd=directory, toplevel=top, label=label, explicit=project, keys=keys)


# ---------------------------------------------------------------- tickets


@dataclass
class Ticket:
    id: str
    pid: int
    created: float
    state: str
    project: str
    cwd: str
    command: list[str]
    weight: int = 1
    limit: int = 0  # the slot count this build was queued under
    slots: list[int] = field(default_factory=list)
    started: float | None = None
    coalesce_key: str | None = None
    leader: str | None = None
    keys: list[str] = field(default_factory=list)  # project keys, for first-come-first-served project waits
    note: str = ""

    def preview(self, width: int = 80) -> str:
        text = " ".join(self.command)
        return text if len(text) <= width else text[: width - 1] + "…"


TICKET_ID = re.compile(r"\d{20}-\d+-[0-9a-f]{6}")


def new_ticket_id(state: Path) -> str:
    """The next ticket id: a machine-wide arrival number, so ids sort in arrival order.

    Wall clocks are not enough: on a busy VM they can step backwards between CPUs, and a later build would sort ahead
    of an earlier one. The counter is a file read and bumped under its own lock. If it was lost or damaged, numbering
    resumes after the newest live ticket, so no new build can sort ahead of one already waiting.
    """
    path = state / "sequence"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
        current = int(raw) if raw.isdigit() else -1
        if current < 0:
            live = [int(t.id.split("-", 1)[0]) for t in live_tickets(state)]
            current = max(live, default=0)
        number = current + 1
        text = str(number).encode("ascii")
        os.ftruncate(fd, 0)
        os.pwrite(fd, text, 0)
    except OSError as exc:
        raise QueueError(f"ticket counter unusable: {path}: {exc.strerror or exc}") from exc
    finally:
        os.close(fd)  # also releases the lock
    return f"{number:020d}-{os.getpid()}-{os.urandom(3).hex()}"


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(3).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_ticket(path: Path) -> Ticket | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Ticket(**data)
    except (OSError, ValueError, TypeError):
        return None


def live_tickets(state: Path) -> list[Ticket]:
    """Every ticket whose owner is alive, oldest first. Tickets of dead owners are removed on the way."""
    tickets = []
    directory = state / "tickets"
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    for name in names:
        if not name.endswith(".json"):
            continue
        ticket_id = name[:-5]
        lock = directory / f"{ticket_id}.lock"
        record = directory / name
        if not is_locked(lock):
            _reap_ticket(lock, record)
            continue
        ticket = _read_ticket(record)
        if ticket is not None:
            tickets.append(ticket)
    return tickets


def _reap_ticket(lock: Path, record: Path) -> None:
    """Remove a dead owner's ticket, holding its lock so a ticket being created right now is never touched."""
    fd = None
    try:
        fd = try_lock(lock)
    except QueueError:
        return
    if fd is None:
        return  # its owner is alive after all (it was just taking the lock)
    try:
        record.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    finally:
        unlock(fd)


# ---------------------------------------------------------------- events


def log_event(state: Path, event: str, **fields) -> None:
    path = state / "events.jsonl"
    line = json.dumps({"time": round(time.time(), 3), "event": event, "pid": os.getpid(), **fields})
    try:
        if path.exists() and path.stat().st_size > EVENT_LOG_LIMIT:
            os.replace(path, state / "events.1.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"smart-build: event log not written ({path}): {exc.strerror or exc}", file=sys.stderr)


def read_events(state: Path) -> Iterator[dict]:
    for name in ("events.1.jsonl", "events.jsonl"):
        try:
            with open(state / name, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except FileNotFoundError:
            continue


# ---------------------------------------------------------------- the lease


Reporter = Callable[[str], None]


@dataclass
class Lease:
    """One build's place in the queue, from arrival to release. Use acquire() and release(), or `with`."""

    state: Path
    keys: ProjectKeys
    command: list[str]
    slots: int
    weight: int = 1
    memory_floor: int = 0
    coalesce: bool = False
    max_wait: float | None = None
    report: Reporter = lambda message: None
    memory_probe: Callable[[], int | None] | None = None

    ticket: Ticket | None = None
    _ticket_fd: int | None = None
    _ticket_id: str = ""
    _project_fds: list[int] = field(default_factory=list)
    _slot_fds: list[int] = field(default_factory=list)
    waited: float = 0.0
    deadline: float | None = None  # monotonic time --max-wait runs out; set once, kept across a coalesce fallback
    _cache: list = field(default_factory=list)
    _seen: int | None = None
    _scanned: float = 0.0
    _read_at: int = 0
    _memory_read: tuple[float, int | None] | None = None

    # -------- lifecycle

    def __post_init__(self) -> None:
        if self.slots < 1:
            raise QueueError("slots must be at least 1")
        if self.weight < 1:
            raise QueueError("weight must be at least 1")
        if self.weight > self.slots:
            self.report(f"weight {self.weight} is more than the {self.slots} slots; using {self.slots}")
            self.weight = self.slots

    def __enter__(self) -> "Lease":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def open_ticket(self) -> Ticket:
        ticket_id = new_ticket_id(self.state)
        lock = self.state / "tickets" / f"{ticket_id}.lock"
        fd = try_lock(lock)
        if fd is None:
            raise QueueError(f"ticket {ticket_id} is already locked")
        self._ticket_fd, self._ticket_id = fd, ticket_id
        try:
            self._create_record(ticket_id)
        except BaseException:
            self.release()
            raise
        return self.ticket

    def _create_record(self, ticket_id: str) -> None:
        key = None
        if self.coalesce:
            key = hashlib.sha256(
                json.dumps([str(self.keys.cwd), self.command]).encode("utf-8")
            ).hexdigest()
        self.ticket = Ticket(
            id=ticket_id,
            pid=os.getpid(),
            created=time.time(),
            state=STATE_PROJECT,
            project=self.keys.label,
            cwd=str(self.keys.cwd),
            command=list(self.command),
            weight=self.weight,
            limit=self.slots,
            coalesce_key=key,
            keys=list(self.keys.keys),
        )
        self._save()

    def find_leader(self) -> Ticket | None:
        """An identical build that has not started yet, so it will see every change this caller can see."""
        if self.ticket is None or self.ticket.coalesce_key is None:
            return None
        for other in live_tickets(self.state):
            if (
                TICKET_ID.fullmatch(other.id)
                and other.id != self.ticket.id
                and other.coalesce_key == self.ticket.coalesce_key
                and other.state in (STATE_PROJECT, STATE_QUEUED)
                and other.id < self.ticket.id
            ):
                return other
        return None

    def follow(self, leader: Ticket) -> None:
        assert self.ticket is not None
        self.ticket.state = STATE_FOLLOWING
        self.ticket.leader = leader.id
        self._save()

    def unfollow(self) -> None:
        assert self.ticket is not None
        self.ticket.state = STATE_PROJECT
        self.ticket.leader = None
        self.ticket.coalesce_key = None  # never lead others after a leader vanished; keeps fallbacks simple
        self._save()

    def acquire(self) -> None:
        """Wait for the project locks, then for this build's turn and its slots."""
        if self.ticket is None:
            self.open_ticket()
        began = time.monotonic()
        if self.deadline is None and self.max_wait is not None:
            self.deadline = began + self.max_wait
        try:
            self._acquire_projects(self.deadline)
            self.ticket.state = STATE_QUEUED
            self._save()
            self._acquire_slots(self.deadline)
            self.ticket.state = STATE_RUNNING
            self.ticket.started = time.time()
            self._save()
        except BaseException:
            self.release()  # a failed or interrupted wait never leaves a ticket, project or slot held
            raise
        finally:
            self.waited = time.monotonic() - began

    def release(self) -> None:
        for fd in self._slot_fds:
            unlock(fd)
        self._slot_fds = []
        while self._project_fds:
            unlock(self._project_fds.pop())
        if self._ticket_fd is not None:
            directory = self.state / "tickets"
            ticket_id = self.ticket.id if self.ticket is not None else self._ticket_id
            (directory / f"{ticket_id}.json").unlink(missing_ok=True)
            (directory / f"{ticket_id}.lock").unlink(missing_ok=True)
            unlock(self._ticket_fd)
            self._ticket_fd = None

    def _save(self) -> None:
        assert self.ticket is not None
        _write_json_atomic(self.state / "tickets" / f"{self.ticket.id}.json", asdict(self.ticket))

    # -------- waiting

    def _check_deadline(self, deadline: float | None, what: str) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise WaitTimeout(f"gave up after {self.max_wait:g}s waiting for {what}")

    def _acquire_projects(self, deadline: float | None) -> None:
        """Take every project lock in global order. Waiters for a key take it in arrival order.

        If an earlier build turns up waiting for a key this build already holds (it fell back from --coalesce, or its
        ticket became visible late), this build lets go of everything and starts over, so neither can wait forever.
        """
        assert self.ticket is not None
        waited = False
        began = last_report = time.monotonic()
        announced: set[str] = set()
        index = 0
        while index < len(self.keys.keys):
            key = self.keys.keys[index]
            path = self.state / "projects" / project_lock_name(key)
            subject = key.split(":", 1)[1]
            ahead = self._earlier_waiters(key)
            fd = None if ahead else try_lock(path)
            if fd is not None:
                self._project_fds.append(fd)
                index += 1
                continue
            waited = True
            if self._project_fds and self._earlier_waiter_needs_held_keys(index):
                while self._project_fds:
                    unlock(self._project_fds.pop())
                index = 0
            if subject not in announced:
                announced.add(subject)
                holder = self._holder_of_project()
                self.report(
                    f"waiting for project {subject}"
                    + (f" (busy: {holder.preview(60)}, pid {holder.pid})" if holder else "")
                )
            elif time.monotonic() - last_report >= HEARTBEAT:
                last_report = time.monotonic()
                self.report(f"still waiting for project {subject} [{int(last_report - began)}s]")
            self._check_deadline(deadline, f"project {subject}")
            time.sleep(BEHIND_POLL if ahead else HEAD_POLL)
        if not waited:
            return
        # The directory may have moved or been re-pointed while this build waited: lock what is there now.
        current = resolve_project(str(self.keys.cwd), self.keys.explicit)
        if set(current.keys) - set(self.keys.keys):
            raise QueueError("the project directory or its Git work tree changed while waiting for its lock")

    def _earlier_waiter_needs_held_keys(self, index: int) -> bool:
        held = set(self.keys.keys[:index])
        return any(
            other.id < self.ticket.id and other.state == STATE_PROJECT and held.intersection(other.keys)
            for other in self._tickets()
        )

    def _earlier_waiters(self, key: str) -> bool:
        """True while a build that arrived earlier is still waiting for the same project key."""
        return any(
            other.id < self.ticket.id and other.state == STATE_PROJECT and key in other.keys
            for other in self._tickets()
        )

    def _tickets(self) -> list[Ticket]:
        """Live tickets, re-read only when the tickets folder changed, and at least every RESCAN seconds.

        File system clocks are coarse (a Linux tick is several ms, HFS+ keeps whole seconds), so a change made in the
        same tick as our last read keeps the old mtime. A read made that close to the stamp proves nothing, so the
        folder is read again: the "racy" rule git uses for its index. The periodic re-read notices an owner that died
        without its files being removed.
        """
        try:
            stamp = os.stat(self.state / "tickets").st_mtime_ns
        except OSError:
            stamp = None
        racy = self._seen is None or self._read_at - self._seen < RACY_NS
        if racy or stamp != self._seen or time.monotonic() - self._scanned >= RESCAN:
            self._read_at = time.time_ns()
            self._cache = live_tickets(self.state)
            self._seen, self._scanned = stamp, time.monotonic()
        return self._cache

    def _holder_of_project(self) -> Ticket | None:
        for other in live_tickets(self.state):
            if other.state == STATE_RUNNING and (
                other.cwd == str(self.keys.cwd) or other.project == self.keys.label
            ):
                return other
        return None

    def _acquire_slots(self, deadline: float | None) -> None:
        assert self.ticket is not None
        announced = None
        began = last_report = time.monotonic()
        while True:
            tickets = self._tickets()
            queued = [t for t in tickets if t.state == STATE_QUEUED]
            ahead = [t for t in queued if t.id < self.ticket.id]
            running = [t for t in tickets if t.state == STATE_RUNNING]
            if not ahead:
                # Slots first: memory is only worth reading (it costs a process on macOS) when one is free.
                taken = self._take_slots()
                if taken is None:
                    reason = f"all {self.slots} slots busy"
                else:
                    reason = self._memory_hold(running)
                    if reason is None:
                        self._slot_fds = [fd for _, fd in taken]
                        self.ticket.slots = [number for number, _ in taken]
                        return
                    for _, fd in taken:
                        unlock(fd)
            else:
                reason = f"position {len(ahead) + 1} in queue"
            now = time.monotonic()
            if reason != announced or now - last_report >= HEARTBEAT:
                busy = ", ".join(t.project.rstrip("/").rsplit("/", 1)[-1] for t in running[:4])
                waited = "" if reason != announced else f" [{int(now - began)}s]"
                self.report(f"waiting: {reason}{waited}" + (f" (running: {busy})" if busy else ""))
                announced, last_report = reason, now
            self._check_deadline(deadline, "a build slot")
            time.sleep(HEAD_POLL if not ahead else BEHIND_POLL)

    def _memory_hold(self, running: list[Ticket]) -> str | None:
        """Why this build should not start yet because memory is short. Never holds a build when none is running."""
        if not self.memory_floor or not running or self.memory_probe is None:
            return None
        now = time.monotonic()
        if self._memory_read is None or now - self._memory_read[0] >= MEMORY_CACHE:
            self._memory_read = (now, self.memory_probe())
        available = self._memory_read[1]
        if available is None or available >= self.memory_floor:
            return None
        return (
            f"memory low ({available / 1024**3:.1f} GiB free, need {self.memory_floor / 1024**3:.1f} GiB)"
        )

    def _take_slots(self) -> list[tuple[int, int]] | None:
        """weight free slots as (number, fd), or None (holding nothing) when fewer are free."""
        taken: list[tuple[int, int]] = []
        for number in range(self.slots):
            fd = try_lock(self.state / "slots" / f"slot-{number:03d}.lock")
            if fd is not None:
                taken.append((number, fd))
                if len(taken) == self.weight:
                    return taken
        for _, fd in taken:
            unlock(fd)
        return None


# ---------------------------------------------------------------- nesting and shared runs


def inside_live_run(state: Path) -> str | None:
    """The ticket id of the smart-build run this process is part of, when that run is still alive."""
    ticket_id = os.environ.get("SMART_BUILD_TICKET")
    if not ticket_id or not TICKET_ID.fullmatch(ticket_id):
        return None
    if os.environ.get("SMART_BUILD_STATE_DIR_RESOLVED") not in (None, str(state)):
        return None
    return ticket_id if is_locked(state / "tickets" / f"{ticket_id}.lock") else None


def read_ticket(state: Path, ticket_id: str) -> Ticket | None:
    if not TICKET_ID.fullmatch(ticket_id):
        return None
    return _read_ticket(state / "tickets" / f"{ticket_id}.json")


def run_log_paths(state: Path, ticket_id: str) -> tuple[Path, Path]:
    """Where a shared build's stdout and stderr are copied, for its followers to replay."""
    return state / "runs" / f"{ticket_id}.out", state / "runs" / f"{ticket_id}.err"


def run_result_path(state: Path, ticket_id: str) -> Path:
    return state / "runs" / f"{ticket_id}.result"


def write_run_result(state: Path, ticket_id: str, exit_code: int) -> None:
    _write_json_atomic(run_result_path(state, ticket_id), {"exit": exit_code, "time": time.time()})


def read_run_result(state: Path, ticket_id: str) -> int | None:
    try:
        return int(json.loads(run_result_path(state, ticket_id).read_text())["exit"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prune_runs(state: Path) -> None:
    cutoff = time.time() - RUN_RETENTION_SECONDS
    directory = state / "runs"
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return
    for entry in entries:
        try:
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            continue
