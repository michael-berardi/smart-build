---
name: smart-build
description: Run heavy builds, test suites, type checks, packaging and deploys through Smart Build, the machine-wide build queue, so parallel agents do not overload the machine or build the same repo at once. Use before running any command that takes more than about 30 seconds or a lot of CPU or memory (cargo build, next/astro/vite build, tsc on a large project, full test suites, docker build, deploy scripts). Do not use for dev servers, watchers or quick commands.
---

# Smart Build

Several agents on one machine tend to start builds at the same time. Smart Build makes them take turns: a few at
once machine-wide, one per repo, in arrival order. Output and exit status pass through unchanged.

## Setup (once per machine)

```sh
smart-build --version          # expect: smart-build 0.3.0 or newer
```

If it is missing, install it (needs Python 3.10+ on macOS or Linux; no other dependencies):

```sh
uv tool install "git+https://github.com/your-github-username/smart-build@v0.3.0"
# or: pipx install "git+https://github.com/your-github-username/smart-build@v0.3.0"
```

Replace `your-github-username` with the repository's GitHub owner (the address bar of the README shows it).

Without a permanent install: `uvx --from "git+https://github.com/your-github-username/smart-build@v0.3.0" smart-build -- <cmd>`.
If you cannot install it, run the command directly and say so; do not invent a substitute queue.

## Use

```sh
smart-build -- npm run build
smart-build --cwd path/to/app -- cargo test --release
smart-build --max-wait 20m --timeout 30m -- ./scripts/deploy.sh
```

- Your tool call's own timeout includes time spent waiting in the queue. Pass `--max-wait` shorter than that
  timeout so you get a clear exit 75 instead of being cut off.
- Lines on stderr starting with `smart-build: waiting` are normal. Run `smart-build status` to see what is running
  and queued before concluding that something hangs.
- Use `--coalesce` when other agents may request the identical build: it runs once and everyone gets the result.
  It pipes output, so the command cannot prompt.
- A `smart-build` call inside an already-wrapped command runs immediately; nesting is safe.

## Exit status

The command's own status, or: `75` no turn within `--max-wait` (retry later or report the busy queue), `124`
`--timeout` hit, `2` refused (the message says why), `126`/`127` the command could not run, `128+N` ended by
signal N.

## Do not

- Wrap dev servers, watchers or anything that never exits; they hold a slot forever.
- Pass a different `--slots` per call. Machine-wide limits belong in `SMART_BUILD_SLOTS`, set once.
- Kill builds you did not start to free a slot.
