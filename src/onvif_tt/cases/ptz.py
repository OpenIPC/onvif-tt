"""Profile S PTZ implementations from PTZ.html.

Read-only smoke tests — no movement commands (those need explicit opt-in
via ``--allow-writes`` once we add that flag).
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime.dut import DUT
from ..runtime.fault import assert_soap_fault


@register("PTZ-1-1-1", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_get_nodes(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-1-1-1 — PTZ NODES.

    GetNodes must return at least one PTZNode entry with a token.
    """
    nodes = dut.ptz.GetNodes()
    assert nodes, "PTZ.GetNodes returned empty"
    for n in nodes:
        assert getattr(n, "token", None), "PTZNode missing token"


@register("PTZ-1-1-2", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_get_node(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-1-1-2 — PTZ NODE.

    GetNode for the first node token returned by GetNodes must match.
    """
    nodes = dut.ptz.GetNodes()
    assert nodes, "no nodes — cannot exercise GetNode"
    first_token = nodes[0].token
    node = dut.ptz.GetNode(first_token)
    assert node is not None
    assert node.token == first_token, (
        f"GetNode token mismatch: asked {first_token!r}, got {node.token!r}"
    )


@register("PTZ-1-1-4", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"},
          xfail_on=[
              {
                  "Manufacturer": "H264",
                  "reason": "Xiongmai stock firmware (Manufacturer=H264) "
                            "closes the TCP connection on invalid PTZ node "
                            "token instead of returning a SOAP Fault.",
              },
          ])
def test_ptz_soap_fault_invalid_node(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-1-1-4 — PTZ SOAP FAULT MESSAGE.

    Querying a bogus PTZ node token must return a SOAP fault, not silently
    succeed with empty data.
    """
    assert_soap_fault(lambda: dut.ptz.GetNode("__definitely_not_a_real_token__"))


# ---------------------------------------------------------------------------
# PTZ Move write ops — actuate the pan/tilt/zoom motors. --allow-writes only.
#
# Helper: every PTZ Move test starts from "what profile + what node does
# this device expose?". If GetProfiles returns no profile with a
# PTZConfiguration attached, we can't address the move — skip.
# ---------------------------------------------------------------------------

def _first_ptz_profile_token(dut: DUT) -> str:
    """Return the first media profile that has a PTZConfiguration."""
    profiles = dut.media.GetProfiles() or []
    for p in profiles:
        if getattr(p, "PTZConfiguration", None):
            return p.token
    pytest.skip("no media profile with a PTZConfiguration — cannot run PTZ Move")


def _get_node_for_profile(dut: DUT, profile_token: str):
    """Resolve the PTZNode for the given profile via its PTZConfiguration."""
    profiles = dut.media.GetProfiles() or []
    for p in profiles:
        if p.token != profile_token:
            continue
        node_token = getattr(p.PTZConfiguration, "NodeToken", None)
        if not node_token:
            return None
        return dut.ptz.GetNode(node_token)
    return None


@register("PTZ-3-1-1", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media", "ptz"},
          requires_writes=True)
def test_ptz_absolute_move(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-3-1-1 — ABSOLUTE MOVE.

    Issue an AbsoluteMove to the centre of the supported space (pan=0,
    tilt=0, zoom at the bottom of its range). We then verify the device
    accepted the command; we don't wait for it to physically arrive.
    """
    profile_token = _first_ptz_profile_token(dut)
    node = _get_node_for_profile(dut, profile_token)
    if node is None:
        pytest.skip("could not resolve PTZNode for profile")
    supported = getattr(node, "SupportedPTZSpaces", None)
    if supported is None or not getattr(supported, "AbsolutePanTiltPositionSpace", None):
        pytest.skip("DUT does not support AbsoluteMove for PanTilt")

    req = dut.ptz.create_type("AbsoluteMove")
    req.ProfileToken = profile_token
    req.Position = {"PanTilt": {"x": 0.0, "y": 0.0}}
    # AbsoluteMoveResponse body is empty by spec — success = no Fault.
    dut.ptz.AbsoluteMove(req)


@register("PTZ-3-1-3", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media", "ptz"},
          requires_writes=True)
def test_ptz_relative_move(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-3-1-3 — RELATIVE MOVE.

    Nudge PanTilt by zero — exercises the codepath without physically
    moving the head.
    """
    profile_token = _first_ptz_profile_token(dut)
    node = _get_node_for_profile(dut, profile_token)
    if node is None:
        pytest.skip("could not resolve PTZNode for profile")
    supported = getattr(node, "SupportedPTZSpaces", None)
    if supported is None or not getattr(supported, "RelativePanTiltTranslationSpace", None):
        pytest.skip("DUT does not support RelativeMove for PanTilt")

    req = dut.ptz.create_type("RelativeMove")
    req.ProfileToken = profile_token
    req.Translation = {"PanTilt": {"x": 0.0, "y": 0.0}}
    # RelativeMoveResponse body is empty by spec.
    dut.ptz.RelativeMove(req)


@register("PTZ-3-1-5", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media", "ptz"},
          requires_writes=True)
def test_ptz_continuous_move_and_stop(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-3-1-5 — CONTINUOUS MOVE & STOP.

    Start a continuous PanTilt move at zero velocity (so the head
    doesn't actually move) and then immediately Stop. This tests both
    sides of the Move/Stop contract without physically rotating the
    device.
    """
    profile_token = _first_ptz_profile_token(dut)
    node = _get_node_for_profile(dut, profile_token)
    if node is None:
        pytest.skip("could not resolve PTZNode for profile")
    supported = getattr(node, "SupportedPTZSpaces", None)
    if supported is None or not getattr(supported, "ContinuousPanTiltVelocitySpace", None):
        pytest.skip("DUT does not support ContinuousMove for PanTilt")

    move_req = dut.ptz.create_type("ContinuousMove")
    move_req.ProfileToken = profile_token
    move_req.Velocity = {"PanTilt": {"x": 0.0, "y": 0.0}}
    try:
        dut.ptz.ContinuousMove(move_req)
    finally:
        stop_req = dut.ptz.create_type("Stop")
        stop_req.ProfileToken = profile_token
        stop_req.PanTilt = True
        stop_req.Zoom = True
        dut.ptz.Stop(stop_req)
