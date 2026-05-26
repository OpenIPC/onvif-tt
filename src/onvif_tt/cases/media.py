"""Profile S Media (v10) smoke tests.

The published ONVIF v20.12 test specs collapsed media v10 + media2 into
``MEDIA2.html``; the only test cases that explicitly stress media v10
live in ``RTSS.html`` and target full RTP/RTSP integration. The simple
"does GetStreamUri / GetSnapshotUri work" smoke tests have no clean
catalog counterpart, so we mint ``LOCAL-MEDIA-S-*`` IDs for those —
the ``LOCAL-`` prefix signals they're tool-author additions, not from
the official ONVIF Test Specification corpus.

Where a corpus ID maps cleanly to the universal intent (e.g.
``MEDIA2-1-1-1`` "ready-to-use profile for video streaming"), we keep
that ID and implement against whichever media service the DUT
advertises (media v10 for Profile S, media2 for Profile T).
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from ..registry import register
from ..runtime.dut import DUT


_RTSP_RE = re.compile(r"^rtsp://[^\s]+$")


def _media_service(dut: DUT, services):
    """Return whichever of ``dut.media2`` / ``dut.media`` is advertised.

    Returns ``(svc, profile_letter)`` where profile_letter is "T" if
    media2 is used and "S" otherwise. Caller is expected to have already
    decided whether to skip if both are absent.
    """
    if "media2" in services:
        return dut.media2, "T"
    if "media" in services:
        return dut.media, "S"
    return None, ""


def _first_profile_token(dut: DUT, services) -> str:
    svc, _ = _media_service(dut, services)
    assert svc is not None, "no media service available — should have been skipped"
    profiles = svc.GetProfiles()
    if not profiles:
        pytest.fail("GetProfiles returned no profiles")
    return profiles[0].token


# ---------------------------------------------------------------------------
# READY-TO-USE PROFILE — implemented per MEDIA2-1-1-1 intent, but adaptive:
# falls back to media v10 when media2 isn't advertised.
# ---------------------------------------------------------------------------

@register("MEDIA2-1-1-1", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"},
          tags={"adaptive_media"})
def test_ready_to_use_profile_for_video(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-1-1-1 — READY TO USE MEDIA PROFILE FOR VIDEO.

    Asserts the DUT exposes at least one profile per video source whose
    VideoEncoder configuration encodes to H.264 or H.265. Spec is written
    for media2 (Profile T); we honour the intent on Profile S devices by
    falling back to the media v10 service and checking ``VideoEncoderConfiguration``.
    """
    from ..runtime.features import discover_services
    services = discover_services(dut)
    svc, prof = _media_service(dut, services)
    if svc is None:
        pytest.skip("Neither media2 nor media v10 service is advertised")
    sources = svc.GetVideoSources()
    assert sources, f"GetVideoSources (media {prof}) returned nothing"
    profiles = svc.GetProfiles()
    assert profiles, f"GetProfiles (media {prof}) returned nothing"

    for src in sources:
        src_token = src.token
        # Each video source must have at least one profile bound to it
        # with a usable VideoEncoder configuration.
        matched = []
        for p in profiles:
            cfgs = getattr(p, "Configurations", None)
            vsc = (
                cfgs.VideoSource if (cfgs is not None and cfgs.VideoSource) else None
            ) if hasattr(p, "Configurations") else getattr(p, "VideoSourceConfiguration", None)
            vec = (
                cfgs.VideoEncoder if (cfgs is not None and cfgs.VideoEncoder) else None
            ) if hasattr(p, "Configurations") else getattr(p, "VideoEncoderConfiguration", None)
            if vsc is None or vec is None:
                continue
            if getattr(vsc, "SourceToken", None) != src_token:
                continue
            encoding = getattr(vec, "Encoding", None)
            if encoding in ("H264", "H265"):
                matched.append(p)
        assert matched, (
            f"video source {src_token!r} has no profile with H.264/H.265 encoder"
        )


# ---------------------------------------------------------------------------
# LOCAL-MEDIA-S-* — Media v10 unit-level smoke tests with no catalog ID.
# ---------------------------------------------------------------------------

