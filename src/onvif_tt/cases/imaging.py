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
