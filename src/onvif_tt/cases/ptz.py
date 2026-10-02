"""Profile S PTZ implementations from PTZ.html.

Read-only tests run by default. Anything that moves the head or lens, or
changes a configuration, is ``requires_writes=True`` and runs only with
``--allow-writes``. Moves follow the node's advertised spaces: a zoom lens
with no pan/tilt head is exercised on zoom, not skipped on pan/tilt.
"""

from __future__ import annotations

import datetime
import time

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

    For each continuous movement the configuration offers (PanTilt, Zoom):
    ContinuousMove at a real velocity, Stop, and GetStatus must then report
    MoveStatus IDLE or UNKNOWN for that axis. A zero velocity would not
    exercise the move at all.
    """
    profile_token = _first_ptz_profile_token(dut)
    axes = _continuous_axes(dut, profile_token)
    if not axes:
        pytest.skip("configuration offers no continuous movement with a non-zero velocity")
    for axis, velocity in axes:
        try:
            _continuous_move(dut, profile_token, velocity)
            time.sleep(0.5)
        finally:
            _stop(dut, profile_token)
        time.sleep(0.5)
        _assert_idle(dut, profile_token, axis)



# ---------------------------------------------------------------------------
# Helpers for the configuration and continuous-move cases.
# ---------------------------------------------------------------------------

def _configurations(dut: DUT):
    configs = dut.ptz.GetConfigurations() or []
    assert configs, "GetConfigurations returned no PTZConfiguration"
    return configs


def _config_for_profile(dut: DUT, profile_token: str):
    for p in dut.media.GetProfiles() or []:
        if p.token == profile_token:
            return p.PTZConfiguration
    pytest.skip("profile has no PTZConfiguration")


def _options(dut: DUT, profile_token: str):
    cfg = _config_for_profile(dut, profile_token)
    opts = dut.ptz.GetConfigurationOptions(cfg.token)
    assert opts is not None and getattr(opts, "Spaces", None) is not None, (
        "GetConfigurationOptions returned no Spaces"
    )
    return cfg, opts


def _nonzero(rng):
    """A non-zero value inside `rng`, its Max unless that is 0; None if the
    range holds nothing but 0."""
    for v in (rng.Max, rng.Min):
        if v:
            return v
    return None


def _within(rng, v):
    return rng is not None and rng.Min <= v <= rng.Max


def _pick_space(spaces, default_uri):
    """The configuration's default space when the options offer it, else the
    first one offered."""
    return next((s for s in spaces if s.URI == default_uri), spaces[0])


def _continuous_axes(dut: DUT, profile_token: str):
    """[(axis, velocity)] for each continuous movement the profile's options
    offer, in an offered space (named in the vector) at a non-zero velocity
    inside its range. An axis whose range holds only 0 is left out."""
    cfg, opts = _options(dut, profile_token)
    axes = []
    pt = getattr(opts.Spaces, "ContinuousPanTiltVelocitySpace", None) or []
    if pt:
        s = _pick_space(pt, getattr(cfg, "DefaultContinuousPanTiltVelocitySpace", None))
        x = _nonzero(s.XRange)
        y = 0.0 if _within(s.YRange, 0.0) else (s.YRange.Max if s.YRange else 0.0)
        if x is not None:
            axes.append(("PanTilt", {"PanTilt": {"x": x, "y": y, "space": s.URI}}))
    z = getattr(opts.Spaces, "ContinuousZoomVelocitySpace", None) or []
    if z:
        s = _pick_space(z, getattr(cfg, "DefaultContinuousZoomVelocitySpace", None))
        x = _nonzero(s.XRange)
        if x is not None:
            axes.append(("Zoom", {"Zoom": {"x": x, "space": s.URI}}))
    return axes


def _continuous_move(dut: DUT, profile_token: str, velocity, timeout=None) -> None:
    req = dut.ptz.create_type("ContinuousMove")
    req.ProfileToken = profile_token
    req.Velocity = velocity
    if timeout is not None:
        req.Timeout = timeout
    dut.ptz.ContinuousMove(req)  # empty response by spec: success = no fault


def _stop(dut: DUT, profile_token: str) -> None:
    req = dut.ptz.create_type("Stop")
    req.ProfileToken = profile_token
    req.PanTilt = True
    req.Zoom = True
    dut.ptz.Stop(req)


def _assert_idle(dut: DUT, profile_token: str, axis: str) -> None:
    """MoveStatus for `axis` is IDLE or UNKNOWN. A device may leave MoveStatus
    out only when its capabilities say it does not report one; then there is
    nothing to observe."""
    status = dut.ptz.GetStatus(profile_token)
    move = getattr(getattr(status, "MoveStatus", None), axis, None)
    if move is None:
        caps = dut.ptz.GetServiceCapabilities()
        assert not getattr(caps, "MoveStatus", False), (
            f"GetStatus has no MoveStatus.{axis}, though the capabilities say MoveStatus"
        )
        return
    assert move in ("IDLE", "UNKNOWN"), (
        f"MoveStatus.{axis} is {move!r} after the move ended; expected IDLE or UNKNOWN"
    )


def _dump(obj):
    """A zeep object as plain data, for comparing two answers field by field."""
    from zeep.helpers import serialize_object
    return serialize_object(obj, dict)


# ---------------------------------------------------------------------------
# PTZ configuration — read-only.
# ---------------------------------------------------------------------------

@register("PTZ-2-1-1", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_configurations(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-2-1-1 — PTZ CONFIGURATIONS.

    GetConfigurations returns at least one PTZConfiguration, each with a
    token and the node it belongs to.
    """
    for c in _configurations(dut):
        assert c.token, "PTZConfiguration without a token"
        assert c.NodeToken, f"PTZConfiguration {c.token} without a NodeToken"


