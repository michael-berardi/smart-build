#!/bin/sh
# Smart Build installer for macOS and Linux:
#   curl -fsSL https://github.com/michael-berardi/smart-build/releases/latest/download/install.sh | sh
# Downloads the release wheel, checks its SHA-256 before anything is installed, and installs it into a private
# Python venv under ~/.smart-build. Smart Build has no dependencies, so nothing else is fetched. Puts
# `smart-build` in ~/.local/bin. No sudo, no shell profile edits; re-running upgrades in place.
# Environment: SMART_BUILD_HOME (default ~/.smart-build), SMART_BUILD_BIN_DIR (default ~/.local/bin),
# SMART_BUILD_VERSION (pin a version, e.g. 0.2.0), SMART_BUILD_RELEASE_BASE (download base override, used by tests).
set -eu

REPO="michael-berardi/smart-build"
HOME_DIR="${SMART_BUILD_HOME:-$HOME/.smart-build}"
case "$HOME_DIR" in /*) ;; *) HOME_DIR="$(pwd)/$HOME_DIR" ;; esac
BIN_DIR="${SMART_BUILD_BIN_DIR:-$HOME/.local/bin}"
case "$BIN_DIR" in /*) ;; *) BIN_DIR="$(pwd)/$BIN_DIR" ;; esac
if [ -n "${SMART_BUILD_RELEASE_BASE:-}" ]; then BASE="$SMART_BUILD_RELEASE_BASE"
elif [ -n "${SMART_BUILD_VERSION:-}" ]; then BASE="https://github.com/$REPO/releases/download/v${SMART_BUILD_VERSION#v}"
else BASE="https://github.com/$REPO/releases/latest/download"; fi

say() { printf '%s\n' "$*"; }
die() { printf 'smart-build install: %s\n' "$*" >&2; exit 1; }

if command -v curl >/dev/null 2>&1; then fetch() { curl -fsSL --retry 3 -o "$2" "$1" || die "download failed: $1"; }
elif command -v wget >/dev/null 2>&1; then fetch() { wget -q -O "$2" "$1" || die "download failed: $1"; }
else die "needs curl or wget (macOS ships curl; on Debian/Ubuntu: apt-get install curl)"; fi
case "$BASE" in file://*) command -v curl >/dev/null 2>&1 || die "a file:// release base needs curl" ;; esac

if command -v sha256sum >/dev/null 2>&1; then sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else die "needs sha256sum or shasum"; fi

main() {
  TMP=$(mktemp -d "${TMPDIR:-/tmp}/smart-build-install.XXXXXX") || die "cannot create a temporary folder"
  STAGE=""
  trap 'rm -rf "$TMP" "$STAGE"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  # Prerequisites first: a Python 3.10+ that can create venvs. We never install a system Python ourselves.
  if ! command -v python3 >/dev/null 2>&1; then
    case "$(uname -s)" in
      Darwin) die "needs Python 3.10 or newer; install it with: brew install python@3.12 (or from https://www.python.org/downloads/), then run this again" ;;
      *) die "needs Python 3.10 or newer; install it with your package manager (Debian/Ubuntu: apt-get install python3 python3-venv; Fedora: dnf install python3), then run this again" ;;
    esac
  fi
  PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])' 2>/dev/null) || die "python3 is on PATH but does not run"
  PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])' 2>/dev/null) || die "python3 is on PATH but does not run"
  if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    die "needs Python 3.10 or newer; found $PY_MAJOR.$PY_MINOR (macOS: brew install python@3.12; Debian/Ubuntu: apt-get install python3)"
  fi
  python3 -c 'import ensurepip' >/dev/null 2>&1 \
    || die "this python3 cannot create venvs (ensurepip is missing); on Debian/Ubuntu install it with: apt-get install python3-venv, then run this again"

  say "Downloading Smart Build..."
  fetch "$BASE/SHA256SUMS" "$TMP/SHA256SUMS"
  WHEEL=$(awk '{ f=$2; sub(/^\*/, "", f); if (f ~ /^smart_build-.*-py3-none-any\.whl$/) { print f; exit } }' "$TMP/SHA256SUMS")
  [ -n "$WHEEL" ] || die "no Smart Build wheel is listed in SHA256SUMS; this is not a Smart Build release"
  VERSION=${WHEEL#smart_build-}; VERSION=${VERSION%%-*}
  if [ -n "${SMART_BUILD_VERSION:-}" ] && [ "$VERSION" != "${SMART_BUILD_VERSION#v}" ]; then
    die "the release at $BASE has version $VERSION, not the requested ${SMART_BUILD_VERSION#v}"
  fi
  fetch "$BASE/$WHEEL" "$TMP/$WHEEL"
  want=$(awk -v n="$WHEEL" '{ f=$2; sub(/^\*/, "", f); if (f == n) print $1 }' "$TMP/SHA256SUMS")
  [ -n "$want" ] || die "no checksum for $WHEEL in SHA256SUMS"
  [ "$(sha256 "$TMP/$WHEEL")" = "$want" ] || die "checksum mismatch for $WHEEL; nothing was installed"

  say "Installing Smart Build $VERSION into $HOME_DIR ..."
  mkdir -p "$HOME_DIR" "$BIN_DIR" || die "cannot create $HOME_DIR or $BIN_DIR"
  VENV="$HOME_DIR/venv"
  # Stage on the same filesystem as the final venv so the move into place is a rename.
  STAGE="$HOME_DIR/.venv.new.$$"
  rm -rf "$STAGE"
  python3 -m venv "$STAGE" || die "cannot create the private venv (python3 -m venv failed)"
  "$STAGE/bin/python3" -m pip install --no-index --no-deps --disable-pip-version-check --quiet "$TMP/$WHEEL" \
    || die "pip could not install the wheel; nothing was changed"
  got=$("$STAGE/bin/smart-build" --version 2>&1) || die "the new copy does not start: $got"
  [ "$got" = "smart-build $VERSION" ] || die "the new copy reports '$got', not smart-build $VERSION; nothing was changed"

  # Move into place last, keeping the old venv until the new one is final.
  rm -rf "$VENV.old"
  [ ! -d "$VENV" ] || mv "$VENV" "$VENV.old" || die "cannot move the old install aside"
  NEWPATH="$STAGE"
  mv "$STAGE" "$VENV" || { [ ! -d "$VENV.old" ] || mv "$VENV.old" "$VENV"; die "cannot move the new venv into place"; }
  STAGE=""
  # A venv embeds its creation path in script shebangs and activate files; point them at the final path.
  for f in "$VENV/bin/"*; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    if grep -q "$NEWPATH" "$f" 2>/dev/null; then
      sed "s|$NEWPATH|$VENV|g" "$f" > "$TMP/fix" || die "cannot finalize $f"
      cat "$TMP/fix" > "$f" || die "cannot finalize $f"
    fi
  done
  rm -rf "$VENV.old"

  # `smart-build` on PATH: a small wrapper, so the tool folder can be moved by re-running the installer.
  cat > "$BIN_DIR/smart-build.tmp.$$" <<WRAP
#!/bin/sh
exec "$VENV/bin/smart-build" "\$@"
WRAP
  chmod 755 "$BIN_DIR/smart-build.tmp.$$"
  mv -f "$BIN_DIR/smart-build.tmp.$$" "$BIN_DIR/smart-build"

  got=$("$BIN_DIR/smart-build" --version 2>&1) || die "the installed command does not run: $got"
  say "$got installed ($BIN_DIR/smart-build)."
  case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "Add $BIN_DIR to your PATH:  export PATH=\"$BIN_DIR:\$PATH\"" ;; esac
  say "Next: smart-build -- <your build command>   (for example: smart-build -- npm run build)"
}

main "$@"
