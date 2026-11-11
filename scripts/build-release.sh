#!/bin/sh
# Builds the Smart Build release assets into <out> (default dist/release):
#   smart_build-<version>-py3-none-any.whl   the package (no dependencies), installed by install.sh
#   install.sh, install.ps1                  the installers
#   SHA256SUMS                               checksums of the three files above
# Usage: scripts/build-release.sh [out-dir]   (run from a clean checkout of the release tag)
# Needs python3 (3.10+) with venv support and network access for pip's build backend; the install itself
# needs no network. SOURCE_DATE_EPOCH fixes the timestamps inside the wheel so a commit builds the same bytes.
set -eu
cd "$(dirname "$0")/.."
OUT="${1:-dist/release}"
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "build-release: the working tree has changes; build from a clean checkout" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "build-release: needs python3 (3.10+)" >&2; exit 1; }
commit=$(git rev-parse HEAD)
stage=$(mktemp -d "${TMPDIR:-/tmp}/smart-build-release.XXXXXX")
trap 'rm -rf "$stage"' EXIT
python3 -m venv "$stage/venv" || { echo "build-release: python3 -m venv failed (Debian/Ubuntu: apt-get install python3-venv)" >&2; exit 1; }
export PIP_CACHE_DIR="$stage/pip-cache"
SOURCE_DATE_EPOCH=0 "$stage/venv/bin/python3" -m pip wheel --no-deps --disable-pip-version-check --quiet --wheel-dir "$stage/dist" . \
  || { echo "build-release: building the wheel failed (pip needs network access for the build backend)" >&2; exit 1; }
set -- "$stage/dist/"smart_build-*-py3-none-any.whl
[ -f "$1" ] || { echo "build-release: no wheel was built" >&2; exit 1; }
[ "$#" -eq 1 ] || { echo "build-release: more than one wheel was built" >&2; exit 1; }
WHEEL=$(basename "$1")
rm -rf "$OUT"; mkdir -p "$OUT"
mv "$1" "$OUT/$WHEEL"
cp scripts/install.sh scripts/install.ps1 "$OUT/"
( cd "$OUT" && if command -v sha256sum >/dev/null 2>&1; then sha256sum install.ps1 install.sh "$WHEEL"; else shasum -a 256 install.ps1 install.sh "$WHEEL"; fi > SHA256SUMS )
echo "built $OUT for $commit"
ls -l "$OUT"
