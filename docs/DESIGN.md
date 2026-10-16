# Design

Smart Build is a queue made of files. Each piece is chosen so that a crash, a kill or a reboot can never leave the
queue stuck, and so that waiting costs almost nothing.

## State directory

```text
slots/slot-000.lock ...       one empty file per slot; a running build holds `weight` of them locked
projects/<name>-<hash>.lock   one per project key
sequence                      the arrival counter
tickets/<id>.lock, <id>.json  one pair per live invocation
runs/<id>.out, .err, .result  output and exit status of a shared (--coalesce) build, removed after an hour
events.jsonl                  queued, started, following, abandoned and finished events, rotated at 8 MB
```

All locks are `flock(2)` locks. The kernel releases them when the process that took them exits, however it exits.
Lock files are opened with `O_NOFOLLOW`, and a lock taken on a file that was replaced in the meantime is dropped and
taken again, so a replaced or symlinked lock file cannot split one lock into two.

## Tickets and order

A ticket id is `<arrival number>-<pid>-<random>`, so sorting ids gives arrival order. The arrival number comes from
a counter file bumped under its own lock, not from the clock: on a busy VM the wall clock can step backwards between
CPUs. If the counter is lost, numbering resumes after the newest live ticket. The owner keeps
`<id>.lock` locked for its whole life. Anyone who can lock it has proved the owner dead and removes the pair while
holding that lock, so a ticket being created at that moment is never removed.

A build moves through these states:

1. `waiting-project`: taking its project locks in a fixed global order (sorted by lock file name), which rules out
   deadlock between builds that share some of their keys. A build only tries a key when no earlier ticket is still
   waiting for that same key, so builds of one project also start in arrival order.
2. `queued`: holding its project, waiting for slots. It may take slots only when no queued ticket with an earlier id
   exists. A build that needs two slots therefore cannot be starved by a stream of one-slot builds.
3. `running`: holding its project and slots until the command and its process group are gone.

Waiting for a project does not hold a slot, so a build blocked behind another build of the same project never keeps
an unrelated project waiting.

The head of the queue retries every 50 ms; others every 200 ms, and they re-read tickets only when the tickets folder
changed. Because file system clocks are coarse (a Linux tick is several milliseconds, HFS+ keeps whole seconds), a
read made within 1.1 s of the folder's stamp proves nothing and is repeated: the "racy" rule git uses for its index.
Every waiter also re-reads at least once a second, to notice an owner that died without its files being removed.

## Project keys

A build locks every key that applies: `dir:<canonical working directory>`, `dir:<Git work tree>` (the nearest folder
up the tree with a `.git` entry, which covers worktrees and submodules), and `label:<--project>` when given. On macOS
the directory is spelled the way the kernel spells it (`F_GETPATH`), so `~/Dev/app` and `~/dev/app` on a
case-insensitive disk are one project. If a build had to wait for its project, the keys are worked out again before it
continues; a directory that moved or was re-pointed while it waited is refused.

## Memory gate

When it is a build's turn, Smart Build reads available memory (`MemAvailable` on Linux; free, inactive, speculative
and purgeable pages from `vm_stat` on macOS). If it is under the floor and at least one other build is running, the
build waits. A build is never held when nothing else is running, so a machine that is short of memory for other
reasons still makes progress.

## Running the command

The command runs in a new process group led by a small guard process (`_guard.py`, started with `python -I -S`). The
guard forks the command, restores the signal defaults Python changes (SIGPIPE, SIGXFSZ), and waits.

On a terminal, the group gets the foreground, so prompts, Ctrl-C and Ctrl-\ reach the command directly. When Ctrl-Z
stops the command, Smart Build takes the terminal back and stops its own job (SIGTSTP to its process group), so the
shell sees the job stopped; `fg` gives the terminal back to the command, `bg` leaves it with the shell. Without a
terminal there is no job control: a stopped command waits for whoever stopped it.

SIGTERM, SIGINT and SIGHUP sent to Smart Build are passed to the group, followed by SIGKILL after the grace period.
`--timeout` does the same and exits 124. If the group itself is told to stop (Ctrl-C reaches the group, not Smart
Build), the guard stops whatever the command left behind in the group before it exits. Either way the slot is
released only after the group is gone. After a normal exit, processes the command left running are not touched.

Signals the caller ignores (`nohup`, `&` in a non-interactive shell) stay ignored by Smart Build, the guard and the
command. When Smart Build is interrupted by a signal, or the command dies of SIGINT, Smart Build releases its lease
and then ends by that signal, so a shell loop around it behaves as it would without Smart Build.

The guard holds the read end of a pipe whose write end only Smart Build has. If Smart Build is killed outright, the
pipe reads end-of-file and the guard sends SIGTERM to the group, then SIGKILL after the grace period. A command that
moves itself into another session (a daemon) has chosen to outlive its build and is left alone.

## Nesting

The command receives `SMART_BUILD_TICKET`. A `smart-build` started inside it whose parent ticket is still locked runs
immediately, inside the parent's turn, and takes no locks: taking another project's lock while holding a slot could
deadlock against a build that holds that project and waits for a slot.

## Coalescing

With `--coalesce`, a ticket records a key: the SHA-256 of its directory and arguments. A new caller with the same key
looks for an earlier ticket with that key that has not started yet. If it finds one, it follows it: it replays that
build's output and exits with the status in `runs/<id>.result`. A running build is never
followed, because it may predate the caller's latest change. A follower that finds its leader has itself become a
follower switches to that build. Output is kept as `runs/<id>.out` and `runs/<id>.err` and replayed to the matching
stream. If the followed build dies, is interrupted by its own
caller or times out, it leaves no result, and the follower queues and runs the command itself.

## Known limits

- One queue per user. Users on a shared machine do not see each other's builds.
- The state directory must be on a local file system; `flock` is unreliable on network file systems.
- Builds started without Smart Build are invisible to it, except through the memory gate.
- When Smart Build is SIGKILLed, the kernel frees its slot and project locks at once; the guard stops the command a
  moment later. Another build can start in that moment.
- A nested call whose environment was cleared cannot see its parent's ticket and queues behind it.
