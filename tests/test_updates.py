"""Update checks, notices, preferences, installs and their bounds: a local release server, no network."""

from __future__ import annotations

import contextlib
import email.message
import hashlib
import importlib.metadata
import io
import json
import os
try:
    import pty
except ImportError:  # a non-POSIX host cannot allocate a terminal
    pty = None
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smart_build import __version__, updates  # noqa: E402

FAKE_INSTALLER = b"""#!/bin/sh
printf '%s\\n' "$SMART_BUILD_VERSION" "$SMART_BUILD_RELEASE_BASE" "$SMART_BUILD_NO_UPDATE_CHECK" > "$SMART_BUILD_HOME/installed"
echo "installer ran"
exit "${FAKE_INSTALLER_EXIT:-0}"
"""
NEWER = "9.9.9"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        server = self.server
        server.requests.append((self.path, dict(self.headers)))
        if self.path == "/release":
            body, status = json.dumps(server.record).encode(), server.status
        elif self.path.startswith("/assets/") and self.path[8:] in server.files:
            body, status = server.files[self.path[8:]], 200
        else:
            body, status = b"not found", 404
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeRelease:
    """A local stand-in for the GitHub latest-release record and its assets."""

    def __init__(self, tag=f"v{NEWER}", status=200, draft=False, prerelease=False, script=FAKE_INSTALLER, sums=None):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests, self.server.status = [], status
        base = f"http://127.0.0.1:{self.server.server_port}"
        digest = hashlib.sha256(script).hexdigest()
        self.server.files = {"install.sh": script, "SHA256SUMS": sums or f"{digest}  install.sh\n".encode()}
        self.server.record = {
            "tag_name": tag, "draft": draft, "prerelease": prerelease,
            "body": "Faster queue wakes\n\nFixed a lock race\n",
            "assets": [{"name": name, "browser_download_url": f"{base}/assets/{name}"} for name in self.server.files],
        }
        self.api = base + "/release"
        self.base = base + "/assets"

    @property
    def requests(self):
        return self.server.requests

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["SMART_BUILD_UPDATE_API"] = self.api
        return self

    def __exit__(self, *_args):
        os.environ.pop("SMART_BUILD_UPDATE_API", None)
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="smart-build-updates-test-")).resolve()
        self.saved = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("SMART_BUILD") or key in (
                "CI", "TERM", "NO_COLOR", "XDG_CACHE_HOME", *updates.AGENT_ENV,
            ) or key.lower().endswith("_proxy"):
                del os.environ[key]
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ.update(
            XDG_CACHE_HOME=str(self.tmp / "cache"),
            SMART_BUILD_STATE_DIR=str(self.tmp / "state"),
            SMART_BUILD_MEMORY_FLOOR="0",
            SMART_BUILD_HOME=str(self.home),
        )
        terminal = patch.object(updates, "on_terminal", return_value=True)
        terminal.start()
        self.addCleanup(terminal.stop)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @contextlib.contextmanager
    def env(self, **values):
        saved = {key: os.environ.get(key) for key in values}
        os.environ.update({key: value for key, value in values.items() if value is not None})
        for key in (key for key, value in values.items() if value is None):
            os.environ.pop(key, None)
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def known(self, latest=NEWER, checked=None, **extra):
        updates.update_state(latest=latest, checkedAt=time.time() if checked is None else checked, **extra)

    @contextlib.contextmanager
    def installer_copy(self):
        """Pretend this process runs from the install.sh venv under self.home."""
        (self.home / "venv").mkdir(exist_ok=True)
        with patch.object(updates.sys, "prefix", str(self.home / "venv")):
            yield

    @contextlib.contextmanager
    def capture(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err


def make_venv(path: Path, version: str) -> None:
    (path / "lib" / "python3.12" / "site-packages" / f"smart_build-{version}.dist-info").mkdir(parents=True)


class VersionTests(unittest.TestCase):
    def test_newer_equal_older_garbage_prerelease(self):
        self.assertEqual(updates.parse_version("v1.2.3"), (1, 2, 3))
        self.assertTrue(updates.is_newer("0.4.0", "0.4.1"))
        self.assertTrue(updates.is_newer("0.4.0", "v0.10.0"))  # numeric, not text, order
        self.assertFalse(updates.is_newer("0.4.0", "0.4.0"))
        self.assertFalse(updates.is_newer("0.4.0", "0.3.9"))
        for garbage in ("0.5.0-rc.1", "v0.5", "latest", "", None, "1.2.3.4"):
            self.assertFalse(updates.is_newer("0.4.0", garbage), garbage)
        self.assertFalse(updates.is_newer("garbage", "9.9.9"))


class CheckTests(Base):
    def test_one_anonymous_get_with_fixed_user_agent(self):
        with FakeRelease() as fake:
            self.assertEqual(updates.record_check().version, NEWER)
        self.assertEqual(len(fake.requests), 1)
        path, headers = fake.requests[0]
        self.assertEqual(path, "/release")  # no query: no id, version or machine data
        self.assertEqual(headers["User-Agent"], "smart-build-update-check")
        self.assertLessEqual(set(headers), {"Host", "User-Agent", "Accept", "Accept-Encoding", "Connection"})

    def test_release_source_comes_from_installed_metadata(self):
        meta = email.message.Message()
        meta["Project-URL"] = "Homepage, https://github.com/example-owner/example-tool"
        with patch.object(importlib.metadata, "metadata", return_value=meta):
            self.assertEqual(updates.release_api(), "https://api.github.com/repos/example-owner/example-tool/releases/latest")
        self.assertTrue(updates.release_api().endswith("/smart-build/releases/latest"))  # the checkout's pyproject.toml

    def test_cache_is_one_private_file(self):
        with FakeRelease():
            updates.record_check()
        self.known()
        updates.show_notice("0.4.0", io.StringIO())
        with self.capture():
            updates.skip_command("0.4.0")
            updates.auto_command("on")
            updates.checks_command("off")
        self.assertEqual(sorted(p.name for p in (self.tmp / "cache" / "smart-build").iterdir()), ["update.json"])
        self.assertEqual(updates.cache_file().stat().st_mode & 0o777, 0o600)
        self.assertLess(updates.cache_file().stat().st_size, 1024)

    def test_failure_is_recorded_with_its_cause(self):
        with FakeRelease(status=503), self.capture() as (out, err):
            self.assertEqual(updates.check_command("0.4.0"), 1)
            self.assertIn("update check failed: HTTP 503", err.getvalue())
            self.assertIn("HTTP 503", updates.load_state()["error"])
            updates.auto_command("status")
            self.assertIn("last check failed: HTTP 503", out.getvalue())
        with FakeRelease(), self.capture():
            updates.check_command("0.4.0")
        self.assertIsNone(updates.load_state()["error"])

    def test_check_exit_codes(self):
        with FakeRelease(), self.capture() as (out, _err):
            self.assertEqual(updates.check_command("0.4.0"), updates.EXIT_UPDATE_AVAILABLE)
            self.assertEqual(updates.check_command(NEWER), 0)
        self.assertIn(f"smart-build {NEWER} available (you have 0.4.0)\n  Faster queue wakes\n", out.getvalue())
        self.assertIn(f"smart-build {NEWER} is current", out.getvalue())

    def test_drafts_prereleases_and_bad_tags_are_not_newer(self):
        for options in ({"draft": True}, {"prerelease": True}, {"tag": "v10.0.0-rc1"}, {"tag": "nightly"}):
            with self.subTest(options=options), FakeRelease(**options), self.capture() as (out, _err):
                self.assertEqual(updates.check_command("0.4.0"), 0)
                self.assertIn("is current", out.getvalue())
                self.assertIsNone(updates.load_state()["latest"])

    def test_schedule_is_daily_and_never_doubled(self):
        with patch("subprocess.Popen") as popen:
            updates.schedule_check()
            updates.schedule_check()  # claimed a moment ago by the first
            self.assertEqual(popen.call_count, 1)
            self.known(checked=time.time() - 3600)
            updates.schedule_check()
            self.assertEqual(popen.call_count, 1)
            self.known(checked=time.time() - 25 * 3600, claimedAt=0)
            updates.schedule_check()
            self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args.kwargs["env"]["SMART_BUILD_NO_UPDATE_CHECK"], "1")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_no_check_off_terminal_in_ci_agents_builds_or_opted_out(self):
        cases = [{"CI": "true"}, {"SMART_BUILD_NO_UPDATE_CHECK": "1"}, {"CLAUDECODE": "1"}, {"SMART_BUILD_TICKET": "x"}]
        with patch("subprocess.Popen") as popen:
            for overrides in cases:
                with self.subTest(overrides=overrides), self.env(**overrides):
                    updates.schedule_check()
            with patch.object(updates, "on_terminal", return_value=False):
                updates.schedule_check()
            with self.capture():
                updates.checks_command("off")
            updates.schedule_check()
        popen.assert_not_called()


