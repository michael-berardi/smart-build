# Privacy

Smart Build has no telemetry, no account and no identifier.

## Network

A wrapped build (`smart-build -- <command>`) makes no network connections of its own.

The update check is the only request Smart Build makes by itself. It is one anonymous GET of the
public latest-release record of the repository in the package metadata, on GitHub. The request carries a fixed
User-Agent (`smart-build-update-check`) and nothing else: no identifier, no installed version, no usage or
machine data. As with any web request, GitHub sees your IP address. The check runs at most once every 24 hours,
and only after `smart-build status` or `smart-build --version` on a terminal. It never runs in CI, in coding
agents or inside a wrapped build. To turn it off, set `SMART_BUILD_NO_UPDATE_CHECK=1` or run
`smart-build update --checks off`.

`smart-build update`, and automatic updates if you turn them on, download the release's `install.sh`,
`SHA256SUMS` and wheel from GitHub. For uv and pipx installs, the package manager fetches the release instead.

## Files on your machine

- The state folder (`$XDG_STATE_HOME/smart-build`, normally `~/.local/state/smart-build`) keeps the commands
  Smart Build ran, with their directories, timings and exit status (`events.jsonl`, kept under 16 MB).
  Builds run with `--coalesce` also keep their output there for an hour, so that followers can replay it.
- The cache folder (`smart-build/update.json` under `$XDG_CACHE_HOME`, `~/Library/Caches` or `~/.cache`)
  keeps the last update check and its result, the skipped version and your two update switches, in a 0600 file.
  With automatic updates on, a log of the last installs sits beside it, capped at 256 KB.
- An install.sh install keeps the previous version as `~/.smart-build/venv.previous` until the new version
  has run once.

Nothing else is stored. [docs/updates.md](docs/updates.md#files) shows how to remove all of it.
