# Smart Build

Run five builds at once on a laptop and they all get slower. Run twenty, which is what a few AI coding agents in
parallel will do, and the machine starts swapping. Two builds of the same checkout at the same time can also corrupt
each other's output.

Smart Build puts every heavy command on the machine in one fair queue. Prefix a build, test or deploy with
`smart-build --` and it:

- runs only a few builds at once across the whole machine (worked out from your CPUs and memory);
- runs one build per project at a time, keyed on the Git work tree, so a monorepo's apps never build over each other;
- starts waiting builds in the order they arrived;
- holds a new build while memory is short and another build is running;
- runs an identical build only once when two callers queue it (`--coalesce`);
- stops the whole command tree on Ctrl-C, SIGTERM or a timeout before the next build gets its slot, and within
  moments if Smart Build itself is killed.

There is no daemon and no setup. The queue is a folder of lock files, so a crashed build never leaves a slot held.
It is one small Python package with no dependencies, for macOS and Linux.

## Install

Requires Python 3.10 or newer on macOS or Linux.

```sh
pipx install "git+https://github.com/michael-berardi/smart-build@v0.1.0"
smart-build --version
```

Or run it from a checkout with no install: `PYTHONPATH=src python3 -m smart_build --help`.

## Use

```sh
smart-build -- npm run build
smart-build -- cargo build --release
smart-build --weight 2 --timeout 30m -- ./scripts/release.sh
```

Output and exit status pass straight through. On a terminal the command gets the foreground, so prompts, Ctrl-C and
Ctrl-\\ reach it directly and Ctrl-Z suspends the whole job. Signals you ignore (`nohup`, `&` in a script) stay
ignored. While it waits, Smart Build says why on stderr:

```text
smart-build: waiting: all 2 slots busy (running: web, api)
smart-build: starting after 41s (slot 1 of 2)
```

See what is running and waiting:

```text
$ smart-build status
2 of 2 slots busy, 1 queued, 9.6 GiB memory free
  running   1m12s  web                      npm run build  (pid 41207)
  running     18s  api                      cargo build --release  (pid 41388)
  #1           9s  docs                     npm run build  (pid 41455)
```

`smart-build stats` summarizes finished builds: how many waited, the median and 90th-percentile wait, and the
projects that used the most build time. Both take `--json`.

### Options

| Option | Default | What it does |
|---|---|---|
| `--slots N` | auto | Builds that may run at once on the machine: one per 4 CPUs and per 8 GiB of memory, between 1 and 8. |
| `--weight N` | 1 | Slots this build takes. Give a release build or a big link step 2. |
| `--project NAME` | none | An extra name to serialize on. The directory and its Git work tree are always used. |
| `--cwd DIR` | current | Directory to run the command in. |
| `--memory-floor SIZE` | auto | Hold a build while less memory than this is free and another build is running. Auto is 10% of memory, at most 4 GiB. `0` turns it off. |
| `--coalesce` | off | If an identical command for the same directory is already waiting, wait for it and share its output and exit status. |
| `--jobs auto\|N` | off | Set `CARGO_BUILD_JOBS`, `CMAKE_BUILD_PARALLEL_LEVEL` and `GOFLAGS=-p=N` for the command when they are unset. `auto` is this build's share of the CPUs. |
| `--timeout DURATION` | none | Stop the command after this long and exit 124. |
| `--max-wait DURATION` | none | Give up with exit 75 if the build has not started by then. `--no-wait` is `--max-wait 0`. |
| `--grace DURATION` | 10s | Time between SIGTERM and SIGKILL when stopping a command. |
| `--dry-run` | | Wait for a turn, report it and exit without running anything. |
| `-q`, `--quiet` | | Print only errors. |

Durations take `90`, `90s`, `30m` or `2h`. Sizes take `512M` or `4G`. Set machine-wide defaults with
`SMART_BUILD_SLOTS`, `SMART_BUILD_MEMORY_FLOOR` and `SMART_BUILD_COALESCE=1`. Every caller on the machine should agree
on the slot count, so set `SMART_BUILD_SLOTS` in your shell profile rather than passing different `--slots` values.

### Exit status

The command's own status. Otherwise `124` timed out, `75` no turn within `--max-wait`, `2` refused (the message says
why, for example a missing `--cwd`), `126`/`127` the command could not be run, and `128+N` when the command died of
signal N. When Smart Build itself is interrupted, or Ctrl-C ends the command, Smart Build ends by that same signal, so
a shell loop around it stops as it would without Smart Build.

## When to use it

Wrap commands that need a lot of CPU or memory for a while: production builds (`next build`, `astro build`,
`vite build`, `cargo build --release`), full test suites, large type checks, packaging and deploys. Do not wrap
dev servers or watchers. They never finish, so they would hold a slot forever.

