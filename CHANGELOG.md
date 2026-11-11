# Changelog

## Unreleased

- One-step installers. `scripts/install.sh` (macOS and Linux) downloads the release wheel, verifies its
  SHA-256 before installing anything, and installs it into a private venv under `~/.smart-build` with the
  `smart-build` command in `~/.local/bin`. No sudo, no shell profile edits, no dependencies fetched; re-running
  updates in place, and it finishes by running `smart-build --version`.
- Windows: Smart Build needs POSIX file locks and process-group signals, so there is no native Windows build.
  `scripts/install.ps1` explains that it runs under WSL, prints the one-step WSL command, and exits non-zero.
- `scripts/build-release.sh` builds the release assets (wheel, both installers, SHA256SUMS) deterministically;
  `scripts/test-install.sh` tests the installers on macOS, in an Ubuntu 24.04 container, and under PowerShell 7.
- README: the Install section leads with the one-liners and covers what is installed, where, updating and removing.

## 0.2.0 (2026-11-01)

- Neutrality: install instructions in the README and agent skill use a neutral `your-github-username` placeholder
  instead of a person's GitHub handle, so the shipped text names no person or project. The tool itself remains a
  generic build queue with no built-in model, provider or fallback choice.
- New `tests/test_neutrality.py`, modelled on Carl's neutrality test, scans the shipped source, skill, docs,
  benchmark and README for model ids and personal names and fails if one returns. LICENSE and package metadata
  keep legal attribution and are excluded, as are tests' synthetic fixtures.
- The Linux container test script now copies the shipped docs and benchmark alongside `src` and `tests`, so the
  neutrality scan runs in the container suite too.

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
