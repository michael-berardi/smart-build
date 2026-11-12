#!/bin/sh
# Installer tests for Smart Build. Builds the release, serves it from a local folder, then checks:
#   macOS: install into a temp HOME, version, re-run upgrade, corrupted wheel refused, missing prerequisite named
#   Linux: the same inside ubuntu:24.04 (Docker, --rm), plus the repo's own test suite
#   Windows script: install.ps1 under PowerShell 7 (mcr.microsoft.com/powershell on Linux) prints the WSL
#     route and exits non-zero (Smart Build has no native Windows build); PSScriptAnalyzer when it installs.
# Needs python3, curl and docker; uses shellcheck when available. Usage: scripts/test-install.sh
# Environment: SMART_BUILD_TEST_PORT (default 18473) for the local release server.
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PORT="${SMART_BUILD_TEST_PORT:-18473}"
BASE_GOOD="http://127.0.0.1:$PORT/good"
BASE_BAD="http://127.0.0.1:$PORT/bad"

say() { printf '%s\n' "$*"; }
fail() { printf 'test-install: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "needs python3"
command -v curl >/dev/null 2>&1 || fail "needs curl"
command -v docker >/dev/null 2>&1 || fail "needs docker (the Linux and PowerShell tests run in containers)"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck scripts/install.sh scripts/build-release.sh || fail "shellcheck reported problems"
else
  say "shellcheck not found; skipping lint"
fi

scripts/build-release.sh "$ROOT/dist/release"

# The workspace lives under dist/ (gitignored): the Docker VM shares the checkout's path, not $TMPDIR.
WORK=$(mktemp -d "$ROOT/dist/test-install.XXXXXX")
SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$WORK/www/good" "$WORK/www/bad"
cp "$ROOT/dist/release/"* "$WORK/www/good/"
cp "$ROOT/dist/release/"* "$WORK/www/bad/"
for f in "$WORK/www/bad/"smart_build-*.whl; do printf 'corrupted' >> "$f"; done

if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then fail "port $PORT is already in use"; fi
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$WORK/www" >/dev/null 2>&1 &
SERVER_PID=$!
i=0
until curl -fsS -o /dev/null "$BASE_GOOD/SHA256SUMS"; do
  i=$((i + 1)); [ "$i" -lt 30 ] || fail "the local release server did not start on port $PORT"
  sleep 1
done

say "== macOS: install, version, re-run upgrade"
TH="$WORK/home-mac"; mkdir -p "$TH"
HOME="$TH" SMART_BUILD_RELEASE_BASE="$BASE_GOOD" /bin/sh scripts/install.sh
V=$("$TH/.local/bin/smart-build" --version) || fail "the installed command does not run"
case "$V" in smart-build\ *) ;; *) fail "unexpected version output: $V" ;; esac
HOME="$TH" SMART_BUILD_RELEASE_BASE="$BASE_GOOD" /bin/sh scripts/install.sh
"$TH/.local/bin/smart-build" --version >/dev/null || fail "the command is broken after a re-run"

say "== macOS: home path with | & and backslash, marker shim, foreign shim left alone, 0.3.0 shim adopted"
TH5="$WORK/home-weird"; mkdir -p "$TH5"
WEIRD="$TH5/a|b&c\\d"
HOME="$TH5" SMART_BUILD_HOME="$WEIRD/tool" SMART_BUILD_BIN_DIR="$WEIRD/bin" SMART_BUILD_RELEASE_BASE="$BASE_GOOD" /bin/sh scripts/install.sh
"$WEIRD/bin/smart-build" --version >/dev/null || fail "install under a path with | & and backslash is broken"
"$WEIRD/tool/venv/bin/smart-build" --version >/dev/null || fail "venv script shebang is broken under a special path"
grep -qF "# smart-build-installer-shim" "$WEIRD/bin/smart-build" || fail "the shim carries no marker"
HOME="$TH5" SMART_BUILD_HOME="$WEIRD/tool" SMART_BUILD_BIN_DIR="$WEIRD/bin" SMART_BUILD_RELEASE_BASE="$BASE_GOOD" /bin/sh scripts/install.sh >/dev/null \
  || fail "re-running over our own marked shim failed"
