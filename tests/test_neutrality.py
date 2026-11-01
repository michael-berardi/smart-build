"""Neutrality test for Smart Build's shipped files.

Smart Build is a generic build queue. Its source, docs, skill and benchmark ship no
built-in model, provider or fallback choice and no person's, client's or project's
names: those belong to the operator and the machine, never to the tool. This test
keeps it that way, modelled on Carl's neutrality test.

Not scanned, by design:
- LICENSE and pyproject.toml keep legal attribution (copyright holder and package
  author metadata). They are attribution, not shipped product behaviour.
- tests/ holds synthetic fixtures and test data, not shipped product text.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Shipped product text. scripts/ and changelog/legal files are attribution or repo
# plumbing; the leaf's shipped set is source, skill, docs, benchmark and README.
SHIPPED_DIRS = ("src/smart_build", "skills", "docs", "bench")
SHIPPED_FILES = ("README.md",)

# Concrete model ids and paid-route markers that only an operator should choose.
# A provider-adapter module, if one is ever added, may name its own provider's
# catalog but must not set defaults; list it in ADAPTERS with a comment explaining
# why its names are the provider's own catalog. There is no adapter code today.
MODEL_CHOICE = re.compile(
    r"gpt-[56][a-z0-9.-]*|deepseek[a-z0-9.-]*|opencode-go/|openrouter/|:exacto|"
    r"mimo-v[0-9][a-z0-9.-]*|glm-[0-9][a-z0-9.-]*|"
    r"claude-[a-z0-9.-]*-5-5|(haiku|sonnet|opus)[ -]?5[.-]5",
    re.IGNORECASE,
)
ADAPTERS: tuple[str, ...] = ()

# Names of the people and organisations this tool was first built for: never in the
# shipped tool. Case-insensitive so a lowercase handle cannot slip through.
PERSONAL = re.compile(
    r"Mike|Berardi|Dana|Felix|acme|acme|Mouseia|Liberty Design|"
    r"libertydesign|acme|/Users/",
    re.IGNORECASE,
)

# Documented allowlist: {relative path: {line number: reason}}. Empty today; any
# entry added later must say why the match is acceptable (for example a product's
# technical identity such as a bundle id).
ALLOWED: dict[str, dict[int, str]] = {}


def shipped_files() -> list[Path]:
    """Every shipped file the neutrality scan covers, in a stable order."""
    paths: list[Path] = []
    for name in SHIPPED_FILES:
        path = ROOT / name
        if path.is_file():
            paths.append(path)
    for name in SHIPPED_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if str(path.relative_to(ROOT)) in ADAPTERS:
                continue
            paths.append(path)
    return paths


class NeutralityTests(unittest.TestCase):
    def _scan(self, pattern: re.Pattern[str]) -> list[str]:
        hits: list[str] = []
        for path in shipped_files():
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if lineno in ALLOWED.get(rel, {}):
                    continue
                for match in pattern.finditer(line):
                    hits.append(f"{rel}:{lineno}: {match.group(0)!r} in {line.strip()[:120]}")
        return hits

    def test_shipped_files_are_present(self) -> None:
        missing = [name for name in SHIPPED_FILES if not (ROOT / name).is_file()]
        missing += [name for name in SHIPPED_DIRS if not (ROOT / name).is_dir()]
        self.assertEqual(missing, [], "Neutrality scan cannot see shipped files: " + ", ".join(missing))

    def test_shipped_files_have_no_model_or_provider_choice(self) -> None:
        hits = self._scan(MODEL_CHOICE)
        self.assertEqual(hits, [], "Built-in model or provider choices found in shipped files:\n" + "\n".join(hits))

    def test_shipped_files_have_no_personal_names(self) -> None:
        hits = self._scan(PERSONAL)
        self.assertEqual(hits, [], "Personal or project names found in shipped files:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
