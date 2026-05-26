# onvif-tt

> Open-source, headless, **CI- and AI-friendly** ONVIF conformance test tool.

`onvif-tt` is a Linux-native, MIT-licensed alternative to the closed
ONVIF Device Test Tool. It parses the public ONVIF Test Specification
HTML corpus into a machine-readable catalog and executes registered
test implementations against a Device Under Test, emitting both
**JUnit XML** (CI) and **structured JSON** (LLM agents).

This is **0.1.0 / alpha**. MVP slice today:

* 22 ONVIF test specification files → **1,121 test cases** parsed and
  cached as JSON.
* 5 test implementations against the Device service + Media2 (Profile S
  mandatory subset).
* CLI: `list`, `show`, `corpus refresh|stats`, `run`.
* `run` outputs JUnit XML + JSON + plain pytest stdout.

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

## Conformance scope

`onvif-tt` is for **development feedback**, not formal ONVIF
conformance. Only the actual ONVIF Device Test Tool (members-only)
plus the ONVIF Conformance Process can produce a real Declaration of
Conformance. Use `onvif-tt` in CI; cross-check with the official tool
on milestone builds.