@register("LOCAL-MEDIA-S-VIDEO-SOURCES", profiles={"S"}, mandatory=True,
          requires_services={"devicemgmt", "media"},
          tags={"local"})
def test_media_get_video_sources(dut: DUT, spec) -> None:
    """Media v10 GetVideoSources returns at least one source with a token."""
    sources = dut.media.GetVideoSources()
    assert sources, "Media.GetVideoSources returned empty"
    for s in sources:
        assert s.token, "video source missing token"


@register("LOCAL-MEDIA-S-STREAM-URI", profiles={"S"}, mandatory=True,
          requires_services={"devicemgmt", "media"},
          tags={"local"})
def test_media_get_stream_uri(dut: DUT, spec) -> None:
    """Media v10 GetStreamUri returns a parseable ``rtsp://…`` URL.

    Uses the first profile + RTP-Unicast/RTSP transport — the lowest
    common denominator Profile S devices have to support.
    """
    profiles = dut.media.GetProfiles()
    if not profiles:
        pytest.skip("no media profiles to query StreamUri for")
    profile = profiles[0]

    stream_setup = dut.media.create_type("GetStreamUri")
    stream_setup.StreamSetup = {
        "Stream": "RTP-Unicast",
        "Transport": {"Protocol": "RTSP"},
    }
    stream_setup.ProfileToken = profile.token
    resp = dut.media.GetStreamUri(stream_setup)
    uri = getattr(resp, "Uri", None) or ""
    assert _RTSP_RE.match(uri), f"GetStreamUri returned non-RTSP URL: {uri!r}"


@register("LOCAL-MEDIA-S-RTSP-LIVE", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local", "network", "requires_ffprobe"})
def test_media_rtsp_decodes(dut: DUT, spec) -> None:
    """End-to-end signal: the URL from GetStreamUri actually streams.

    Uses ffprobe over RTSP/TCP to verify at least one video stream is
    decoded from the live URL. Same shape of check that
    ``frigate-check.sh`` makes against Frigate's snapshot endpoint.
    """
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    profiles = dut.media.GetProfiles()
    if not profiles:
        pytest.skip("no media profiles")
    profile = profiles[0]

    req = dut.media.create_type("GetStreamUri")
    req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
    req.ProfileToken = profile.token
    uri = dut.media.GetStreamUri(req).Uri

    # If the URL doesn't already embed creds, inject the DUT's user:pass.
    if "@" not in uri and dut.config.user:
        from urllib.parse import urlparse, urlunparse
        u = urlparse(uri)
        netloc = f"{dut.config.user}:{dut.config.password}@{u.hostname}"
        if u.port:
            netloc += f":{u.port}"
        uri = urlunparse(u._replace(netloc=netloc))

    result = subprocess.run(
        [
            "ffprobe", "-hide_banner", "-v", "error",
            "-rtsp_transport", "tcp",
            "-timeout", "8000000",  # μs
            "-show_streams", "-of", "default=noprint_wrappers=1:nokey=0",
            uri,
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f"ffprobe failed: rc={result.returncode}\n"
        f"stderr: {result.stderr[:500]}"
    )
    assert "codec_type=video" in result.stdout, (
        f"no video stream in ffprobe output:\n{result.stdout[:500]}"
    )


@register("LOCAL-MEDIA-S-SNAPSHOT-URI", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local"})
def test_media_get_snapshot_uri(dut: DUT, spec) -> None:
    """Media v10 GetSnapshotUri returns an ``http(s)://…`` URL.

    Optional in the spec: many devices return an empty URI when snapshot
    isn't supported. We treat empty as a clean skip; non-empty must be
    a valid HTTP URL.
    """
    profiles = dut.media.GetProfiles()
    if not profiles:
        pytest.skip("no media profiles")
    profile = profiles[0]

    resp = dut.media.GetSnapshotUri(profile.token)
    uri = getattr(resp, "Uri", "") or ""
    if not uri:
        pytest.skip("DUT does not provide a snapshot URI for this profile")
    assert uri.startswith(("http://", "https://")), (
        f"GetSnapshotUri returned non-HTTP URI: {uri!r}"
    )