# 0.3.0 shim: no marker, execs into the tool home
VREAL=$(cd "$WEIRD/tool" && pwd -P)
printf '#!/bin/sh\nexec "%s/venv/bin/smart-build" "$@"\n' "$VREAL" > "$WEIRD/bin/smart-build"
HOME="$TH5" SMART_BUILD_HOME="$WEIRD/tool" SMART_BUILD_BIN_DIR="$WEIRD/bin" SMART_BUILD_RELEASE_BASE="$BASE_GOOD" /bin/sh scripts/install.sh >/dev/null
grep -qF "# smart-build-installer-shim" "$WEIRD/bin/smart-build" || fail "a 0.3.0 shim was not adopted"
# foreign file
printf '#!/bin/sh\necho mine\n' > "$WEIRD/bin/smart-build"
out=$(HOME="$TH5" SMART_BUILD_HOME="$WEIRD/tool" SMART_BUILD_BIN_DIR="$WEIRD/bin" SMART_BUILD_RELEASE_BASE="$BASE_GOOD" /bin/sh scripts/install.sh 2>&1) \
  || fail "a foreign smart-build made the install fail: $out"
case "$out" in *"Left "*"alone"*) ;; *) fail "no one-line notice for a foreign smart-build: $out" ;; esac
[ "$("$WEIRD/bin/smart-build")" = "mine" ] || fail "a foreign smart-build was overwritten"

say "== macOS: corrupted wheel refused, nothing installed"
TH2="$WORK/home-mac-bad"; mkdir -p "$TH2"
if out=$(HOME="$TH2" SMART_BUILD_RELEASE_BASE="$BASE_BAD" /bin/sh scripts/install.sh 2>&1); then
  fail "a corrupted wheel was accepted"
fi
case "$out" in *"checksum mismatch"*) ;; *) fail "wrong error for a corrupted wheel: $out" ;; esac
[ ! -e "$TH2/.smart-build/venv" ] || fail "the corrupted install left a venv behind"
[ ! -e "$TH2/.local/bin/smart-build" ] || fail "the corrupted install left a shim behind"

say "== macOS: missing python3 is named"
FAKE="$WORK/fakebin"; mkdir -p "$FAKE"
for t in curl shasum awk mktemp rm mkdir mv cat chmod grep sed head cut uname dirname; do
  p=$(command -v "$t") || fail "test needs $t on PATH"
  ln -s "$p" "$FAKE/$t"
done
TH3="$WORK/home-mac-nopy"; mkdir -p "$TH3"
if out=$(HOME="$TH3" PATH="$FAKE" SMART_BUILD_RELEASE_BASE="$BASE_GOOD" /bin/sh scripts/install.sh 2>&1); then
  fail "the install succeeded without python3"
fi
case "$out" in *"Python 3.10"*) ;; *) fail "the missing-python error does not name Python 3.10: $out" ;; esac

say "== macOS: too-old python3 is named"
OLD="$WORK/oldbin"; mkdir -p "$OLD"
cat > "$OLD/python3" <<'FAKEPY'
#!/bin/sh
case "$2" in
  *"[0]"*) echo 3 ;;
  *"[1]"*) echo 9 ;;
esac
FAKEPY
chmod 755 "$OLD/python3"
TH4="$WORK/home-mac-oldpy"; mkdir -p "$TH4"
if out=$(HOME="$TH4" PATH="$OLD:$FAKE" SMART_BUILD_RELEASE_BASE="$BASE_GOOD" /bin/sh scripts/install.sh 2>&1); then
  fail "the install succeeded with Python 3.9"
fi
case "$out" in *"Python 3.10"*"found 3.9"*) ;; *) fail "the old-python error does not name the problem: $out" ;; esac

say "== Linux (ubuntu:24.04 container): venv prerequisite, install, upgrade, corrupted wheel, repo suite"
docker run --rm -v "$WORK/www:/www:ro" -v "$ROOT:/src:ro" ubuntu:24.04 sh -eu -c '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null
  apt-get install -y -qq python3 curl >/dev/null
  # Without venv support the installer must name python3-venv and install nothing.
  if out=$(SMART_BUILD_RELEASE_BASE=file:///www/good sh /www/good/install.sh 2>&1); then
    echo "install succeeded without venv support" >&2; exit 1
  fi
  case "$out" in *python3-venv*) ;; *) echo "wrong error without venv support: $out" >&2; exit 1 ;; esac
  apt-get install -y -qq python3-venv >/dev/null
  SMART_BUILD_RELEASE_BASE=file:///www/good sh /www/good/install.sh
  "$HOME/.local/bin/smart-build" --version
  SMART_BUILD_RELEASE_BASE=file:///www/good sh /www/good/install.sh
  "$HOME/.local/bin/smart-build" --version
  BADHOME=$(mktemp -d)
  if out=$(HOME="$BADHOME" SMART_BUILD_RELEASE_BASE=file:///www/bad sh /www/good/install.sh 2>&1); then
    echo "a corrupted wheel was accepted" >&2; exit 1
  fi
  case "$out" in *"checksum mismatch"*) ;; *) echo "wrong checksum error: $out" >&2; exit 1 ;; esac
  [ ! -e "$BADHOME/.smart-build/venv" ] || { echo "the corrupted install left a venv behind" >&2; exit 1; }
  cd /src && PYTHONPATH=src python3 -B -m unittest discover -s tests
