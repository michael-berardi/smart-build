# Updates

<!-- update-contract: v1 -->

Smart Build tells you when a newer release exists and updates in one step. A wrapped build
(`smart-build -- <command>`) never checks and never prints a notice, so builds are never delayed and their
output is never mixed with update text.

## The notice

On a terminal, after `smart-build status` or `smart-build --version`, Smart Build may print two lines on
standard error:

```text
smart-build 0.5.0 available (you have 0.4.0) · run: smart-build update
turn on automatic updates: smart-build update --auto on · skip: smart-build update --skip
```

The second line is dimmed. A version's notice shows at most once a day. Skipped versions stay quiet until a
newer one comes out. There is no notice with `--json`, or when standard output or standard error is not a terminal.
For uv and pipx installs, `run:` shows the package manager's command.

## Commands

```sh
smart-build update               # show current → new and the release notes, ask, install
smart-build update --check       # report only: exit 0 current, 10 update available, 1 check failed (with the cause)
smart-build update --skip        # no notice for this version
smart-build update --auto on     # install future updates automatically (off by default)
smart-build update --auto off
smart-build update --auto status # the setting, and the last failure if any
smart-build update --checks off  # no daily check and no notice
smart-build update --rollback    # go back to the version before the last update (install.sh installs)
```

On a terminal, `update` asks `Install 0.5.0 now? [Y/n]`. After the first install it asks once
`Install future updates automatically? [y/N]` and remembers the answer. When it is not on a terminal,
`update` installs without asking, because running the command is the consent.

## The check

The check is one anonymous GET of the public latest-release record of the repository in the package metadata
(`https://api.github.com/repos/<owner>/<repo>/releases/latest`). It sends a fixed User-Agent
(`smart-build-update-check`) and nothing else: no identifier, no installed version, no usage or machine data.
It runs at most once every 24 hours, in a detached low-priority process with a 4-second timeout. A failure
is recorded with its cause, shown by `update --check` and `update --auto status`, and not retried until
the next day. Only stable `x.y.z` releases newer than yours count. Drafts and prereleases are ignored.

Automatic checks are off when standard output or standard error is not a terminal, when `CI` is set, inside a
coding agent or a wrapped build, with `SMART_BUILD_NO_UPDATE_CHECK=1`, and after `smart-build update --checks off`.

## Automatic updates

Automatic updates are off by default. When they are on and the daily check finds a new release, the
background process installs it the same way `smart-build update` does. Your next command uses the new version.
Its output goes to one log file that is capped at 256 KB. If an automatic install fails, the current
version keeps working, `update --auto status` shows why, and the notice appears again.

## How each install is updated

- **install.sh** (`~/.smart-build`): `update` downloads the release's `install.sh` and `SHA256SUMS` and checks
  the installer's SHA-256 before running it. The installer checks the wheel's SHA-256 before it changes
  anything. Then it swaps the new venv in and keeps the old one as `venv.previous` for `update --rollback`.
  The new version deletes that copy after its first build or `status`, so there is never more than one.
  A rollback also skips the version you rolled back from. If you installed with a custom `SMART_BUILD_HOME`,
  set it when you run `update`.
- **uv / pipx**: `update` runs `uv tool install --force git+<repository>@v<new>` (or the `pipx install --force`
  equivalent), so your files stay with the package manager. Use the manager to go back.
- **Anything else** (a source checkout, a pip install into your own venv): `update` replaces nothing and tells
  you how to update.

Downloads go to a temporary folder that is deleted whether the install succeeds or fails.

## Files

The only file the check writes is `smart-build/update.json` in your cache folder (`$XDG_CACHE_HOME`,
otherwise `~/Library/Caches` on macOS or `~/.cache` on Linux). It is mode 0600 and holds the last check time
and result, the skipped version, the notice time and the two switches. With automatic updates on,
`update.log` sits beside it, and `update --auto off` deletes that log. To remove Smart Build completely:

```sh
rm -rf ~/.smart-build ~/.local/bin/smart-build ~/.local/state/smart-build
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/smart-build" ~/Library/Caches/smart-build
```

See [PRIVACY.md](../PRIVACY.md) for everything Smart Build stores.
