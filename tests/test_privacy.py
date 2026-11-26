"""Privacy regressions: no telemetry path in the tool, private local state by default."""

from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smart_build import cli, queue  # noqa: E402

# Importing any of these in shipped runtime code would add a network or analytics path.
FORBIDDEN_MODULES = {
    "socket", "urllib", "http", "requests", "httpx", "aiohttp", "urllib3",
    "sentry_sdk", "posthog", "segment", "mixpanel", "analytics", "amplitude",
    "datadog", "opentelemetry", "telemetry",
}
FORBIDDEN_ENDPOINTS = (
    "sentry.io", "posthog.com", "segment.io", "mixpanel.com", "analytics.google.com", "datadoghq.com",
)
SECRETS = ("secret-value-xyz", "abc-secret", "pw-secret")


class NoTelemetryTests(unittest.TestCase):
    def test_runtime_source_has_no_network_or_telemetry_path(self) -> None:
        hits: list[str] = []
        for path in sorted((ROOT / "src" / "smart_build").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    names = []
                for name in names:
                    if name in FORBIDDEN_MODULES:
                        hits.append(f"{path.name}:{node.lineno}: imports {name}")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text = node.value.lower()
                    for endpoint in FORBIDDEN_ENDPOINTS:
                        if endpoint in text:
                            hits.append(f"{path.name}:{node.lineno}: endpoint {endpoint}")
                    if "http://" in text or "https://" in text:
                        hits.append(f"{path.name}:{node.lineno}: hard-coded URL")
        self.assertEqual(hits, [], "Runtime code gained a network or telemetry path:\n" + "\n".join(hits))

    def test_no_runtime_dependencies_declared(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        project = text.split("[project]", 1)[1].split("\n[", 1)[0]
        self.assertNotRegex(project, r"(?m)^\s*dependencies\s*=", "A runtime dependency could ship its own telemetry")

    def test_dependency_telemetry_is_off_by_default_without_overriding_the_caller(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            env = cli.child_environment("00000000000000000001-1-abcdef", Path("/tmp/state"), 2, 1, "off", None)
        self.assertEqual({name: env[name] for name in cli.TELEMETRY_OFF}, cli.TELEMETRY_OFF)
        with patch.dict(os.environ, {"DO_NOT_TRACK": "0"}, clear=True):
            self.assertEqual(cli.private_environment()["DO_NOT_TRACK"], "0")


class StorageTests(unittest.TestCase):
    def run_build(self, state: Path) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if not key.startswith("SMART_BUILD")}
        env.update(
            PYTHONPATH=str(ROOT / "src"),
            SMART_BUILD_STATE_DIR=str(state),
            SMART_BUILD_MEMORY_FLOOR="0",
        )
        command = [
            sys.executable, "-m", "smart_build", "--slots", "2", "--", "true",
            "--token", SECRETS[0], f"API_KEY={SECRETS[1]}", "--url", f"https://user:{SECRETS[2]}@example.test/x",
        ]
        return subprocess.run(command, cwd=state.parent, env=env, capture_output=True, text=True, timeout=60)

    def test_state_records_hide_secrets_and_are_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="smart-build-privacy-") as directory:
            state = Path(directory) / "state"
            result = self.run_build(state)
            self.assertEqual(result.returncode, 0, result.stderr)
            files = [path for path in state.rglob("*") if path.is_file()]
            self.assertTrue(files, "the build wrote no state")
            for path in files:
                content = path.read_bytes()
                for secret in SECRETS:
                    self.assertNotIn(secret.encode(), content, f"{path.name} contains a command secret")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0, f"{path.name} is not private")
            for folder in (state, *(path for path in state.rglob("*") if path.is_dir())):
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode) & 0o077, 0, f"{folder} is not private")

    def test_redaction_does_not_hide_the_next_option(self) -> None:
        self.assertEqual(
            queue.redact_command(["deploy", "--token", SECRETS[0], "--verbose"]),
            ["deploy", "--token", "***", "--verbose"],
        )


if __name__ == "__main__":
    unittest.main()