Build scripts can call `smart-build` again inside a wrapped command. A nested call runs straight away inside its
parent's turn, taking no further locks, instead of queueing behind itself.

### Coalescing duplicate builds

Agents and editors often ask for the same build several times while one is already waiting. With `--coalesce`,
the second and later callers attach to the first identical command (same arguments, same directory) that has not
started yet. They print its output as it runs and exit with its status. A build that is already running is never
shared, because it may have started before your latest change. If the first caller interrupts its build, the
others run it themselves. Followers keep their own `--max-wait` and `--timeout`, and get stdout and stderr
separately.

With `--coalesce`, every build's output goes through a pipe and its stdin is closed, even when nobody joins it,
because a later caller may still join. Programs that color only on a terminal print plain text, and the build cannot
prompt. Leave it off for interactive work.

## Embedding

Tools written in Python can take a turn directly:

```python
from pathlib import Path
from smart_build.queue import Lease, default_state_dir, prepare_state_dir, resolve_project

state = prepare_state_dir(default_state_dir())
with Lease(state=state, keys=resolve_project("/path/to/repo", None), command=["deploy"], slots=2, max_wait=600):
    ...  # holds the project and a slot until the block ends
```

A wait that fails (`WaitTimeout` after `max_wait`, an interrupt) releases everything it held before raising.
From any other language, run your step as the command: `smart-build -- your-tool deploy`.

## Compared with other tools

| | Smart Build | GNU `sem` | `nq` | `pueue` / `task-spooler` |
|---|---|---|---|---|
| Runs in your shell, output and exit status unchanged | yes | with `--fg` | no (log file) | no (a daemon runs it) |
| Machine-wide cap on concurrent builds | yes | yes, per semaphore name | one at a time | yes, per queue or group |
| One build per project or Git work tree | yes | no | per queue directory | no |
| First come, first served | yes | not documented | yes | yes |
| Memory-aware start | yes | no | no | no |
| Shares duplicate queued builds | yes | no | no | no |
| Background daemon | none | none | none | required |

Use `pueue` or `task-spooler` when you want jobs to run detached from your terminal. Use Smart Build when the
caller, whether a person, a script or an AI agent, needs the command's output and exit status in its own shell.

## Benchmark

`bench/compare.py` runs the same workloads through Smart Build and any other wrapper. Against the in-house queue it
replaced, which was polling-based, on an Apple M4 Mac (10 CPUs, 24 GB) with 2 slots:

| Workload | Previous queue | Smart Build |
|---|---|---|
| 12 builds of 0.5 s arriving 50 ms apart: total time | 11.6 s | 3.9 s |
| Gap between one build ending and the next starting (mean) | 1,333 ms | 90 ms |
| Pairs of builds started out of arrival order | 8 | 0 |
| CPU used by 10 builds waiting for 8 s | 4.9 s | 1.5 s |
| Child process left running after SIGTERM to the wrapper | yes | no |
| Overhead of a run with nothing else queued | 165 ms | 85 ms |

Both tools ran the same workloads in one session on a busy machine. Timings vary between runs; the ordering and
cleanup results do not.

## How it works

Every invocation owns a ticket: a lock file it keeps locked for its whole life, and a small JSON record. Ticket names
start with an arrival number, so sorting them gives the queue order. A build first takes the lock for its project,
then waits until no earlier ticket is ahead of it and enough slot files are free to lock. The kernel releases all of
a process's locks when it exits, so the queue never needs cleaning after a crash.

The command runs in its own process group. A small guard process leads that group and watches a pipe from Smart
Build; if Smart Build dies, even by SIGKILL, the guard stops the group. More detail is in
[docs/DESIGN.md](docs/DESIGN.md).

State lives in `$XDG_STATE_HOME/smart-build` (normally `~/.local/state/smart-build`), created readable only by you;
Smart Build refuses a state folder that another user owns or can write to. Set
`SMART_BUILD_STATE_DIR` to move it. It must be on a local disk, because file locks are unreliable on network file
systems. Each user on a machine has their own queue.

## Privacy

Smart Build makes no network connections. It stores the commands it ran, their directories, timings and exit status
in its state folder (`events.jsonl`, kept under 16 MB). Builds run with `--coalesce` also keep their output there
for an hour so that followers can replay it. Nothing else is stored.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests   # macOS or Linux
./scripts/test-linux.sh                                # the same suite in Linux containers (needs Docker)
python3 bench/compare.py --baseline "other-queue --project {project} --"
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security problems as described in [SECURITY.md](SECURITY.md).

## License

MIT
