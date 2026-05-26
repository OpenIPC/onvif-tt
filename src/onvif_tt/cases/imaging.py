"""Imaging service smoke tests from IMAGING.html.

Read-only — uses the first VideoSource token reported by Media service.
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime.dut import DUT


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
    import zeep.exceptions
    try:
        dut.imaging.GetMoveOptions("__definitely_not_a_real_token__")
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on invalid VideoSourceToken")


@register("IMAGING-2-1-17", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "imaging"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock firmware swallows invalid tokens.",
          }])
def test_imaging_get_status_invalid_token(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-2-1-17 — GETSTATUS – INVALID VIDEOSOURCETOKEN."""
    import zeep.exceptions
    try:
        dut.imaging.GetStatus("__definitely_not_a_real_token__")
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on invalid VideoSourceToken")
