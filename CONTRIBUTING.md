# Contributing to onvif-tt

Thanks for considering a contribution! Here's the quick orientation.

## Quick setup

```bash
git clone https://github.com/OpenIPC/onvif-tt
cd onvif-tt
python -m venv .venv && . .venv/bin/activate
pip install -e .
pytest tests/                 # parser + registry unit tests
onvif-tt corpus stats         # catalog sanity
```

You'll need network access to a real ONVIF device (or a Happytime
virtual ONVIF server) to exercise the `run` command. Read-only tests
work against any compliant device.

## Adding a test

See [`docs/adding-a-test.md`](docs/adding-a-test.md) for a focused
walk-through and [`CLAUDE.md`](CLAUDE.md) for the conventions —
especially around calling-convention quirks, void responses, and the
`LOCAL-*` prefix for IDs not in the public spec corpus.

## Pull request expectations

* CI must be green. `.github/workflows/ci.yml` runs the parser and
  registry unit tests across Python 3.10–3.13 plus catalog sanity
  checks.
* If you change the parser, regenerate the cache:
  `onvif-tt corpus refresh` (the deterministic-parse test will catch
  drift).
* New spec implementations should mark `mandatory` honestly per the
  ONVIF spec's requirement level, declare `requires_services`, and use
  `xfail_on` only when you have observed a real vendor non-conformance.
* New `LOCAL-*` IDs are fine — they're how we cover gaps in the v20.12
  spec corpus.

## Reporting bugs / new device non-conformances

When a test surfaces a new vendor bug, include in the issue:

* The full `GetDeviceInformation` (`Manufacturer`, `Model`,
  `FirmwareVersion`, `SerialNumber`, `HardwareId`).
* The failing test ID and `results.json` snippet (especially
  `last_request` and `last_response`).
* What the spec calls for (link to the relevant `corpus/html/*.html`
  section).

If it's reproducible on a known firmware family, an `xfail_on` matcher
plus the bug report is the highest-value contribution.

## Code style

* Python ≥ 3.10. Type hints encouraged, not mandatory.
* Standard library + the existing `zeep` / `lxml` / `pytest` stack
  preferred over new deps.
* Tests for the tool itself live under `tests/`; ONVIF test
  implementations live under `src/onvif_tt/cases/`.

## License

MIT. By contributing you agree your code is released under the same
terms (see `LICENSE`).
