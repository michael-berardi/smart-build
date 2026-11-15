# Smart Build

A per-user queue for heavy builds and tests on macOS and Linux. Use it when several
terminals or coding agents share a machine: cap concurrent builds, serialize each
Git work tree, and share identical queued commands with `--coalesce`.

Python 3.10+; no runtime dependencies or daemon. Commands you do not wrap are not queued.
Windows users need WSL.

## Install

From a downloaded or cloned checkout, create a private environment and install:

```sh
python3 -m venv .venv && .venv/bin/python -m pip install .
.venv/bin/smart-build --version
.venv/bin/smart-build -- python3 -c 'print("build complete")'
```

Use `.venv/bin/smart-build` below, or activate the environment with
`. .venv/bin/activate`. Remove `.venv` to uninstall.
The build backend may require network access during installation.

Release assets also include `install.sh`, `install.ps1`, a wheel and `SHA256SUMS`.
The shell installer downloads and verifies the wheel and creates a private venv;
re-running it updates the install. The PowerShell script only explains the WSL route
and returns an error. See [the installer](scripts/install.sh) for location overrides.

Without installing: `PYTHONPATH=src python3 -m smart_build --help`.

## Use

```sh
smart-build -- npm run build
smart-build -- cargo build --release
smart-build --weight 2 --timeout 30m -- ./scripts/release.sh
smart-build status
smart-build stats --since 24h
```

Output and exit status pass through. Status lists running and waiting builds;
stats summarizes completed builds and waits of at least one second. Both accept
`--json`. An even-sized set of waits uses the average of its middle pair as the median.
`--since 0` includes no events older than the current instant.

On a terminal, prompts and job-control signals reach the command. Interrupts and
timeouts stop its process group before releasing the slot. Detached daemons are not
stopped. A guard also stops the group if the wrapper dies; after SIGKILL a slot may
be reused before that cleanup finishes.

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

## Agents and embedding

Copy [the skill](skills/smart-build/SKILL.md) into your agent's skills folder, or tell
it to prefix heavy commands with `smart-build --` and keep `--max-wait` shorter than
its own tool timeout. Do not wrap dev servers or watchers.

Python callers can use `smart_build.queue.Lease` as a context manager with a prepared
state directory and project keys from `resolve_project`. See [the design](docs/DESIGN.md)
for locking, nesting, process supervision and limits.

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
PYTHONPATH=src python3 -m unittest discover -s tests
./scripts/test-linux.sh       # requires Docker; may start Colima
./scripts/test-install.sh     # macOS, Ubuntu and PowerShell; requires Docker
python3 bench/compare.py      # isolated synthetic workloads
```

Tests use temporary state directories. Set `TMPDIR` to keep fixtures in a chosen
location. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

MIT
