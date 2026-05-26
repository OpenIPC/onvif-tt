"""RECEIVER service tests — the Receiver service models an ONVIF device
that pulls a stream FROM another device (NVR-style behaviour). Most
camera/encoder devices don't expose this service.
"""

from __future__ import annotations

import pytest
import zeep.exceptions

from ..registry import register
from ..runtime.dut import DUT


_RECEIVER_NS = "http://www.onvif.org/ver10/receiver/wsdl"
_INVALID_TOKEN = "__definitely_not_a_real_receiver_token__"


# ---------------------------------------------------------------------------
# Capabilities + GetServices consistency
# ---------------------------------------------------------------------------

@register("RECEIVER-1-1-1", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "receiver"})
def test_receiver_service_capabilities(dut: DUT, spec) -> None:
    """RECEIVER.html#tc.RECEIVER-1-1-1 — service capabilities."""
    caps = dut.receiver.GetServiceCapabilities()
    assert caps is not None, "GetServiceCapabilities returned None"
    # SupportedReceivers is mandatory and must be >= 1.
    val = getattr(caps, "SupportedReceivers", None) or getattr(caps, "MaximumReceivers", None)
    if val is not None:
        assert int(val) >= 1, f"SupportedReceivers={val} (must be >= 1)"


@register("RECEIVER-1-1-2", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "receiver"})
def test_receiver_get_services_consistency(dut: DUT, spec) -> None:
    """RECEIVER.html#tc.RECEIVER-1-1-2 — GetServices(true) must include
    the receiver service with populated Capabilities.
    """
    services = dut.devicemgmt.GetServices(True) or []
    recv_entries = [s for s in services if s.Namespace == _RECEIVER_NS]
    assert recv_entries, "Receiver service missing from GetServices"
    rcv = recv_entries[0]
    assert rcv.XAddr, "Receiver service XAddr empty"
    assert getattr(rcv, "Capabilities", None) is not None, (
        "GetServices(True) did not include Capabilities for receiver"
    )


# ---------------------------------------------------------------------------
# Read-only enumeration
# ---------------------------------------------------------------------------

@register("RECEIVER-2-1-1", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "receiver"})
def test_receiver_get_receivers(dut: DUT, spec) -> None:
    """RECEIVER.html#tc.RECEIVER-2-1-1 — GET RECEIVERS.

    Returns all configured receivers. Empty list is acceptable (device
    hasn't been provisioned). Per-receiver token must be present.
    """
    receivers = dut.receiver.GetReceivers() or []
    for r in receivers:
        assert getattr(r, "Token", None) or getattr(r, "token", None), (
            "receiver missing token"
        )


@register("RECEIVER-2-1-2", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "receiver"})
def test_receiver_get_receiver(dut: DUT, spec) -> None:
    """RECEIVER.html#tc.RECEIVER-2-1-2 — GET RECEIVER."""
    receivers = dut.receiver.GetReceivers() or []
    if not receivers:
        pytest.skip("DUT has no receivers configured")
    token = getattr(receivers[0], "Token", None) or receivers[0].token
    detail = dut.receiver.GetReceiver(token)
    assert detail is not None, "GetReceiver returned None"
    got_token = getattr(detail, "Token", None) or getattr(detail, "token", None)
    assert got_token == token, (
        f"GetReceiver echoed wrong token: asked {token!r}, got {got_token!r}"
    )


# ---------------------------------------------------------------------------
# Invalid-token negative tests
# ---------------------------------------------------------------------------

@register("RECEIVER-2-1-10", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "receiver"})
def test_receiver_delete_invalid_token(dut: DUT, spec) -> None:
    """RECEIVER.html#tc.RECEIVER-2-1-10 — DeleteReceiver with invalid
    token → SOAP Fault.
    """
    try:
        dut.receiver.DeleteReceiver(_INVALID_TOKEN)
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on DeleteReceiver(invalid token)")


@register("RECEIVER-2-1-13", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "receiver"})
def test_receiver_set_mode_invalid_token(dut: DUT, spec) -> None:
    """RECEIVER.html#tc.RECEIVER-2-1-13 — SetReceiverMode with invalid
    token → SOAP Fault.
    """
    try:
        dut.receiver.SetReceiverMode(_INVALID_TOKEN, "AlwaysConnect")
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on SetReceiverMode(invalid token)")


@register("RECEIVER-2-1-19", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "receiver"})
def test_receiver_configure_invalid_token(dut: DUT, spec) -> None:
    """RECEIVER.html#tc.RECEIVER-2-1-19 — ConfigureReceiver with invalid
    token → SOAP Fault.
    """
    cfg = dut.receiver.create_type("ConfigureReceiver")
    cfg.ReceiverToken = _INVALID_TOKEN
    cfg.Configuration = {
        "Mode": "AlwaysConnect",
        "MediaUri": "rtsp://example.invalid/onvif",
        "StreamSetup": {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        },
    }
    try:
        dut.receiver.ConfigureReceiver(cfg)
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on ConfigureReceiver(invalid token)")
