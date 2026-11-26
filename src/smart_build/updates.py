"""Release checks and verified updates for Smart Build itself.

The command line imports this module for `status`, `--version` and `update` only. A wrapped build never
checks and never prints a notice; once it has run, it only removes an old rollback copy. The check is one
anonymous GET to the public "latest release" record of the repository the package declares. Its result,
the skipped version and the automatic-update choice live in one small private cache file.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__

CHECK_INTERVAL = 24 * 60 * 60
CHECK_TIMEOUT = 4.0
DOWNLOAD_TIMEOUT = 60.0
CLAIM_SECONDS = 60  # a check started this recently by another command is not started again
LOG_LIMIT = 256 * 1024
EXIT_UPDATE_AVAILABLE = 10
USER_AGENT = "smart-build-update-check"
VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
AGENT_ENV = ("CLAUDECODE", "CODEX_SANDBOX", "PI_AGENT", "ANTIGRAVITY", "DSH_AGENT", "SMART_BUILD_AGENT")
DEFAULTS = {
    "checkedAt": 0, "claimedAt": 0, "latest": None, "error": None, "skipped": None,
    "shownVersion": None, "shownAt": 0, "autoCheck": True, "autoInstall": False, "asked": False, "autoError": None,
}


class UpdateError(RuntimeError):
    """An update check or install that did not happen, with the reason in the message."""


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    assets: dict[str, str]  # asset name -> download URL


@dataclass(frozen=True)
class InstallKind:
    name: str  # "installer" (install.sh), "uv", "pipx" or "other"
    home: Path | None = None


# ---------------------------------------------------------------- versions and source


def parse_version(text) -> tuple[int, int, int] | None:
    """Stable x.y.z (an optional leading v) only; prereleases and anything else are None."""
    match = VERSION.fullmatch(str(text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(current, latest) -> bool:
    old, new = parse_version(current), parse_version(latest)
    return bool(old and new and new > old)


def notes_preview(notes: str, limit: int = 6) -> list[str]:
    lines = [line.strip()[:100] for line in str(notes or "").splitlines()]
    return [line for line in lines if line][:limit]


def repository_url() -> str | None:
    """The GitHub repository the package declares: installed metadata, or a source checkout's pyproject.toml."""
    import importlib.metadata

    try:
        meta = importlib.metadata.metadata("smart-build")
        urls = [entry.split(",", 1)[-1] for entry in meta.get_all("Project-URL") or []] + [meta.get("Home-page") or ""]
    except importlib.metadata.PackageNotFoundError:
        try:
            text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        except OSError:
            text = ""
        urls = re.findall(r'^\s*Homepage\s*=\s*"([^"]+)"', text, re.MULTILINE)
    for url in urls:
        match = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?", url.strip())
        if match:
            return f"https://github.com/{match.group(1)}/{match.group(2)}"
    return None


def release_api() -> str | None:
    override = os.environ.get("SMART_BUILD_UPDATE_API", "").strip()  # a local release server, for tests
    if override:
        return override
    repo = repository_url()
    return repo.replace("https://github.com/", "https://api.github.com/repos/", 1) + "/releases/latest" if repo else None


def _cause(exc: BaseException) -> str:
    text = str(getattr(exc, "reason", None) or exc) or type(exc).__name__
    return " ".join(text.split())[:160]


