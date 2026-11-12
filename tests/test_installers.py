"""Static checks on the installers (the behavioural ones live in scripts/test-install.sh)."""
import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class InstallerStaticTests(unittest.TestCase):
    def test_ps1_never_exits(self):
        # `exit` under `irm | iex` closes the user's own PowerShell window.
        text = (SCRIPTS / "install.ps1").read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"(?im)^\s*exit\b", code))
        self.assertIn("throw ", code)

    def test_ps1_body_is_wrapped_in_a_function(self):
        text = (SCRIPTS / "install.ps1").read_text()
        self.assertRegex(text, r"(?m)^function Install-SmartBuild \{")
        self.assertNotIn("$ErrorActionPreference", text)

    def test_sh_escapes_sed_paths_and_marks_shim(self):
        text = (SCRIPTS / "install.sh").read_text()
        self.assertNotIn('sed "s|$NEWPATH|$VENV|g"', text)
        self.assertIn("# smart-build-installer-shim", text)
        self.assertIn("not written by this installer", text)


if __name__ == "__main__":
    unittest.main()
