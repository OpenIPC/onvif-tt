# Working on `onvif-tt` with Claude (or any AI agent)

This file is a cold-start brief for AI agents collaborating on this
repo. It explains what the project is, how its pieces fit together,
and the conventions that — if you get them wrong — produce subtle
bugs that look right but aren't.

## What this is

`onvif-tt` is a Linux-native, headless ONVIF conformance test tool —
an open-source alternative to the closed (members-only, Windows-only)
ONVIF Device Test Tool. It:

1. Parses the public ONVIF Test Specification HTML corpus into a
   structured catalog (`corpus/parsed.json`, 1,121 test cases).
2. Lets contributors register Python implementations of individual
   spec IDs via `@register("DEVICE-1-1-2")` decorators.
3. Runs the registered tests against a Device Under Test via pytest,
   emitting JUnit XML for CI and a structured JSON report whose schema
   is published in `docs/schemas/`.

## Repo layout

```
onvif-tt/
├── corpus/
│   ├── html/                 # 22 ONVIF Test Specification files (verbatim, redistributable)
│   └── parsed.json           # generated cache; deterministic-parse-tested
├── src/onvif_tt/
│   ├── specs/                # lxml DocBook parser + dataclasses
│   ├── registry.py           # @register decorator + REGISTRY dict + xfail_on matcher
│   ├── runtime/
│   │   ├── dut.py            # ONVIFCamera wrapper, lazy service binding,
│   │   │                     # PullPointHandle, NotifyHandle, SOAP trace
│   │   ├── features.py       # cached GetServices + GetDeviceInformation
│   │   ├── discovery.py      # multicast WS-Discovery Probe helper
│   │   └── soap_trace.py     # zeep plugin that captures envelopes
│   ├── runner/
│   │   ├── dispatch.py       # pytest parametrise — one node per registry entry
│   │   └── plugin.py         # CLI options + JSON reporter (xdist-safe)
│   ├── cases/                # one .py per profile area
│   │   ├── base.py           # Device + capabilities + GetServices flavours
│   │   ├── auth.py           # LOCAL-AUTH-* (no AUTH-* in catalog)
│   │   ├── discovery.py      # WS-Discovery
│   │   ├── ipconfig.py       # LOCAL-NETWORK-* read-only (catalog IPCONFIG-* are writes)
│   │   ├── media.py          # Media v10 + Media2 (adaptive) + consistency tests
│   │   ├── event.py          # GetEventProperties + PullPoint + Basic Notification
│   │   ├── ptz.py            # GetNodes + Move/Stop write ops
│   │   └── imaging.py        # GetImagingSettings + Move/Stop write ops
│   └── cli.py                # argparse: `onvif-tt list|show|corpus|run`
├── tests/                    # Tests for the tool itself (parser + registry)
├── docs/
│   ├── ai-readme.md          # How to drive the tool from an LLM tool-use loop
│   ├── adding-a-test.md      # Contributor guide
│   └── schemas/              # JSON Schema for results.json + corpus/parsed.json
└── scripts/                  # Shell helpers (subscription-cleanup verifier, …)
```

## Conventions that matter

These are mistakes that bit us during development. If you reproduce
them, the tests will pass in spurious ways.

### 1. Look up real test IDs via the catalog before `@register`-ing

