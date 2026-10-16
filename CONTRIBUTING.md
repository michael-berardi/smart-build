# Contributing

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
./scripts/test-linux.sh   # the same suite on Linux, in Docker
```

The tests start real processes and take real locks in a temporary folder. Nothing outside it is read or written.
They take about 30 seconds.

## Changes

1. Keep the queue crash-safe: no state that the kernel does not release when a process dies.
2. Add a test in `tests/` that fails without the change.
3. Update `README.md` and `docs/DESIGN.md` where behaviour changes, in the same commit.
4. For anything that touches waiting or starting, run `python3 bench/compare.py` before and after and include both.

## Style

Standard library only. Every refusal names its cause. No silent fallbacks.