def fetch_release(timeout: float = CHECK_TIMEOUT) -> Release | None:
    """One anonymous GET: a fixed User-Agent and nothing else. None when there is no stable release to offer."""
    import urllib.error
    import urllib.request

    api = release_api()
    if not api:
        raise UpdateError("this copy declares no GitHub repository to check")
    request = urllib.request.Request(api, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            record = json.loads(response.read(1 << 20))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise UpdateError(f"HTTP {exc.code} {exc.reason}".strip()) from None
    except (OSError, ValueError) as exc:
        raise UpdateError(_cause(exc)) from None
    if not isinstance(record, dict):
        raise UpdateError("the release record is not a JSON object")
    version = parse_version(record.get("tag_name"))
    if record.get("draft") or record.get("prerelease") or version is None:
        return None
    assets = {
        str(entry["name"]): str(entry.get("browser_download_url") or "")
        for entry in record.get("assets") or []
        if isinstance(entry, dict) and entry.get("name")
    }
    return Release(".".join(map(str, version)), str(record.get("body") or ""), assets)


# ---------------------------------------------------------------- the cache file


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", "").strip() or ("~/Library/Caches" if sys.platform == "darwin" else "~/.cache")
    return Path(base).expanduser() / "smart-build"


def cache_file() -> Path:
    return cache_dir() / "update.json"


def log_file() -> Path:
    return cache_dir() / "update.log"


def load_state() -> dict:
    try:
        data = json.loads(cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {key: data.get(key, default) for key, default in DEFAULTS.items()}


def update_state(**changes) -> dict:
    """Re-read, change and atomically replace the cache file (mode 0600), so concurrent writers lose little."""
    import tempfile

    state = {**load_state(), **changes}
    path = cache_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".update.", dir=path.parent)  # mkstemp creates it 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=1, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return state


def checks_enabled(state: dict | None = None) -> bool:
    """Automatic checks and notices: off in CI, in agent hosts, inside a wrapped build and when opted out."""
    if os.environ.get("SMART_BUILD_NO_UPDATE_CHECK", "").strip() not in ("", "0"):
        return False
    if os.environ.get("CI", "").strip() or os.environ.get("SMART_BUILD_TICKET"):
        return False
    if any(os.environ.get(name, "").strip() for name in AGENT_ENV):
        return False
    return bool((state or load_state())["autoCheck"])


def on_terminal() -> bool:
    try:
        return sys.stdout.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def record_check(timeout: float = CHECK_TIMEOUT) -> Release | None:
    """Fetch now and record the outcome. A failure is recorded with its cause and raised."""
    try:
        release = fetch_release(timeout)
    except UpdateError as exc:
        update_state(checkedAt=time.time(), claimedAt=0, error=str(exc))
        raise UpdateError(f"update check failed: {exc}") from None
    update_state(checkedAt=time.time(), claimedAt=0, error=None, latest=release.version if release else None)
    return release


# ---------------------------------------------------------------- background check and notice


def _recent(stamp, seconds: float) -> bool:
    try:
        return 0 <= time.time() - float(stamp or 0) < seconds
    except (TypeError, ValueError):
        return False


def schedule_check() -> None:
    """Start the daily check in a detached, low-priority process; the command never waits for it."""
    try:
        state = load_state()
        if not (on_terminal() and checks_enabled(state)):
            return
        if _recent(state["checkedAt"], CHECK_INTERVAL) or _recent(state["claimedAt"], CLAIM_SECONDS):
            return
        update_state(claimedAt=time.time())
        import subprocess

        code = "import sys; sys.path.insert(0, sys.argv[1]); from smart_build.updates import worker; worker()"
        subprocess.Popen(
            [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parent.parent)],
            cwd=cache_dir(),
            env={**os.environ, "SMART_BUILD_NO_UPDATE_CHECK": "1"},  # nothing it starts checks again
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, preexec_fn=lambda: os.nice(10),
        )
    except Exception:  # a check is a convenience, never a reason a command fails
        pass


def worker() -> None:
    """The detached check: record the result, then install quietly when automatic updates are on."""
    try:
        release = record_check()
    except (UpdateError, OSError):
        return
    state = load_state()
    if not release or not is_newer(__version__, release.version) or not state["autoInstall"] or state["skipped"] == release.version:
        return
    try:
        with open(log_file(), "a", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as log:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} installing {release.version} over {__version__}", file=log, flush=True)
            install(release, out=log)
    except (UpdateError, OSError) as exc:
        update_state(autoError=f"{release.version}: {exc}")  # the notice falls back to asking
    else:
        update_state(autoError=None)
    finally:
        trim_log(log_file())


def trim_log(path: Path, limit: int = LOG_LIMIT) -> None:
    """Keep the automatic-update log to its newest `limit` bytes."""
    try:
        if path.stat().st_size > limit:
            with open(path, "rb") as handle:
                handle.seek(-limit, os.SEEK_END)
                tail = handle.read()
            path.write_bytes(tail)
    except OSError:
        pass


