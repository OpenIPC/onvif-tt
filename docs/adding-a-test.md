# Adding a new ONVIF test

Walk-through of the workflow, end-to-end.

## 1. Find the right spec ID

Don't invent test IDs. Always look them up in the parsed corpus:

```bash
# What's left in BASE.html?
onvif-tt list --id-glob "DEVICE-*" --missing --format json | jq

# What does this specific test ask for?
onvif-tt show DEVICE-3-1-9
```

If the conformance intent you want to verify has no clean counterpart
in the spec corpus (e.g. "GetStreamUri returns a valid URL" — the
catalog only tests deeper RTP streaming behaviour), use a `LOCAL-*`
prefix.

## 2. Pick the right module

| Profile area | File |
|---|---|
| Device, capabilities, GetServices, GetSystemDateAndTime | `cases/base.py` |
| Network read-only | `cases/ipconfig.py` |
| Authentication | `cases/auth.py` |
| WS-Discovery | `cases/discovery.py` |
| Media v10 / Media2 / streaming | `cases/media.py` |
| Events (PullPoint, Basic Notification) | `cases/event.py` |
| PTZ | `cases/ptz.py` |
| Imaging | `cases/imaging.py` |

New profile area? Add a new `cases/<area>.py` — it'll be auto-imported
by `registry.discover()`.

## 3. Write the implementation

Minimal shape:

```python
from onvif_tt.registry import register
from onvif_tt.runtime.dut import DUT


@register(
    "DEVICE-1-1-13",
    profiles={"S", "T"},          # which ONVIF profiles this satisfies
    mandatory=True,               # match the spec's requirement level
    requires_services={"devicemgmt"},
)
def test_get_services_device(dut: DUT, spec) -> None:
    """Spec: BASE.html#tc.DEVICE-1-1-13 — GET SERVICES (DEVICE).

    Short prose copy of the procedure goes here.
    """
    services = dut.devicemgmt.GetServices(False)   # positional!
    assert services, "GetServices returned no entries"
    dev_ns = "http://www.onvif.org/ver10/device/wsdl"
    devs = [s for s in services if s.Namespace == dev_ns]
    assert devs, "GetServices does not include the Device service"
    assert devs[0].XAddr, "Device service XAddr empty"
```

### Decorator options

| Option | Use |
|---|---|
| `profiles={"S","T"}` | Filter set for `--profile` |
| `mandatory=True/False` | Match the spec's "must"/"shall" vs "may" |
| `requires_services={"media2"}` | Skip if DUT doesn't advertise it (via `GetServices`) |
| `requires_writes=True` | Skip unless `--allow-writes` (motors, reboot, config change) |
| `xfail_on=[{"Manufacturer": "H264", "reason": "..."}]` | Mark as expected-failure on a fingerprinted device |
| `tags={"local", "network"}` | Free-form labels |

### Common assertions

* `assert resp.Field`, never `assert resp.Field is not None` — empty
  strings are still wrong.
* For void SOAP responses (`Move`, `Stop`, `Unsubscribe`, …) zeep
  returns `None` — don't assert response-not-none. Test passes simply
  by not raising a Fault.
* Negative tests want `zeep.exceptions.Fault`; catch other exceptions
  with `pytest.fail`.

## 4. Verify

Against the reference camera (Xiongmai stock at `10.216.128.71:8899` is
a typical OEM ONVIF target):

```bash
onvif-tt run \
  --target 10.216.128.71:8899 --user admin --password admin \
  --id-glob "DEVICE-1-1-13" \
  --json-report /tmp/r.json
jq '.results[] | {id, status, last_response: .last_response[:200]}' /tmp/r.json
```

For write tests, add `--allow-writes`.

Tool's own unit tests still passing:

```bash
pytest tests/
```

## 5. Update corpus cache (only if you touched the parser)

```bash
onvif-tt corpus refresh
pytest tests/test_parser.py   # asserts the regenerated JSON matches the committed copy
```

## 6. Open the PR

Include in the description:

* What spec IDs you added.
* Reference camera + result (e.g. "Xiongmai stock: 3 passed, 0 failed").
* Any new `xfail_on` matchers and the device behaviour you observed.

CI will run the parser + registry + catalog-sanity checks on Python
3.10–3.13. Green is required; reviewers want to see the actual run
output too.