class NoticeTests(Base):
    def test_text_once_a_day_per_version_skip_and_newer(self):
        self.known()
        err = io.StringIO()
        self.assertEqual(updates.show_notice("0.4.0", err), [
            f"smart-build {NEWER} available (you have 0.4.0) · run: smart-build update",
            "turn on automatic updates: smart-build update --auto on · skip: smart-build update --skip",
        ])
        self.assertEqual(updates.show_notice("0.4.0", err), [])  # shown today
        updates.update_state(shownAt=time.time() - 25 * 3600)
        self.assertEqual(len(updates.show_notice("0.4.0", err)), 2)  # a day later
        updates.update_state(shownAt=0, skipped=NEWER)
        self.assertEqual(updates.show_notice("0.4.0", err), [])
        self.known(latest="9.10.0")
        self.assertIn("9.10.0", updates.show_notice("0.4.0", err)[0])
        self.assertEqual(updates.show_notice("9.10.0", err), [])

    def test_silent_off_terminal_in_ci_agents_builds_and_opted_out(self):
        self.known()
        for overrides in ({"CI": "1"}, {"SMART_BUILD_NO_UPDATE_CHECK": "1"}, {"CLAUDECODE": "1"}, {"SMART_BUILD_TICKET": "x"}):
            with self.subTest(overrides=overrides), self.env(**overrides):
                self.assertEqual(updates.show_notice("0.4.0", io.StringIO()), [])
        with patch.object(updates, "on_terminal", return_value=False):
            self.assertEqual(updates.show_notice("0.4.0", io.StringIO()), [])
        updates.update_state(autoCheck=False)
        self.assertEqual(updates.show_notice("0.4.0", io.StringIO()), [])

    def test_dim_second_line_and_auto_on_wording(self):
        self.known()
        err = io.StringIO()
        with self.env(TERM="xterm-256color"):
            updates.show_notice("0.4.0", err)
        self.assertIn("\n\033[2mturn on automatic updates", err.getvalue())
        self.known(shownAt=0, autoInstall=True)
        err = io.StringIO()
        with self.env(TERM="xterm-256color", NO_COLOR="1"):
            self.assertEqual(updates.show_notice("0.4.0", err)[1], "skip: smart-build update --skip")
        self.assertNotIn("\033[", err.getvalue())

    def test_notice_names_the_package_manager_command(self):
        self.known()
        prefix = self.tmp / "uv-tool"
        prefix.mkdir()
        (prefix / "uv-receipt.toml").write_text("[tool]\n")
        with patch.object(updates.sys, "prefix", str(prefix)):
            line = updates.show_notice("0.4.0", io.StringIO())[0]
        self.assertIn("run: uv tool install --force git+https://github.com/", line)
        self.assertTrue(line.endswith(f"/smart-build@v{NEWER}"), line)