def notice_lines(current: str, state: dict) -> list[str]:
    """The two-line notice, or nothing: not newer, skipped, or already shown for this version today."""
    latest = state["latest"]
    if not is_newer(current, latest) or state["skipped"] == latest:
        return []
    if state["shownVersion"] == latest and _recent(state["shownAt"], CHECK_INTERVAL):
        return []
    command = upgrade_command(install_kind(), latest)
    first = f"smart-build {latest} available (you have {current}) · run: {' '.join(command) if command else 'smart-build update'}"
    if state["autoInstall"]:
        return [first, "skip: smart-build update --skip"]
    return [first, "turn on automatic updates: smart-build update --auto on · skip: smart-build update --skip"]


def show_notice(current: str = __version__, stream=None) -> list[str]:
    """After status or --version on a terminal: the notice on stderr, at most once a day per version."""
    stream = stream or sys.stderr
    try:
        state = load_state()
        if not (on_terminal() and checks_enabled(state)):
            return []
        lines = notice_lines(current, state)
        if lines:
            update_state(shownVersion=state["latest"], shownAt=time.time())
    except Exception:
        return []
    dim = os.environ.get("TERM", "") not in ("", "dumb") and "NO_COLOR" not in os.environ
    for index, line in enumerate(lines):
        print(f"\033[2m{line}\033[0m" if dim and index else line, file=stream, flush=True)
    return lines


# ---------------------------------------------------------------- install kinds, install and rollback


def installer_home() -> Path | None:
    """The install.sh home folder when this process runs from its venv (`<home>/venv`)."""
    try:
        home = Path(os.environ.get("SMART_BUILD_HOME") or "~/.smart-build").expanduser().resolve()
        return home if Path(sys.prefix).resolve() == home / "venv" else None
    except (OSError, RuntimeError):
        return None


def install_kind() -> InstallKind:
    home = installer_home()
    if home is not None:
        return InstallKind("installer", home)
    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").is_file():
        return InstallKind("uv")
    if (prefix / "pipx_metadata.json").is_file():
        return InstallKind("pipx")
    return InstallKind("other")


def upgrade_command(kind: InstallKind, version: str) -> list[str] | None:
    """The package manager's own command for uv and pipx installs; None when `smart-build update` does it."""
    repo = repository_url()
    if kind.name not in ("uv", "pipx") or not repo:
        return None
    source = f"git+{repo}@v{version}"
    return ["uv", "tool", "install", "--force", source] if kind.name == "uv" else ["pipx", "install", "--force", source]


def _venv_version(venv: Path) -> str | None:
    for info in venv.glob("lib/python*/site-packages/smart_build-*.dist-info"):
        return info.name[len("smart_build-"):-len(".dist-info")]
    return None


def _download(url: str) -> bytes:
    import urllib.request

    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            return response.read()
    except (OSError, ValueError) as exc:
        raise UpdateError(f"download failed: {_cause(exc)}") from None


def checksum_for(name: str, sums: str) -> str | None:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def _run(command: list[str], out, env: dict | None = None, name: str = "") -> None:
    import subprocess

    if out is not None:
        out.flush()
    try:
        code = subprocess.call(command, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env)
    except OSError as exc:
        raise UpdateError(f"cannot run {command[0]}: {exc.strerror or exc}") from None
    if code:
        raise UpdateError(f"{name or ' '.join(command[:3])} failed with exit status {code}")


def refuse_unmanaged(kind: InstallKind) -> None:
    if kind.name == "other":
        repo = repository_url() or "the project's release page"
        raise UpdateError(
            "this copy was not installed by install.sh, uv or pipx, so it is not replaced; update it the way you "
            f"installed it, or run: curl -fsSL {repo}/releases/latest/download/install.sh | sh"
        )


def install(release: Release, out=None, kind: InstallKind | None = None) -> None:
    """Install a release with the package manager that owns this copy, or with the release's verified install.sh."""
    import hashlib
    import hmac
    import tempfile

    kind = kind or install_kind()
    refuse_unmanaged(kind)
    command = upgrade_command(kind, release.version)
    if command:
        _run(command, out)
        return
    script_url, sums_url = release.assets.get("install.sh"), release.assets.get("SHA256SUMS")
    if not script_url or not sums_url:
        raise UpdateError(f"release {release.version} has no install.sh or SHA256SUMS asset")
    with tempfile.TemporaryDirectory(prefix="smart-build-update-") as folder:
        sums = _download(sums_url).decode("utf-8", "replace")
        script = _download(script_url)
        expected = checksum_for("install.sh", sums)
        if not expected or not hmac.compare_digest(hashlib.sha256(script).hexdigest(), expected):
            raise UpdateError("install.sh does not match the release's SHA256SUMS; nothing was installed")
        path = Path(folder) / "install.sh"
        path.write_bytes(script)
        env = {
            **os.environ,
            "SMART_BUILD_HOME": str(kind.home),
            "SMART_BUILD_VERSION": release.version,  # the installer refuses a wheel of any other version
            "SMART_BUILD_RELEASE_BASE": script_url.rsplit("/", 1)[0],
            "SMART_BUILD_NO_UPDATE_CHECK": "1",
        }
        _run(["/bin/sh", str(path)], out, env=env, name="the installer")  # verifies the wheel's SHA-256, then swaps, keeping venv.previous