The spec ID `DEVICE-1-1-2` is "ALL CAPABILITIES", not "GetDeviceInformation"
(that's `DEVICE-3-1-9`). The names are not as obvious as they look.

```bash
onvif-tt show DEVICE-3-1-9    # prints the actual procedure
onvif-tt list --id-glob "MEDIA2-2-*" --missing   # what's left to implement
```

If you invent an ID that isn't in the catalog, the CI check
"every registered spec ID exists in the catalog" will fail.

### 2. `python-onvif-zeep` takes **positional** WSDL parameters

```python
dut.devicemgmt.GetServices(False)                       # ✅
dut.devicemgmt.GetServices(IncludeCapability=False)     # ❌ raises
dut.devicemgmt.GetCapabilities("All")                   # ✅
dut.devicemgmt.GetCapabilities(Category="All")          # ❌
```

For operations with structured arguments, build with `create_type`:

```python
req = dut.media.create_type("GetStreamUri")
req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
req.ProfileToken = profile.token
resp = dut.media.GetStreamUri(req)
```

### 3. ONVIF "void" responses come back as Python `None`

Operations like `Move`, `Stop`, `AbsoluteMove`, `RelativeMove`,
`Unsubscribe`, `SetSynchronizationPoint`, … have empty response bodies
in the WSDL — zeep returns `None`. Never assert `resp is not None` on
these; the assertion is "no SOAP Fault was raised".

```python
dut.imaging.Move(req)        # ✅ just the call
resp = dut.imaging.Move(req)
assert resp is not None      # ❌ will FAIL spuriously on a perfectly conformant device
```

### 4. PullPoint subscriptions need **two** WSDL bindings

The PullPoint subscription URL is queried via different bindings for
different operations:

* `PullPointSubscriptionBinding` → `PullMessages`, `SetSynchronizationPoint`
* `SubscriptionManagerBinding` → `Renew`, `Unsubscribe`

`PullPointHandle` (and `NotifyHandle` for Basic Notification) in
`runtime/dut.py` already wraps both. Use them; don't roll your own.

```python
with dut.create_pullpoint("PT60S") as pp:
    pp.pull_messages(timeout="PT3S", limit=5)
    # auto-unsubscribe on __exit__
```

### 5. `LOCAL-*` prefix for tool-author IDs

When the spec catalog has no clean counterpart for the conformance
intent you want to test (e.g. a basic "GetStreamUri returns a valid
RTSP URL" smoke), use a `LOCAL-*` ID:

```python
@register("LOCAL-MEDIA-S-STREAM-URI", profiles={"S"}, mandatory=True,
          requires_services={"devicemgmt", "media"},
          tags={"local"})
```

The CI check skips `LOCAL-*` IDs when validating "registered ID is in
catalog". Don't put real-looking IDs there; if it's in `corpus/parsed.json`,
use the real ID.

### 6. `--allow-writes` for hardware-actuating tests

Anything that moves a focus motor, pans a head, mutates network
config, sets the system clock, or reboots needs `requires_writes=True`:

```python
@register("PTZ-3-1-1", ..., requires_writes=True)
def test_ptz_absolute_move(dut, spec):
    ...
```

Without `--allow-writes` on the run command, these skip with a clear
reason. Default runs are read-only.

### 7. `xfail_on` for known-buggy firmware, not for "test is hard"

If a vendor's firmware reliably violates the spec, document it inline:

```python
@register("DEVICE-1-1-9", ...,
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock closes the TCP connection on "
                        "invalid GetCapabilities Category instead of "
                        "returning a SOAP 1.2 Fault.",
          }])
def test_soap_fault_on_invalid_capability(dut, spec): ...
```

Matchers compare against `GetDeviceInformation` fields (literal or
callable). Multiple matchers OR together. If the bug ever gets fixed,
the test becomes `xpassed` and a Python warning surfaces it.

**Do not** use `xfail_on` to silence flaky tests. If a test is flaky,
fix the test.

### 8. Don't break corpus determinism

`corpus/parsed.json` is committed AND regenerable. A CI test asserts
that re-parsing produces byte-identical output. If you touch the
parser, run:

```bash
onvif-tt corpus refresh
pytest tests/test_parser.py
```

Both must succeed.

### 9. Services come from our table + our schemas, not the library's

`python-onvif-zeep`'s `onvif.definition.SERVICES` and its bundled
`wsdl/` directory are **not** used. Its `onvif.xsd` is pinned at 2.4.2,
which has no `tt:StringList` / `tt:VideoEncoder2Configuration`, and it
has no ver20 media entry at all — so Media2 was unbindable and 12 tests
never ran (issue #1).

Instead:

* `src/onvif_tt/runtime/services.py` — one `ServiceDef` row per service:
  short name, WSDL namespace (what `GetServices` reports), WSDL URL,
  SOAP binding. Adding a service means adding a row here. Nothing else
  maps namespaces to short names.
* `src/onvif_tt/schemas/` — the vendored WSDL/XSD closure, **byte-identical
  to onvif.org** and laid out mirroring the source URL paths. ONVIF permits
  redistribution but not modification, so never hand-edit these or rewrite
  a `schemaLocation`. Regenerate instead:

```bash
onvif-tt schemas refresh    # transitively re-crawls from every ServiceDef
onvif-tt schemas verify     # re-hashes the tree against MANIFEST.json
```

XAddrs come from `GetServices` — Media2 has no slot in ver10
`GetCapabilities`, so the library's `update_xaddrs()` discovery can't see
it. `DUT` disables that method (it also leaked a PullPoint subscription
per run) and resolves XAddrs itself.

**A service the DUT advertises but we can't bind is a failure, not a
skip.** That's the whole lesson of issue #1: a skip reads as "not
applicable" and keeps the run green. `dispatch._gate_services` splits the
two, and `LOCAL-CLIENT-SERVICES-BINDABLE` fails on anything advertised
that has no `ServiceDef` or no vendored WSDL.

### 10. Every response is structurally validated — expect reds you didn't write

`runtime/response_validator.py` checks **every** response, on by default, and
a violation **fails the calling test**. A test whose response was missing a
mandatory element cannot honestly claim a pass — that's the point (issue #3).
So a test you didn't touch can go red because the device returned something
the schema forbids. Read the failure before assuming you broke it.

Three checks, from zeep's type model (`min_occurs`, attribute `required`) and
the vendored XSDs parsed as plain XML (`xs:enumeration` facets):
`missing-element`, `bad-enum`, `missing-attribute`.

It is **not** a general XSD validator, and can't be: the published ONVIF
schemas aren't valid XSD 1.0 — `onvif.xsd` alone has 26+ Unique Particle
Attribution violations, so libxml2 refuses to compile them. There is no
datatype/format checking and no undeclared-element detection. Don't read a
clean run as "schema-valid".

One device defect usually reddens many tests (a bad `GetProfiles` hits 13 on
our reference camera). `results.json` carries a deduplicated `schema_violations`
roll-up at the top level — read that, not the failure count.

To silence a device that is knowingly non-conformant, use `xfail_on` as
usual; the check sits inside the xfail block. `--no-schema-validation` exists
for working around a *validator* bug, not a device one.

## Adding a new test — quick recipe

```bash
# 1. Find the right spec ID (don't invent — look it up):
onvif-tt list --id-glob "EVENT-3-*" --missing --format json | jq

# 2. Read the spec procedure:
onvif-tt show EVENT-3-1-32

# 3. Add to the appropriate cases/*.py file. Mirror the spec's
#    Pre-Requisite as requires_services, mark mandatory/optional honestly.

# 4. Verify against the reference camera:
onvif-tt run --target 10.216.128.71:8899 --user admin --password admin \
    --id-glob "EVENT-3-1-32" --json-report /tmp/r.json

# 5. Tool's own unit tests still pass:
pytest tests/
```

Add `xfail_on` only if you observe a real device behaviour you want
the catalog to remember. Don't speculate.

## When to ask before acting

- **Anything that mutates the DUT** (writes, reboot, config change) —
  only with explicit `--allow-writes` user intent.
- **Pushing branches / opening PRs** — CI runs on push; ask before
  triggering it on the live repo.
- **Adding dependencies to `pyproject.toml`** — every dep widens the
  install surface; prefer stdlib or the existing zeep/lxml/pytest
  stack.

## Pointers

- Detailed AI tool-use shapes: `docs/ai-readme.md`
- JSON Schemas: `docs/schemas/`
- Contributor docs: `CONTRIBUTING.md`, `docs/adding-a-test.md`
- Project home: <https://github.com/OpenIPC/onvif-tt>
