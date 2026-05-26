"""DEVICEIO service tests — relay outputs, digital inputs, IO-side
video/audio source enumeration.

The Device I/O service exposes physical I/O on the camera: relay
outputs (open/close contacts driven by the device), digital inputs
(switch contacts read by the device), and parallel queries to the
Media service's video/audio sources.
"""

from __future__ import annotations

import pytest
import zeep.exceptions

from ..registry import register
from ..runtime.dut import DUT


_INVALID_TOKEN = "__definitely_not_a_real_io_token__"


# ---------------------------------------------------------------------------
# Relay outputs (1-1-*) — read-only subset
# ---------------------------------------------------------------------------

@register("DEVICEIO-1-1-1", profiles={"S", "T", "D"}, mandatory=False,
          requires_services={"devicemgmt", "deviceio"})
def test_io_get_relay_outputs(dut: DUT, spec) -> None:
    """DEVICEIO.html#tc.DEVICEIO-1-1-1 — IO GETRELAYOUTPUTS.

    Returns the list of relay-output channels. Empty list is allowed
    (device has no relays) — we just assert the call shape.
    """
    relays = dut.deviceio.GetRelayOutputs() or []
    for r in relays:
        assert getattr(r, "token", None), "relay output missing token"
        # Properties.Mode must be Bistable or Monostable per spec.
        mode = getattr(getattr(r, "Properties", None), "Mode", None)
        if mode is not None:
            assert mode in ("Bistable", "Monostable"), (
                f"relay {r.token!r} has unrecognised Mode={mode!r}"
            )


@register("DEVICEIO-1-1-3", profiles={"D"}, mandatory=False,
          requires_services={"devicemgmt", "deviceio"})
def test_io_get_relay_output_options(dut: DUT, spec) -> None:
    """DEVICEIO.html#tc.DEVICEIO-1-1-3 — IO GETRELAYOUTPUTOPTIONS.

    Returns the valid value ranges for each relay's configurable
    parameters (IdleState, DelayTimes, …). Skip if no relays exist.
    """
    relays = dut.deviceio.GetRelayOutputs() or []
    if not relays:
        pytest.skip("DUT has no relay outputs")
    opts = dut.deviceio.GetRelayOutputOptions(relays[0].token)
    assert opts is not None, "GetRelayOutputOptions returned None"


@register("DEVICEIO-1-1-5", profiles={"D"}, mandatory=False,
          requires_services={"devicemgmt", "deviceio"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock pattern: invalid tokens close the "
                        "connection instead of returning a SOAP Fault.",
          }])
def test_io_set_relay_output_settings_invalid_token(dut: DUT, spec) -> None:
    """DEVICEIO.html#tc.DEVICEIO-1-1-5 — SETRELAYOUTPUTSETTINGS – INVALID
    TOKEN. SOAP Fault required.
    """
    req = dut.deviceio.create_type("SetRelayOutputSettings")
    req.RelayOutput = {
        "token": _INVALID_TOKEN,
        "Properties": {"Mode": "Bistable", "IdleState": "open",
                       "DelayTime": "PT0S"},
    }
    try:
        dut.deviceio.SetRelayOutputSettings(req)
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on invalid relay-output token")


@register("DEVICEIO-1-2-1", profiles={"D"}, mandatory=False,
          requires_services={"devicemgmt", "deviceio"},
          requires_writes=True)
def test_io_set_relay_output_state_bistable(dut: DUT, spec) -> None:
    """DEVICEIO.html#tc.DEVICEIO-1-2-1 — SETRELAYOUTPUTSTATE – BISTABLE
    MODE.

    Toggles a relay's logical state (open → close → restore). Gated on
    --allow-writes because this physically clicks the relay contacts.
    """
    relays = dut.deviceio.GetRelayOutputs() or []
    if not relays:
        pytest.skip("DUT has no relay outputs")
    relay = next(
        (r for r in relays
         if getattr(getattr(r, "Properties", None), "Mode", None) == "Bistable"),
        None,
    )
    if relay is None:
        pytest.skip("no bistable relays")

    # Try the inverse of the current idle state, then restore.
    original_idle = relay.Properties.IdleState  # "open" or "closed"
    inverse = "closed" if original_idle == "open" else "open"
    try:
        dut.deviceio.SetRelayOutputState(relay.token, inverse)
    finally:
        # Restore — best-effort.
        try:
            dut.deviceio.SetRelayOutputState(relay.token, original_idle)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Digital inputs (3-1-*) — read-only
# ---------------------------------------------------------------------------

@register("DEVICEIO-3-1-1", profiles={"S", "T", "D"}, mandatory=False,
          requires_services={"devicemgmt", "deviceio"})
def test_io_get_digital_inputs(dut: DUT, spec) -> None:
    """DEVICEIO.html#tc.DEVICEIO-3-1-1 — IO GETDIGITALINPUTS."""
    inputs = dut.deviceio.GetDigitalInputs() or []
    for d in inputs:
        assert getattr(d, "token", None), "digital input missing token"


@register("DEVICEIO-3-1-2", profiles={"D"}, mandatory=False,
          requires_services={"devicemgmt", "deviceio"})
def test_io_get_digital_inputs_quantity(dut: DUT, spec) -> None:
    """DEVICEIO.html#tc.DEVICEIO-3-1-2 — quantity returned by
    GetDigitalInputs must match the DUT's advertised capability.
    """
    inputs = dut.deviceio.GetDigitalInputs() or []
    caps = dut.deviceio.GetServiceCapabilities()
    advertised = getattr(caps, "InputConnectors", None)
    if advertised is None:
        pytest.skip("DUT does not advertise InputConnectors capability")
    assert len(inputs) == int(advertised), (
        f"GetDigitalInputs returned {len(inputs)} entries, "
        f"capabilities.InputConnectors says {advertised}"
    )


@register("DEVICEIO-3-1-3", profiles={"D"}, mandatory=False,
          requires_services={"devicemgmt", "deviceio"})
def test_io_get_digital_input_config_options(dut: DUT, spec) -> None:
    """DEVICEIO.html#tc.DEVICEIO-3-1-3 — IO GETDIGITALINPUTCONFIGURATIONOPTIONS."""
    inputs = dut.deviceio.GetDigitalInputs() or []
    if not inputs:
        pytest.skip("DUT has no digital inputs")
    opts = dut.deviceio.GetDigitalInputConfigurationOptions(inputs[0].token)
    assert opts is not None, "GetDigitalInputConfigurationOptions returned None"


# ---------------------------------------------------------------------------
# Cross-service consistency
# ---------------------------------------------------------------------------

@register("DEVICEIO-5-1-1", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "media", "deviceio"})
def test_io_video_sources_consistency_with_media(dut: DUT, spec) -> None:
    """DEVICEIO.html#tc.DEVICEIO-5-1-1 — GetVideoSources from the
    DeviceIO service must match GetVideoSources from the Media service.
    """
    io_sources = dut.deviceio.GetVideoSources() or []
    media_sources = dut.media.GetVideoSources() or []
    io_tokens = sorted(getattr(s, "token", None) for s in io_sources)
    media_tokens = sorted(getattr(s, "token", None) for s in media_sources)
    assert io_tokens == media_tokens, (
        f"VideoSource token sets differ between services: "
        f"deviceio={io_tokens}  media={media_tokens}"
    )