def rollback() -> str:
    """Swap back the venv the installer kept as venv.previous; returns the restored version."""
    import shutil

    kind = install_kind()
    if kind.name != "installer":
        raise UpdateError("--rollback works only for copies installed by install.sh")
    venv, previous, failed = kind.home / "venv", kind.home / "venv.previous", kind.home / "venv.failed"
    if not previous.is_dir():
        raise UpdateError("no previous version is kept; it is removed once a new version has run")
    restored = _venv_version(previous) or "the previous version"
    shutil.rmtree(failed, ignore_errors=True)
    os.replace(venv, failed)
    try:
        os.replace(previous, venv)
    except OSError:
        os.replace(failed, venv)
        raise
    shutil.rmtree(failed, ignore_errors=True)
    update_state(skipped=__version__)  # neither offer nor auto-install the version just rolled back
    return restored


def settle_previous() -> None:
    """Once a new version has run a build or status, remove the rollback copy the installer kept."""
    home = installer_home()
    if home is None:
        return
    previous = home / "venv.previous"
    try:
        if previous.is_dir() and _venv_version(previous) != __version__:  # an old process keeps its own copy
            import shutil

            shutil.rmtree(previous, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------- `smart-build update`


def ask(question: str, default: bool) -> bool:
    try:
        answer = input(question).strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in ("y", "yes")


def check_command(current: str = __version__) -> int:
    try:
        release = record_check()
    except UpdateError as exc:
        print(f"smart-build: {exc}", file=sys.stderr)
        return 1
    if release and is_newer(current, release.version):
        print(f"smart-build {release.version} available (you have {current})")
        for line in notes_preview(release.notes):
            print(f"  {line}")
        return EXIT_UPDATE_AVAILABLE
    print(f"smart-build {current} is current")
    return 0


def skip_command(current: str = __version__) -> int:
    latest = load_state()["latest"]
    if not is_newer(current, latest):
        release = record_check()
        latest = release.version if release else None
    if not is_newer(current, latest):
        print(f"smart-build {current} is current; nothing to skip")
        return 0
    update_state(skipped=latest)
    print(f"skipped {latest}; a newer release will show again")
    return 0


def auto_command(value: str) -> int:
    if value == "status":
        state = load_state()
        print(f"automatic updates: {'on' if state['autoInstall'] else 'off'}")
        print(f"automatic checks: {'on' if checks_enabled(state) else 'off'}")
        if state["autoError"]:
            print(f"last automatic install failed: {state['autoError']}")
        if state["error"]:
            print(f"last check failed: {state['error']}")
        return 0
    update_state(autoInstall=value == "on", asked=True, autoError=None)
    if value == "off":
        log_file().unlink(missing_ok=True)
    print(f"automatic updates {value}")
    return 0


def checks_command(value: str) -> int:
    update_state(autoCheck=value == "on")
    print(f"automatic checks {value}")
    return 0


def update_command(current: str = __version__) -> int:
    release = record_check()
    if not release or not is_newer(current, release.version):
        print(f"smart-build {current} is current")
        return 0
    print(f"smart-build {current} → {release.version}")
    for line in notes_preview(release.notes):
        print(f"  {line}")
    kind = install_kind()
    refuse_unmanaged(kind)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and not ask(f"Install {release.version} now? [Y/n] ", True):
        return 0
    install(release, out=sys.stdout, kind=kind)
    print(f"smart-build {release.version} is installed; your next command uses it")
    state = load_state()
    if interactive and not state["autoInstall"] and not state["asked"]:
        update_state(autoInstall=ask("Install future updates automatically? [y/N] ", False), asked=True)
    return 0