@register("PTZ-2-1-2", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_configuration(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-2-1-2 — PTZ CONFIGURATION.

    GetConfiguration for an existing token returns that configuration, with a
    NodeToken and at least one default space.
    """
    first = _configurations(dut)[0]
    c = dut.ptz.GetConfiguration(first.token)
    assert c is not None and c.token == first.token
    assert c.NodeToken, "PTZConfiguration without a NodeToken"
    defaults = [
        "DefaultAbsolutePantTiltPositionSpace", "DefaultAbsoluteZoomPositionSpace",
        "DefaultRelativePanTiltTranslationSpace", "DefaultRelativeZoomTranslationSpace",
        "DefaultContinuousPanTiltVelocitySpace", "DefaultContinuousZoomVelocitySpace",
    ]
    assert any(getattr(c, d, None) for d in defaults), (
        "PTZConfiguration names no default space"
    )


@register("PTZ-2-1-3", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_configuration_options(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-2-1-3 — PTZ CONFIGURATION OPTIONS.

    GetConfigurationOptions returns Spaces and a PTZTimeout range.
    """
    c = _configurations(dut)[0]
    o = dut.ptz.GetConfigurationOptions(c.token)
    assert o is not None, "GetConfigurationOptions returned nothing"
    assert getattr(o, "Spaces", None) is not None, "PTZConfigurationOptions without Spaces"
    to = getattr(o, "PTZTimeout", None)
    assert to is not None, "PTZConfigurationOptions without PTZTimeout"
    assert to.Min <= to.Max, f"PTZTimeout range inverted: {to.Min} > {to.Max}"


@register("PTZ-2-1-5", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_configurations_and_configuration_consistency(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-2-1-5 — GetConfiguration answers each configuration
    exactly as GetConfigurations listed it."""
    for c in _configurations(dut):
        one = dut.ptz.GetConfiguration(c.token)
        assert _dump(one) == _dump(c), (
            f"GetConfiguration({c.token}) differs from its GetConfigurations entry"
        )


def _space_uris(spaces, name: str) -> set[str]:
    return {s.URI for s in (getattr(spaces, name, None) or [])}


_DEFAULT_TO_SPACE = {
    "DefaultAbsolutePantTiltPositionSpace": "AbsolutePanTiltPositionSpace",
    "DefaultAbsoluteZoomPositionSpace": "AbsoluteZoomPositionSpace",
    "DefaultRelativePanTiltTranslationSpace": "RelativePanTiltTranslationSpace",
    "DefaultRelativeZoomTranslationSpace": "RelativeZoomTranslationSpace",
    "DefaultContinuousPanTiltVelocitySpace": "ContinuousPanTiltVelocitySpace",
    "DefaultContinuousZoomVelocitySpace": "ContinuousZoomVelocitySpace",
}


@register("PTZ-2-1-6", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_configurations_and_nodes_consistency(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-2-1-6 — every configuration's node exists, and every
    default space it names is one that node supports."""
    nodes = {n.token: n for n in (dut.ptz.GetNodes() or [])}
    for c in _configurations(dut):
        assert c.NodeToken in nodes, f"{c.token} refers to unknown node {c.NodeToken!r}"
        supported = nodes[c.NodeToken].SupportedPTZSpaces
        for default, space in _DEFAULT_TO_SPACE.items():
            uri = getattr(c, default, None)
            if uri:
                assert uri in _space_uris(supported, space), (
                    f"{c.token}.{default} {uri} is not among the node's {space}"
                )


@register("PTZ-2-1-7", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_configurations_and_options_consistency(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-2-1-7 — each configuration's defaults are among its
    options: default spaces offered, DefaultPTZTimeout inside PTZTimeout."""
    for c in _configurations(dut):
        o = dut.ptz.GetConfigurationOptions(c.token)
        for default, space in _DEFAULT_TO_SPACE.items():
            uri = getattr(c, default, None)
            if uri:
                assert uri in _space_uris(o.Spaces, space), (
                    f"{c.token}.{default} {uri} is not among the options' {space}"
                )
        dt, rng = getattr(c, "DefaultPTZTimeout", None), getattr(o, "PTZTimeout", None)
        if dt is not None and rng is not None:
            assert rng.Min <= dt <= rng.Max, (
                f"{c.token}.DefaultPTZTimeout {dt} outside PTZTimeout {rng.Min}..{rng.Max}"
            )


@register("PTZ-2-1-10", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock firmware answers SetConfiguration for a "
                        "configuration token that does not exist with success.",
          }])
def test_ptz_set_configuration_invalid_token_fault(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-2-1-10 — SetConfiguration naming a configuration that
    does not exist is a SOAP fault (env:Sender/ter:InvalidArgVal/ter:NoConfig).
    Changes nothing, so it is not a write."""
    c = _configurations(dut)[0]
    bad = _dump(c)
    bad["token"] = "__definitely_not_a_real_token__"
    req = dut.ptz.create_type("SetConfiguration")
    req.PTZConfiguration = bad
    req.ForcePersistence = False
    assert_soap_fault(dut.ptz.SetConfiguration, req)


@register("PTZ-2-1-9", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"},
          requires_writes=True)
def test_ptz_set_configuration(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-2-1-9 — PTZ SET CONFIGURATION.

    Set DefaultPTZTimeout to the options' Max (or Min, when it already is the
    Max) without persistence, read it back, and put the original back. A
    configuration with no DefaultPTZTimeout has no value to put back, so it is
    left alone.
    """
    c = _configurations(dut)[0]
    o = dut.ptz.GetConfigurationOptions(c.token)
    rng = getattr(o, "PTZTimeout", None)
    if rng is None:
        pytest.skip("no PTZTimeout range to set DefaultPTZTimeout within")
    original = c.DefaultPTZTimeout
    if original is None:
        pytest.skip("no DefaultPTZTimeout to restore after the change")
    target = rng.Min if original == rng.Max else rng.Max

    def put(timeout):
        cfg = _dump(c)
        cfg["DefaultPTZTimeout"] = timeout
        req = dut.ptz.create_type("SetConfiguration")
        req.PTZConfiguration = cfg
        req.ForcePersistence = False
        dut.ptz.SetConfiguration(req)

    put(target)
    try:
        got = dut.ptz.GetConfiguration(c.token).DefaultPTZTimeout
        assert got == target, f"DefaultPTZTimeout reads {got} after setting {target}"
    finally:
        put(original)


# ---------------------------------------------------------------------------
# Continuous movement — writes.
# ---------------------------------------------------------------------------

@register("PTZ-3-1-4", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media", "ptz"},
          requires_writes=True)
def test_ptz_continuous_move_timeout(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-3-1-4 — PTZ CONTINUOUS MOVE.

    For each continuous movement offered: ContinuousMove with a Timeout of
    60 s (as the spec has it), or the nearest the options' PTZTimeout range
    allows, and once it has run out GetStatus must report MoveStatus IDLE or
    UNKNOWN for that axis -- the move ended by itself.
    """
    profile_token = _first_ptz_profile_token(dut)
    _, opts = _options(dut, profile_token)
    timeout = datetime.timedelta(seconds=60)
    rng = getattr(opts, "PTZTimeout", None)
    if rng is not None:
        timeout = min(max(timeout, rng.Min), rng.Max)
    axes = _continuous_axes(dut, profile_token)
    if not axes:
        pytest.skip("configuration offers no continuous movement with a non-zero velocity")
    for axis, velocity in axes:
        try:
            _continuous_move(dut, profile_token, velocity, timeout=timeout)
            time.sleep(timeout.total_seconds() + 1)
            _assert_idle(dut, profile_token, axis)
        finally:
            _stop(dut, profile_token)


def _generic_velocity_move(dut: DUT, space_name: str, uri: str, two_d: bool) -> None:
    """Through the first profile whose configuration options offer `space_name`
    -- each must include the generic space -- move at both ends of the generic
    range."""
    found = None
    for p in dut.media.GetProfiles() or []:
        cfg = getattr(p, "PTZConfiguration", None)
        if not cfg:
            continue
        opts = dut.ptz.GetConfigurationOptions(cfg.token)
        spaces = getattr(getattr(opts, "Spaces", None), space_name, None) or []
        if spaces:
            found = (p, spaces)
            break
    if found is None:
        pytest.skip(f"no profile's PTZ configuration offers {space_name}")
    profile, spaces = found
    generic = [s for s in spaces if s.URI == uri]
    assert generic, f"profile {profile.token} offers {space_name} but not the generic {uri}"
    g = generic[0]
    assert g.XRange is not None and g.XRange.Min <= g.XRange.Max, "XRange missing or inverted"
    if two_d:
        assert g.YRange is not None and g.YRange.Min <= g.YRange.Max, "YRange missing or inverted"
    try:
        for edge in ("Max", "Min"):
            x = getattr(g.XRange, edge)
            if two_d:
                v = {"PanTilt": {"x": x, "y": getattr(g.YRange, edge), "space": uri}}
            else:
                v = {"Zoom": {"x": x, "space": uri}}
            _continuous_move(dut, profile.token, v)
            time.sleep(0.5)
    finally:
        _stop(dut, profile.token)


@register("PTZ-7-3-3", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media", "ptz"},
          requires_writes=True)
def test_ptz_generic_pantilt_velocity_space(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-7-3-3 — GENERIC PAN/TILT VELOCITY SPACE: ContinuousMove
    at both ends of the generic space's ranges is accepted."""
    _generic_velocity_move(dut, "ContinuousPanTiltVelocitySpace",
                           "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace",
                           two_d=True)


@register("PTZ-7-3-4", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media", "ptz"},
          requires_writes=True)
def test_ptz_generic_zoom_velocity_space(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-7-3-4 — GENERIC ZOOM VELOCITY SPACE: ContinuousMove at
    both ends of the generic zoom velocity range is accepted."""
    _generic_velocity_move(dut, "ContinuousZoomVelocitySpace",
                           "http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace",
                           two_d=False)


# ---------------------------------------------------------------------------
# Service capabilities — read-only.
# ---------------------------------------------------------------------------

_PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
_XS_BOOLEAN = {"true": True, "1": True, "false": False, "0": False}
_PTZ_CAP_FIELDS = ("EFlip", "Reverse", "GetCompatibleConfigurations", "MoveStatus",
                   "StatusPosition")
_XM_NS_ATTRS = {
    "Manufacturer": "H264",
    "reason": "Xiongmai stock firmware puts the PTZ Capabilities attributes in the "
              "tt: namespace (tt:EFlip, tt:Reverse) instead of unqualified, so no "
              "capability parses; it embeds none in GetServices either.",
}


@register("PTZ-8-1-1", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"},
          xfail_on=[_XM_NS_ATTRS])
def test_ptz_service_capabilities(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-8-1-1 — PTZ SERVICE CAPABILITIES: a valid answer, with
    its boolean capabilities readable."""
    caps = dut.ptz.GetServiceCapabilities()
    assert caps is not None, "GetServiceCapabilities returned nothing"
    present = [f for f in _PTZ_CAP_FIELDS if getattr(caps, f, None) is not None]
    assert present, "no PTZ capability could be read from the answer"
    for f in present:
        assert isinstance(getattr(caps, f), bool), f"Capabilities.{f} is not a bool"


@register("PTZ-8-1-2", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"},
          xfail_on=[_XM_NS_ATTRS])
def test_ptz_get_services_and_capabilities_consistency(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-8-1-2 — the PTZ capabilities GetServices(true) embeds are
    the ones GetServiceCapabilities answers."""
    services = dut.devicemgmt.GetServices(True) or []
    ptz = [s for s in services if s.Namespace == _PTZ_NS]
    assert ptz, "PTZ service missing from GetServices"
    embedded = getattr(ptz[0], "Capabilities", None)
    assert embedded is not None, "GetServices(true) carries no PTZ Capabilities"
    direct = dut.ptz.GetServiceCapabilities()
    # The embedded blob is an xs:any: zeep hands it over as an lxml element.
    el = embedded
    if hasattr(el, "_value_1"):
        el = el._value_1[0] if isinstance(el._value_1, list) else el._value_1
    attrs = dict(getattr(el, "attrib", {}) or {})
    for f in _PTZ_CAP_FIELDS:
        want = getattr(direct, f, None)
        if want is None:
            continue
        got = attrs.get(f)
        assert got is not None, f"embedded Capabilities lacks {f}"
        parsed = _XS_BOOLEAN.get(got.strip())
        assert parsed is not None, f"embedded {f}={got!r} is not an xs:boolean"
        assert parsed == want, (
            f"{f}: GetServices says {got}, GetServiceCapabilities says {want}"
        )
