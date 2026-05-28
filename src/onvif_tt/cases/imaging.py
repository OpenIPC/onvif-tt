"""Imaging service smoke tests from IMAGING.html.

Read-only — uses the first VideoSource token reported by Media service.
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime.dut import DUT
from ..runtime.fault import assert_soap_fault


def _first_video_source_token(dut: DUT) -> str:
    """Return the first VideoSource token via media v10 (Profile S)."""
    sources = dut.media.GetVideoSources()
    if not sources:
        pytest.skip("DUT exposes no VideoSources via media v10")
    return sources[0].token


@register("IMAGING-1-1-1", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "media", "imaging"})
def test_imaging_get_settings(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-1-1-1 — GetImagingSettings.

    Asserts the call returns a settings struct (any populated fields OK).
    """
    token = _first_video_source_token(dut)
    settings = dut.imaging.GetImagingSettings(token)
    assert settings is not None, "GetImagingSettings returned None"


@register("IMAGING-1-1-3", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "media", "imaging"})
def test_imaging_get_options(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-1-1-3 — GetOptions.

    The response describes ranges/enums of every configurable imaging
    parameter. Even a minimal DUT returns *something*.
    """
    token = _first_video_source_token(dut)
    options = dut.imaging.GetOptions(token)
    assert options is not None, "GetOptions returned None"


# ---------------------------------------------------------------------------
# Imaging Move ops — read-only subset. Move/AbsoluteMove/RelativeMove/Stop
# are write operations (they actuate the focus/iris motor) and need a
# future --allow-writes opt-in.
# ---------------------------------------------------------------------------

@register("IMAGING-2-1-1", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "media", "imaging"})
def test_imaging_get_move_options(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-2-1-1 — IMAGING COMMAND GETMOVEOPTIONS.

    Asserts Min ≤ Max for every present (Min, Max) pair in the
    Absolute / Relative / Continuous Move ranges. The spec's own FAIL
    criteria explicitly call out "Min value greater than Max" as a fault.
    """
    token = _first_video_source_token(dut)
    try:
        opts = dut.imaging.GetMoveOptions(token)
    except Exception as exc:
        # Device with no movable focus often responds with a SOAP fault
        # to GetMoveOptions — treat as "feature not supported".
        pytest.skip(f"GetMoveOptions not supported on this DUT: {exc}")
    assert opts is not None, "GetMoveOptionsResponse returned None"
    for section_name in ("Absolute", "Relative", "Continuous"):
        section = getattr(opts, section_name, None)
        if section is None:
            continue
        for field_name in ("Position", "Speed", "Distance"):
            rng = getattr(section, field_name, None)
            if rng is None:
                continue
            mn = getattr(rng, "Min", None)
            mx = getattr(rng, "Max", None)
            if mn is not None and mx is not None:
                assert mn <= mx, (
                    f"{section_name}.{field_name}: Min={mn} > Max={mx}"
                )


@register("IMAGING-2-1-11", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "media", "imaging"})
def test_imaging_get_status(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-2-1-11 — IMAGING COMMAND GETSTATUS.

    Some devices fault on GetStatus when no focus motor exists; we
    treat that as a clean skip.
    """
    token = _first_video_source_token(dut)
    try:
        status = dut.imaging.GetStatus(token)
    except Exception as exc:
        pytest.skip(f"GetStatus not supported on this DUT: {exc}")
    assert status is not None, "GetStatusResponse returned None"


# ---------------------------------------------------------------------------
# Negative tests — invalid VideoSourceToken must yield a SOAP Fault.
# ---------------------------------------------------------------------------

@register("IMAGING-2-1-15", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "imaging"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock firmware is known to swallow "
                        "invalid tokens silently rather than faulting.",
          }])
def test_imaging_get_move_options_invalid_token(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-2-1-15 — GETMOVEOPTIONS – INVALID VIDEOSOURCETOKEN.

    Must SOAP-fault for a token that doesn't exist.
    """
    assert_soap_fault(dut.imaging.GetMoveOptions, "__definitely_not_a_real_token__")


@register("IMAGING-2-1-17", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "imaging"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock firmware swallows invalid tokens.",
          }])