class PreferenceTests(Base):
    def test_auto_defaults_off_and_persists(self):
        self.assertFalse(updates.load_state()["autoInstall"])
        self.assertTrue(updates.load_state()["autoCheck"])
        with self.capture() as (out, _err):
            updates.auto_command("on")
            self.assertTrue(updates.load_state()["autoInstall"])
            updates.auto_command("status")
            updates.log_file().write_text("old log\n")
            updates.auto_command("off")
            updates.auto_command("status")
        self.assertFalse(updates.load_state()["autoInstall"])
        self.assertTrue(updates.load_state()["asked"])  # never asked again once chosen
        self.assertFalse(updates.log_file().exists())  # off removes the background log
        self.assertIn("automatic updates: on\n", out.getvalue())
        self.assertIn("automatic updates: off\n", out.getvalue())

    def test_update_asks_on_a_terminal_and_offers_auto_once(self):
        answers, asked = iter(["", "n", "", "n"]), []
        out = type("Terminal", (io.StringIO,), {"isatty": lambda self: True})()
        with FakeRelease(), self.installer_copy(), patch.object(updates, "install") as install, \
                patch.object(updates.sys, "stdin", out), contextlib.redirect_stdout(out), \
                patch("builtins.input", side_effect=lambda question: (asked.append(question), next(answers))[1]):
            self.assertEqual(updates.update_command("0.4.0"), 0)  # Enter = yes; then "n" to automatic
            self.assertEqual(updates.update_command("0.4.0"), 0)  # Enter = yes; never asked about automatic again
            self.assertEqual(updates.update_command("0.4.0"), 0)  # "n": not installed
        self.assertEqual(asked, [f"Install {NEWER} now? [Y/n] ", "Install future updates automatically? [y/N] ",
                                 f"Install {NEWER} now? [Y/n] ", f"Install {NEWER} now? [Y/n] "])
        self.assertEqual(install.call_count, 2)
        self.assertFalse(updates.load_state()["autoInstall"])
        self.assertTrue(out.getvalue().startswith(f"smart-build 0.4.0 → {NEWER}\n  Faster queue wakes\n"))

    def test_skip(self):
        self.known()
        with self.capture() as (out, _err):
            updates.skip_command("0.4.0")
        self.assertEqual(updates.load_state()["skipped"], NEWER)
        self.assertIn(f"skipped {NEWER}", out.getvalue())
        with FakeRelease(), self.capture() as (out, _err):
            self.known(latest=None)
            updates.skip_command(NEWER)
        self.assertIn("nothing to skip", out.getvalue())


