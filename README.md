# onvif-tt

> Open-source, headless, **CI- and AI-friendly** ONVIF conformance test tool.

[![ci](https://github.com/OpenIPC/onvif-tt/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenIPC/onvif-tt/actions/workflows/ci.yml)
![license](https://img.shields.io/badge/license-MIT-green)
![python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)
![tests](https://img.shields.io/badge/implementations-84-informational)

`onvif-tt` is a Linux-native, MIT-licensed alternative to the closed
ONVIF Device Test Tool. It parses the public ONVIF Test Specification
HTML corpus into a machine-readable catalog and executes registered
test implementations against a Device Under Test, emitting both
**JUnit XML** (CI) and **structured JSON** (LLM agents).

It's built and maintained inside [OpenIPC](https://openipc.org) to
provide an automated conformance signal for the OpenIPC camera-side
ONVIF library; the tool itself is vendor-neutral and works against any
ONVIF-compliant device.

## What's inside

* 22 ONVIF Test Specification files → **1,121 test cases** parsed into
  `corpus/parsed.json` with a deterministic-parse guard.
* **84 test implementations** spanning:
  * Device service (capabilities, GetServices, system commands incl.
    `SetSystemDateAndTime` and `GetSystemLog`, SOAP-fault handling)
  * Network read-only (interfaces, DNS, NTP, gateway, protocols)
  * Authentication (valid creds, wrong password, anonymous-rejected)
  * WS-Discovery (multicast Probe, ProbeMatch validation)
  * Media v10 + Media2 (profiles, encoders, stream URI, snapshot,
    cross-endpoint consistency, **deep-decoder checks**: resolution
    matches profile, codec profile matches SPS, frame rate within
    tolerance, PTS monotonicity — all via `ffprobe` on the live stream)
  * Events — PullPoint and Basic Notification lifecycles, with a
    subscription tracker that auto-unsubscribes on teardown
  * PTZ (nodes, AbsoluteMove / RelativeMove / ContinuousMove / Stop)
  * Imaging (settings, options, MoveOptions, Status, Absolute /
    Relative / Continuous focus moves)
  * **Profile G** — Recording control + capabilities, Recording Search
    (`FindRecordings`, `GetRecordingSummary`, `EndSearch`-fault),
    Replay (`GetReplayUri`)
* Read-only by default. Tests that actuate hardware (motors, recording,
  factory reset) opt in via `--allow-writes`.
* Device-fingerprint **`xfail_on`** surfaces known-buggy firmware
  without alarming CI; an `xpassed` warning signals when a vendor
  fixes it.
* `pytest-xdist` parallel execution measured at ~2.7× speedup vs
  sequential against a typical LAN camera.
* `.github/workflows/ci.yml` runs parser + registry + catalog sanity
  on every push across Python 3.10–3.13. Optional integration job
  exercises onvif-tt against a Happytime virtual ONVIF device.
* CLI: `list`, `show`, `corpus refresh|stats`, `run`.

## Why this exists

The official **ONVIF Device Test Tool** is members-only,
non-redistributable, and Windows-only. The public **ONVIF Test
Specification** documents (Base, Media2, PTZ, …) are *not* — anyone may
copy and redistribute them. `onvif-tt` is what you get if you take
those public specs at face value and write the executable mirror in
Python.

## Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Requirements: Python 3.10+, network access to the device under test.

## Quickstart

```bash
# 1. What's in the catalog?
onvif-tt corpus stats
#  RTSS            139
#  BASE            134
#  ...
#  TOTAL          1121

# 2. Which test IDs do we currently implement?
onvif-tt list --implemented

# 3. What does a particular test case say?
onvif-tt show DEVICE-3-1-9
#   DEVICE-3-1-9  (spec BASE.html, version None)
#   Title: SYSTEM COMMAND DEVICE INFORMATION
#     WSDL Reference: devicemgmt.wsdl
#     ...
#   Procedure: ...

# 4. Run the implemented tests against a camera.
onvif-tt run \
  --target 10.216.128.71:8899 \
  --user admin --password admin \
  --junit-xml junit.xml \
  --json-report results.json
#  4 passed, 1 skipped in 3.7s
```

## Project layout

```
onvif-tt/
├── corpus/
│   ├── html/          # 22 verbatim ONVIF Test Specification HTML files
│   └── parsed.json    # generated cache (run `onvif-tt corpus refresh`)
├── src/onvif_tt/
│   ├── specs/         # corpus parser + dataclasses
│   ├── runtime/       # DUT wrapper (zeep), feature discovery, SOAP trace
│   ├── runner/        # pytest plugin + dispatch + JSON reporter
│   ├── cases/         # one .py per profile area; `@register("ID")` fns
│   └── cli.py
├── tests/             # tests *for the tool itself* (parser unit tests)
└── docs/
    └── ai-readme.md   # how an LLM should drive the tool
```

## How to add a test

Find the spec ID:

```bash
onvif-tt list --id-glob "MEDIA2-1-*" --missing
```

Open `src/onvif_tt/cases/<area>.py` and add a function:

```python
from onvif_tt.registry import register
from onvif_tt.runtime.dut import DUT

@register("MEDIA2-1-1-4", profiles={"T"}, mandatory=True,
          requires_services={"devicemgmt", "media2"})
def test_get_profiles_media2(dut: DUT, spec) -> None:
    """Spec: MEDIA2.html#tc.MEDIA2-1-1-4 — GET PROFILES.

    Media2 must return at least one profile with token + Name.
    """
    profiles = dut.media2.GetProfiles()
    assert profiles
    assert profiles[0].token
    assert profiles[0].Name
```

That's it — the pytest dispatch picks the test up automatically next
time `onvif-tt run` is invoked. The `spec` fixture gives you the
parsed TestCase record (procedure text, prerequisites, pass/fail
criteria) if you need it for assertion messages.

### Calling convention quirk

`python-onvif-zeep` service wrappers take **positional** WSDL
parameters, not keyword args:

```python
dut.devicemgmt.GetServices(False)        # ✅
dut.devicemgmt.GetServices(IncludeCapability=False)   # ❌
```

The same applies to every WSDL operation — read the spec procedure to
get the parameter order.

### Expected-failures on known-buggy devices

If a test is known to fail on specific firmware, annotate it with
`xfail_on` matchers — the runner will mark it `xfailed` instead of
failing the whole CI run on that device:

```python
@register("DEVICE-1-1-9", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock closes the TCP connection "
                        "instead of returning a SOAP 1.2 Fault.",
          }])
def test_soap_fault_on_invalid_capability(dut, spec): ...
```

Matchers compare against `GetDeviceInformation` fields (Manufacturer,
Model, FirmwareVersion, SerialNumber, HardwareId). Values can be
literals (case-sensitive equality) or callables (`lambda v: ...`).
Multiple matchers OR together — any one matching expectations the
failure. If a fixed-up device unexpectedly passes, the run logs an
`xpassed` warning so you know the bug was repaired upstream.

## CI integration

```bash
onvif-tt run \
  --target $CAMERA_HOST:$CAMERA_PORT \
  --user $USER --password $PASS \
  --profile S \
  --junit-xml junit.xml \
  --json-report results.json
```

* Exit code 0 on full pass / clean skips, 1 on any test failure.
* `junit.xml` is consumed by any CI dashboard out of the box.
* `results.json` is a flat record per test (`id`, `status`,
  `duration_s`, `last_request`, `last_response`, `longrepr`) suitable
  for log shipping or LLM consumption.

## AI integration

`onvif-tt list --format json` and `onvif-tt show <ID> --format json`
emit stable schemas suitable for tool-use prompts. See
[`docs/ai-readme.md`](docs/ai-readme.md) for the agent-driver guide.

## Licensing

* The Python code in this repository is **MIT** (see `LICENSE`).
* The ONVIF Test Specification HTML files in `corpus/html/` are
  redistributable per their own copyright notice ("Recipients of this
  document may copy, distribute, publish, or display this document so
  long as this copyright notice, license and disclaimer are retained
  with all copies of the document"). See `corpus/README.md`.

## Honesty notes about the published ONVIF test corpus

The plan that drove this repo called out a few test families that turned
out not to match the v20.12 corpus exactly. For transparency:

* **`AUTH-*` doesn't exist** in the catalog. Authentication conformance
  is scattered across other tests' Pre-Requisite clauses. We provide
  `LOCAL-AUTH-*` tests (wrong-password, anonymous-rejected) covering
  the intent.
* **`IPCONFIG-1-1-*` are write ops**, not read-only as the plan said —
  every one of them invokes `SetNetworkInterfaces` and `SystemReboot`.
  We provide `LOCAL-NETWORK-*` read-only equivalents
  (GetNetworkInterfaces, GetDNS, GetNTP, GetHostname, GetDefaultGateway,
  GetNetworkProtocols).
* **The corpus doesn't carry per-test ONVIF Profile tags** — there's no
  way to ask "which catalog entries are Profile S?" without a separate
  PROFILES.html parser. `onvif-tt list --profile-area BASE` filters by
  source file, not by ONVIF profile; the `--profile S` filter on `run`
  uses the registry's own profile annotations.
* **`DISCOVERY-2-1-1 / -2`** in the corpus are about XML-namespace
  conformance on ProbeMatch envelopes — they need raw-envelope
  inspection that `wsdiscovery` doesn't expose. We implement a softer
  approximation plus `LOCAL-DISCOVERY-PROBE` (the actual "does
  multicast Probe find the DUT?" smoke).

`LOCAL-*` IDs are tool-author additions; everything else is the literal
corpus ID.

## Parallel execution

`onvif-tt run … -n auto` (or any `-n N`) runs through `pytest-xdist`.
The session-scoped DUT is constructed once per worker; SOAP envelopes
are stashed on `item.user_properties` so the master-side JSON reporter
can recover them after the workers send their reports back.

Verified: 66-test run goes from ~52 s sequential to ~19 s with
`-n 4` (~2.7× speedup) against the dlab reference camera, with the
JSON summary identical.

## Conformance scope

`onvif-tt` is for **development feedback**, not formal ONVIF
conformance. Only the actual ONVIF Device Test Tool (members-only)
plus the ONVIF Conformance Process can produce a real Declaration of
Conformance. Use `onvif-tt` in CI; cross-check with the official tool
on milestone builds.