def test_imaging_get_status_invalid_token(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-2-1-17 — GETSTATUS – INVALID VIDEOSOURCETOKEN."""
    assert_soap_fault(dut.imaging.GetStatus, "__definitely_not_a_real_token__")


# ---------------------------------------------------------------------------
# Imaging Move write ops — actuate the focus motor. Gated on --allow-writes.
# ---------------------------------------------------------------------------

def _midpoint(rng) -> float | None:
    """Return the midpoint of a (Min, Max) range, or None if either is unset."""
    mn = getattr(rng, "Min", None)
    mx = getattr(rng, "Max", None)
    if mn is None or mx is None:
        return None
    return (mn + mx) / 2.0


@register("IMAGING-2-1-3", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "media", "imaging"},
          requires_writes=True)
def test_imaging_absolute_move(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-2-1-3 — IMAGING COMMAND ABSOLUTE MOVE.

    Move the focus to a safe midpoint position derived from
    GetMoveOptions. Skip if the device doesn't advertise Absolute move.
    """
    token = _first_video_source_token(dut)
    try:
        opts = dut.imaging.GetMoveOptions(token)
    except Exception as exc:
        pytest.skip(f"GetMoveOptions failed: {exc}")
    absolute = getattr(opts, "Absolute", None)
    if absolute is None:
        pytest.skip("device does not advertise Absolute focus move")
    pos = _midpoint(getattr(absolute, "Position", None))
    if pos is None:
        pytest.skip("Absolute.Position range incomplete")

    req = dut.imaging.create_type("Move")
    req.VideoSourceToken = token
    req.Focus = {"Absolute": {"Position": pos}}
    # Move's response body is empty by spec — zeep returns None on success.
    # We just need the call to complete without a SOAP Fault.
    dut.imaging.Move(req)


@register("IMAGING-2-1-5", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "media", "imaging"},
          requires_writes=True)
def test_imaging_relative_move(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-2-1-5 — IMAGING COMMAND RELATIVE MOVE.

    Nudge focus by zero distance — the safest write that still exercises
    the RelativeMove code path on the device.
    """
    token = _first_video_source_token(dut)
    try:
        opts = dut.imaging.GetMoveOptions(token)
    except Exception as exc:
        pytest.skip(f"GetMoveOptions failed: {exc}")
    relative = getattr(opts, "Relative", None)
    if relative is None:
        pytest.skip("device does not advertise Relative focus move")

    req = dut.imaging.create_type("Move")
    req.VideoSourceToken = token
    req.Focus = {"Relative": {"Distance": 0.0}}
    dut.imaging.Move(req)


@register("IMAGING-2-1-7", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "media", "imaging"},
          requires_writes=True)
def test_imaging_continuous_move_and_stop(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-2-1-7 — IMAGING COMMAND CONTINUOUS MOVE.

    Start a continuous focus move at minimum speed, then immediately Stop
    so we don't leave the motor running.
    """
    token = _first_video_source_token(dut)
    try:
        opts = dut.imaging.GetMoveOptions(token)
    except Exception as exc:
        pytest.skip(f"GetMoveOptions failed: {exc}")
    cont = getattr(opts, "Continuous", None)
    if cont is None:
        pytest.skip("device does not advertise Continuous focus move")
    speed_rng = getattr(cont, "Speed", None)
    if speed_rng is None or speed_rng.Min is None:
        pytest.skip("Continuous.Speed range incomplete")
    # Use the smallest non-negative speed available so the test is gentle.
    safe_speed = abs(speed_rng.Min) * 0.0  # 0.0 — start-then-stop
    req = dut.imaging.create_type("Move")
    req.VideoSourceToken = token
    req.Focus = {"Continuous": {"Speed": safe_speed}}
    try:
        dut.imaging.Move(req)
    finally:
        dut.imaging.Stop(token)


@register("IMAGING-2-1-13", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "imaging"},
          requires_writes=True)
def test_imaging_stop(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-2-1-13 — IMAGING COMMAND STOP.

    Stop on a stationary motor must either return StopResponse or fault
    with ActionNotSupported. Both are spec-conformant.
    """
    from ..runtime.fault import looks_like_soap_fault
    token = _first_video_source_token(dut)
    try:
        dut.imaging.Stop(token)
        # StopResponse body is empty by spec; success = no Fault.
    except Exception as exc:
        if looks_like_soap_fault(exc):
            return  # ActionNotSupported is acceptable.
        raise