class InstallTests(Base):
    def setUp(self):
        super().setUp()
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        scratch = patch.object(tempfile, "tempdir", str(self.scratch))
        scratch.start()
        self.addCleanup(scratch.stop)

    def release(self, fake):
        return updates.Release(NEWER, "", {name: f"{fake.base}/{name}" for name in ("install.sh", "SHA256SUMS")})

    def run_install(self, fake, **env):
        log = self.tmp / "install.log"
        with self.installer_copy(), self.env(**env), open(log, "w") as out:
            try:
                updates.install(self.release(fake), out=out)
            finally:
                self.assertEqual(list(self.scratch.iterdir()), [], "the download folder was left behind")
        return log.read_text()

    def test_verified_installer_runs_with_the_release_version_and_base(self):
        with FakeRelease() as fake:
            self.assertIn("installer ran", self.run_install(fake))
        version, base, quiet = (self.home / "installed").read_text().split()
        self.assertEqual((version, base, quiet), (NEWER, fake.base, "1"))

    def test_checksum_mismatch_refuses_before_running_anything(self):
        with FakeRelease(sums=b"0" * 64 + b"  install.sh\n") as fake:
            with self.assertRaisesRegex(updates.UpdateError, "does not match"):
                self.run_install(fake)
        self.assertFalse((self.home / "installed").exists())

    def test_failed_installer_is_reported_and_cleaned_up(self):
        with FakeRelease() as fake:
            with self.assertRaisesRegex(updates.UpdateError, "exit status 3"):
                self.run_install(fake, FAKE_INSTALLER_EXIT="3")

    def test_rollback_restores_the_previous_copy_and_skips_the_new_one(self):
        make_venv(self.home / "venv", __version__)
        make_venv(self.home / "venv.previous", "0.3.2")
        with self.installer_copy():
            self.assertEqual(updates.rollback(), "0.3.2")
            self.assertEqual(updates._venv_version(self.home / "venv"), "0.3.2")
            self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["venv"])
            self.assertEqual(updates.load_state()["skipped"], __version__)
            with self.assertRaisesRegex(updates.UpdateError, "no previous version"):
                updates.rollback()
        with self.assertRaisesRegex(updates.UpdateError, "only for copies installed by install.sh"):
            updates.rollback()

    def test_rollback_copy_goes_once_the_new_version_has_run(self):
        make_venv(self.home / "venv.previous", __version__)
        with self.installer_copy():
            updates.settle_previous()  # this process is the old copy: keep it
            self.assertTrue((self.home / "venv.previous").exists())
            shutil.rmtree(self.home / "venv.previous")
            make_venv(self.home / "venv.previous", "0.3.2")
            updates.settle_previous()
        self.assertFalse((self.home / "venv.previous").exists())
        make_venv(self.home / "venv.previous", "0.3.2")
        updates.settle_previous()  # not running from the installer venv: never touch it
        self.assertTrue((self.home / "venv.previous").exists())

    def test_install_kinds(self):
        for marker, kind, command in (
            ("uv-receipt.toml", "uv", ["uv", "tool", "install", "--force"]),
            ("pipx_metadata.json", "pipx", ["pipx", "install", "--force"]),
            (None, "other", None),
        ):
            with self.subTest(kind=kind):
                prefix = self.tmp / kind
                prefix.mkdir()
                if marker:
                    (prefix / marker).write_text("{}")
                with patch.object(updates.sys, "prefix", str(prefix)):
                    found = updates.install_kind()
                self.assertEqual(found.name, kind)
                got = updates.upgrade_command(found, "1.2.3")
                self.assertEqual(got and got[:-1], command)
                if command:
                    self.assertRegex(got[-1], r"^git\+https://github\.com/[\w.-]+/smart-build@v1\.2\.3$")
        with self.installer_copy():
            self.assertEqual(updates.install_kind(), updates.InstallKind("installer", self.home))
        with patch.object(updates.sys, "prefix", str(self.tmp / "other")):
            with self.assertRaisesRegex(updates.UpdateError, "not installed by install.sh, uv or pipx"):
                updates.install(updates.Release(NEWER, "", {}))

    def test_background_auto_install_logs_and_falls_back_on_failure(self):
        updates.update_state(autoInstall=True)
        with FakeRelease(), self.installer_copy(), self.env(FAKE_INSTALLER_EXIT="4"):
            updates.worker()
        state = updates.load_state()
        self.assertRegex(state["autoError"], rf"^{NEWER}: .*exit status 4")
        self.assertTrue(updates.show_notice("0.4.0", io.StringIO())[0].startswith(f"smart-build {NEWER} available ("))
        with FakeRelease(), self.installer_copy():
            updates.worker()
        self.assertIsNone(updates.load_state()["autoError"])
        self.assertIn("installer ran", updates.log_file().read_text())
        self.assertEqual(updates.log_file().stat().st_mode & 0o777, 0o600)

    def test_background_check_never_installs_when_auto_is_off(self):
        with FakeRelease() as fake, self.installer_copy():
            updates.worker()
        self.assertEqual([path for path, _ in fake.requests], ["/release"])
        self.assertFalse((self.home / "installed").exists())
        self.assertFalse(updates.log_file().exists())

    def test_log_is_bounded(self):
        log = self.tmp / "update.log"
        log.write_bytes(b"x" * (2 * updates.LOG_LIMIT) + b"newest line\n")
        updates.trim_log(log)
        self.assertEqual(log.stat().st_size, updates.LOG_LIMIT)
        self.assertTrue(log.read_bytes().endswith(b"newest line\n"))


