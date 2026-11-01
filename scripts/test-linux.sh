#!/usr/bin/env bash
# Run the test suite on Linux in a throwaway container (python:3.10 and python:3.13 images).
# Needs Docker. With Colima, a VM this script starts is stopped again when it exits.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
started_colima=0
cleanup() { if [ "$started_colima" = 1 ]; then colima stop >/dev/null 2>&1 || true; fi; }
trap cleanup EXIT
if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null; then
    colima start --arch aarch64 --vm-type vz --vz-rosetta >/dev/null
    started_colima=1
  else
    echo "test-linux: Docker is not running" >&2
    exit 2
  fi
fi
for image in python:3.10-slim python:3.13-slim; do
  echo "== $image"
  # The sources go in through stdin, so this works whatever folders the Docker VM shares.
  # README.md, docs, skills and bench come along because tests/test_neutrality.py scans them.
  COPYFILE_DISABLE=1 tar --no-xattrs -C "$root" -cf - src tests README.md docs skills bench | docker run --rm -i "$image" sh -c \
    'mkdir /w && tar -xf - -C /w && cd /w && python3 -W error::ResourceWarning -m unittest discover -s tests'
done