'

# mcr.microsoft.com/powershell publishes no arm64 Linux image (amd64 and 32-bit arm only), so the
# PowerShell 7 tests run in a small local image: ubuntu:24.04 plus Microsoft's official tarball.
PS_VERSION=7.4.6
PWSH_IMAGE="pwsh-install-test:$PS_VERSION"
if ! docker image inspect "$PWSH_IMAGE" >/dev/null 2>&1; then
  docker build -q -t "$PWSH_IMAGE" - >/dev/null <<DOCKERFILE || fail "cannot build the PowerShell $PS_VERSION test image"
FROM ubuntu:24.04
RUN apt-get update -qq && apt-get install -y -qq ca-certificates curl libicu74 >/dev/null \\
 && case "\$(uname -m)" in aarch64) a=arm64 ;; x86_64) a=x64 ;; *) echo "unsupported CPU \$(uname -m)" >&2; exit 1 ;; esac \\
 && curl -fsSL --retry 3 -o /tmp/ps.tgz "https://github.com/PowerShell/PowerShell/releases/download/v$PS_VERSION/powershell-$PS_VERSION-linux-\$a.tar.gz" \\
 && mkdir -p /opt/powershell && tar -xzf /tmp/ps.tgz -C /opt/powershell && chmod +x /opt/powershell/pwsh \\
 && ln -s /opt/powershell/pwsh /usr/bin/pwsh && rm /tmp/ps.tgz
DOCKERFILE
fi
say "== install.ps1 under PowerShell 7 (Linux container): WSL route, non-zero exit"
if out=$(docker run --rm -v "$ROOT/dist/release:/rel:ro" "$PWSH_IMAGE" pwsh -NoProfile -File /rel/install.ps1 2>&1); then
  fail "install.ps1 exited 0; it must explain the WSL route and exit non-zero"
fi
case "$out" in *WSL*) ;; *) fail "install.ps1 does not mention WSL: $out" ;; esac
case "$out" in *install.sh*) ;; *) fail "install.ps1 does not print the WSL one-liner: $out" ;; esac

say "== install.ps1 under iex: the session survives, nothing leaks"
out=$(docker run --rm -v "$ROOT/dist/release:/rel:ro" "$PWSH_IMAGE" pwsh -NoProfile -Command '
  $before = $ErrorActionPreference
  try { Invoke-Expression (Get-Content /rel/install.ps1 -Raw) } catch { Write-Host "caught: $($_.Exception.Message)" }
  if ($ErrorActionPreference -ne $before) { Write-Host "LEAK: ErrorActionPreference changed" }
  Write-Host "SESSION-ALIVE"
' 2>&1) || fail "the session did not survive install.ps1 under iex: $out"
case "$out" in *SESSION-ALIVE*) ;; *) fail "the session did not survive install.ps1 under iex: $out" ;; esac
case "$out" in *LEAK*) fail "install.ps1 leaked into the session: $out" ;; esac
case "$out" in *"caught: Smart Build has no native Windows build"*) ;; *) fail "install.ps1 did not throw the expected error: $out" ;; esac

say "== PSScriptAnalyzer (skipped when it does not install)"
if ! docker run --rm -v "$ROOT/dist/release:/rel:ro" "$PWSH_IMAGE" pwsh -NoProfile -Command '
  try { Install-Module PSScriptAnalyzer -Force -Scope CurrentUser -ErrorAction Stop }
  catch { Write-Host "PSScriptAnalyzer unavailable; skipping"; exit 0 }
  $issues = @(Invoke-ScriptAnalyzer -Path /rel/install.ps1)
  if ($issues.Count -gt 0) { $issues | Format-Table -AutoSize | Out-String | Write-Host }
  if (@($issues | Where-Object Severity -eq "Error").Count -gt 0) { exit 1 }
  Write-Host "PSScriptAnalyzer: no errors"
  exit 0
'; then fail "PSScriptAnalyzer found errors in install.ps1"; fi

kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then fail "port $PORT is still serving after the test"; fi
say "all installer tests passed (port $PORT released)"