@unittest.skipUnless(pty is not None, "needs a pseudo-terminal")
class TerminalTests(Base):
    def run_tty(self, *args):
        master, slave = pty.openpty()
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "TERM": "xterm"}
        proc = subprocess.Popen([sys.executable, "-m", "smart_build", *args], cwd=self.tmp, env=env,
                                stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        chunks = []
        with contextlib.suppress(OSError):
            while data := os.read(master, 65536):
                chunks.append(data)
        os.close(master)
        return proc.wait(timeout=30), b"".join(chunks).decode("utf-8", "replace")

    def test_wrapped_build_never_checks_or_prints(self):
        self.known(checked=0)  # a check is due
        before = updates.cache_file().read_bytes()
        with FakeRelease() as fake:
            code, text = self.run_tty("--", sys.executable, "-c", "print('BUILD_ONLY')")
            time.sleep(0.5)
        self.assertEqual((code, text.strip()), (0, "BUILD_ONLY"))
        self.assertEqual(fake.requests, [])
        self.assertEqual(updates.cache_file().read_bytes(), before)

    def test_status_and_version_show_the_notice_after_their_output(self):
        self.known()
        code, text = self.run_tty("status")
        self.assertEqual(code, 0, text)
        self.assertLess(text.index("slots busy"), text.index(f"smart-build {NEWER} available (you have {__version__})"))
        updates.update_state(shownAt=0)
        code, text = self.run_tty("--version")
        self.assertEqual(code, 0, text)
        self.assertTrue(text.startswith(f"smart-build {__version__}\r\nsmart-build {NEWER} available"), text)

    def test_status_starts_one_background_check(self):
        with FakeRelease() as fake:
            code, text = self.run_tty("status")
            deadline = time.monotonic() + 15
            while updates.load_state()["latest"] != NEWER and time.monotonic() < deadline:
                time.sleep(0.1)
        self.assertEqual(code, 0, text)
        self.assertNotIn("available", text)  # nothing known yet: the check runs after the command
        self.assertEqual(updates.load_state()["latest"], NEWER)
        self.assertEqual([path for path, _ in fake.requests], ["/release"])

    def test_json_output_has_no_notice(self):
        self.known()
        code, text = self.run_tty("status", "--json")
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text)["version"], __version__)
        self.assertIsNone(updates.load_state()["shownVersion"])


if __name__ == "__main__":
    unittest.main()
