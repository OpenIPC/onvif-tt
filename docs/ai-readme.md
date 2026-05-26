# Driving `onvif-tt` from an LLM agent

This document is for agents (and humans designing agent prompts). It
specifies the JSON shapes `onvif-tt` exposes so a tool-use loop can
drive it without reading Python code.

## Affordances

Stable, parsable test IDs everywhere. Every ONVIF test case is keyed
by its spec ID — `DEVICE-1-1-2`, `MEDIA2-1-1-4`, `EVENT-2-1-5`, … —
both in the catalog and in run results.

```
ID = /^[A-Z][A-Z0-9_]+-\d+-\d+-\d+(-v\d+(\.\d+)*)?$/
```

## Catalog: `onvif-tt list --format json`

Returns a JSON array of TestCase records:

```json
[
  {
    "id": "DEVICE-3-1-9",
    "version": null,
    "profile_area": "BASE",
    "spec_file": "BASE.html",
    "title": "SYSTEM COMMAND DEVICE INFORMATION",
    "labels": {
      "Test Case ID": "DEVICE-3-1-9",
      "Test Purpose": "To verify GetDeviceInformation command.",
      "WSDL Reference": "devicemgmt.wsdl",
      "Pre-Requisite": "..."
    },
    "procedure": [
      {
        "step_id": "tc.DEVICE-3-1-9.1",
        "text": "ONVIF Client invokes GetDeviceInformation request.",
        "operations": ["GetDeviceInformation"],
        "variables": [],
        "sub_steps": []
      }
    ],
    "pass_criteria": "DUT passes all assertions.",
    "fail_criteria": "...",
    "notes": [],
    "implemented": true
  }
]
```

Filters: `--profile-area BASE`, `--id-glob 'MEDIA2-*'`,
`--implemented`, `--missing`.

For terse listings, add `--compact` — emits only id, title,
profile_area, wsdl_reference, implemented.

## Single test: `onvif-tt show <ID> --format json`

Same record shape as the catalog entry, plus an `impl` block if an
implementation is registered:

```json
{
  "id": "DEVICE-1-1-2",
  ...
  "impl": {
    "qualname": "onvif_tt.cases.base.test_get_capabilities_all",
    "profiles": ["S", "T"],
    "mandatory": true,
    "requires_services": ["devicemgmt"]
  }
}
```

## Expected-failures (device-fingerprint xfail)

Some ONVIF devices have well-documented spec violations. Tests that
match a known-buggy device fingerprint are marked **xfailed** rather
than failing the run, so CI stays green while the real signal — "did
the bug get fixed?" — is preserved.

In `cases/*.py`, an implementation declares the fingerprint inline::

```python
@register("DEVICE-1-1-9", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock closes the TCP connection instead "
                        "of returning a SOAP 1.2 Fault.",
          }])
def test_soap_fault_on_invalid_capability(dut, spec): ...
```

Matcher semantics:

* Each entry is a dict; **all** non-`reason` keys must match the
  `GetDeviceInformation` field values on the DUT (Manufacturer, Model,
  FirmwareVersion, SerialNumber, HardwareId).
* Values can be literal strings (case-sensitive equality) or callables
  (`lambda v: v.startswith("HI3516")`).
* Multiple matchers OR together — any one matching is enough.

In `results.json`, an xfailed result looks like::

```json
{
  "id": "DEVICE-1-1-9",
  "status": "xfailed",
  "xfail_reason": "Xiongmai stock closes the TCP connection ...",
  "longrepr": "Failed: expected a SOAP Fault, got ONVIFError: ..."
}
```

If a test passes on a device where it was expected to fail, the status
becomes `xpassed` and a Python warning is emitted — agents/CI should
treat that as a positive signal ("the vendor fixed it!").

## Running tests: `onvif-tt run --json-report results.json`

After a run, `results.json` has the shape:

```json
{
  "target": "10.216.128.71:8899",
  "summary": {
    "total": 30,
    "passed": 26,
    "failed": 0,
    "skipped": 1,
    "error": 0,
    "xfailed": 3,
    "xpassed": 0
  },
  "results": [
    {
      "id": "DEVICE-3-1-9",
      "status": "passed",
      "duration_s": 0.229,
      "profiles": ["S", "T"],
      "mandatory": true,
      "longrepr": "",
      "last_request":  "<soap:Envelope ...>...</soap:Envelope>",
      "last_response": "<soap:Envelope ...>...</soap:Envelope>"
    },
    {
      "id": "MEDIA2-1-1-4",
      "status": "skipped",
      "duration_s": 0.000,
      "profiles": ["T"],
      "mandatory": true,
      "longrepr": "Skipped: DUT does not advertise services: ['media2']"
    }
  ]
}
```

`status` is one of `passed`, `failed`, `skipped`, `error`, `xfailed`,
`xpassed`. On `failed` / `xfailed`, the `longrepr` carries the pytest
failure message and the last SOAP request/response are pinned to the
result — enough context for an LLM to read the spec, read the SOAP
envelope, and propose a device-side fix.

## Typical agent loop

1. **Discover work**: `onvif-tt list --missing --profile-area BASE
   --compact --format json` → list of unimplemented Profile S/BASE tests.
2. **Pick one**: `onvif-tt show <ID> --format json` → full spec text
   incl. procedure.
3. **Implement** by editing `src/onvif_tt/cases/<area>.py`.
4. **Verify**: `onvif-tt run --target <dev-camera> --id-glob <ID>
   --json-report run.json`. Inspect `run.json.results[0]`.
5. **On failure**: read `last_request` / `last_response`, cross-check
   against the procedure text, propose a fix in the OpenIPC library or
   the test implementation.

## CI integration

* `junit.xml` from `--junit-xml` is the standard JUnit format any
  dashboard handles.
* Exit code: 0 on full pass / clean skips, 1 on test failure, 2 on
  configuration errors.
