# Changelog

## 0.1.1 (2026-10-16)

- Agent skill (`skills/smart-build/SKILL.md`) and an `AGENTS.md` / `CLAUDE.md` snippet in the README, so coding
  agents know when to use Smart Build, how to install it if it is missing, and how to read waits and exit codes.
- `uv tool install` and `uvx` documented as install routes alongside `pipx`.

## 0.1.0 (2026-10-16)

First public release.

- Machine-wide build slots, sized from CPUs and memory, with per-build weights.
- One build per project: the directory, its Git work tree and an optional `--project` name.
- Strict first-come-first-served order.
- Memory gate: holds a new build while memory is short and another build is running.
- `--coalesce`: identical queued builds run once and share output and exit status.
- Process-group supervision: the command gets the terminal, Ctrl-Z suspends the whole job, Ctrl-C ends a shell loop,
  SIGTERM and `--timeout` stop the whole group, and a guard process stops the build if Smart Build itself is killed.
- Signals the caller ignores (`nohup`, `&` in a script) stay ignored.
- `status` and `stats`, with `--json`.
- Nested calls run inside their parent's turn.
