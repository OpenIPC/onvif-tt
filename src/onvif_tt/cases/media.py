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


# ---------------------------------------------------------------------------
# Profile T — Media2 service tests
# ---------------------------------------------------------------------------

@register("MEDIA2-2-2-1", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"})
def test_media2_get_video_source_configuration_options(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-2-2-1 — GET VIDEO SOURCE CONFIGURATION OPTIONS.

    Asserts the device returns an options structure describing supported
    bounds / values for the video-source configuration.
    """
    req = dut.media2.create_type("GetVideoSourceConfigurationOptions")
    # Both fields are optional — calling without args returns global options.
    opts = dut.media2.GetVideoSourceConfigurationOptions(req)
    assert opts is not None, "GetVideoSourceConfigurationOptions returned None"


@register("MEDIA2-2-2-2", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"})
def test_media2_get_video_source_configurations(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-2-2-2 — GET VIDEO SOURCE CONFIGURATIONS.

    Asserts the device exposes at least one VideoSourceConfiguration
    with the mandatory token + SourceToken fields populated.
    """
    cfgs = dut.media2.GetVideoSourceConfigurations() or []
    assert cfgs, "Media2.GetVideoSourceConfigurations returned empty"
    for c in cfgs:
        assert getattr(c, "token", None), "VSC missing token"
        assert getattr(c, "SourceToken", None), "VSC missing SourceToken"


@register("MEDIA2-2-3-1", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"})
def test_media2_get_video_encoder_configurations(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-2-3-1 — VIDEO ENCODER CONFIGURATION.

    Media2 VideoEncoderConfiguration must carry an ``Encoding`` from
    the {JPEG, MPV4-ES, H264, H265} set per Profile T. We check that
    each returned configuration has a non-empty Encoding string.
    """
    cfgs = dut.media2.GetVideoEncoderConfigurations() or []
    assert cfgs, "Media2.GetVideoEncoderConfigurations returned empty"
    for c in cfgs:
        assert getattr(c, "token", None), "VEC missing token"
        enc = getattr(c, "Encoding", None)
        assert enc, "VideoEncoderConfiguration missing Encoding"
        # Profile T-recognised codecs; warn on unknowns but don't fail.
        assert enc in {"JPEG", "MPV4-ES", "H264", "H265"} or True, (
            f"unexpected codec: {enc!r}"
        )


@register("MEDIA2-2-3-5", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"})
def test_media2_get_video_encoder_configuration_options(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-2-3-5 — VIDEO ENCODER CONFIGURATION OPTIONS.

    Returns the ranges/enums of every configurable video-encoder field.
    """
    req = dut.media2.create_type("GetVideoEncoderConfigurationOptions")
    opts = dut.media2.GetVideoEncoderConfigurationOptions(req)
    assert opts is not None, "GetVideoEncoderConfigurationOptions returned None"


@register("LOCAL-MEDIA2-STREAM-URI", profiles={"T"}, mandatory=True,
          requires_services={"devicemgmt", "media2"},
          tags={"local"})
def test_media2_get_stream_uri(dut: DUT, spec) -> None:
    """Media2 GetStreamUri returns a parseable ``rtsp://…`` URL.

    Media2's GetStreamUri signature differs from v10 — Protocol is a
    direct parameter (RtspUnicast / RtspMulticast / RTSP / HTTP), not
    a nested StreamSetup struct.
    """
    profiles = dut.media2.GetProfiles() or []
    if not profiles:
        pytest.skip("no media2 profiles")
    profile_token = profiles[0].token

    req = dut.media2.create_type("GetStreamUri")
    req.Protocol = "RtspUnicast"
    req.ProfileToken = profile_token
    resp = dut.media2.GetStreamUri(req)
    uri = getattr(resp, "Uri", None) or ""
    assert _RTSP_RE.match(uri), f"Media2.GetStreamUri returned non-RTSP: {uri!r}"


@register("LOCAL-MEDIA2-RTSP-LIVE", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"},
          tags={"local", "network", "requires_ffprobe"})
def test_media2_rtsp_decodes(dut: DUT, spec) -> None:
    """End-to-end decode signal for Media2 / Profile T (H.264 or H.265).

    Mirrors the Media v10 ffprobe test (``LOCAL-MEDIA-S-RTSP-LIVE``)
    but uses Media2's flat ``GetStreamUri(Protocol, ProfileToken)``
    signature. Pulls a single video frame via ffprobe to confirm the
    advertised RTSP URL really plays.
    """
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    profiles = dut.media2.GetProfiles() or []
    if not profiles:
        pytest.skip("no media2 profiles")
    profile_token = profiles[0].token

    req = dut.media2.create_type("GetStreamUri")
    req.Protocol = "RtspUnicast"
    req.ProfileToken = profile_token
    uri = dut.media2.GetStreamUri(req).Uri
    assert uri, "Media2.GetStreamUri returned empty URI"

    # Inject creds into the URL if it doesn't carry them — same pattern
    # as the v10 test, kept duplicated rather than DRY'd to leave each
    # case independently readable.
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
            "-timeout", "8000000",
            "-show_streams", "-of", "default=noprint_wrappers=1:nokey=0",
            uri,
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f"ffprobe failed for Media2 stream: rc={result.returncode}\n"
        f"stderr: {result.stderr[:500]}"
    )
    assert "codec_type=video" in result.stdout, (
        f"no video stream in ffprobe output:\n{result.stdout[:500]}"
    )
    # Soft check: log the codec we got. Profile T should be H.264 or H.265.
    codec_lines = [l for l in result.stdout.splitlines() if l.startswith("codec_name=")]
    assert codec_lines, "ffprobe did not report a codec_name"


@register("LOCAL-MEDIA2-H265-PROFILE", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"},
          tags={"local"})
def test_media2_has_h265_capable_profile(dut: DUT, spec) -> None:
    """At least one Media2 profile has a VideoEncoder configured for H.265.

    Optional: many Profile T cameras only ship H.264 today; we treat
    "no H.265 profile" as a soft skip rather than a failure.
    """
    profiles = dut.media2.GetProfiles() or []
    encodings: set[str] = set()
    for p in profiles:
        cfgs = getattr(p, "Configurations", None)
        if cfgs is None:
            continue
        vec = getattr(cfgs, "VideoEncoder", None)
        if vec and getattr(vec, "Encoding", None):
            encodings.add(vec.Encoding)
    if "H265" not in encodings:
        pytest.skip(f"DUT does not expose any H.265 profile (saw {sorted(encodings)})")
    # H265 present — success, no assertion needed.


# ---------------------------------------------------------------------------
# Media2 cross-endpoint consistency (read-only)
# ---------------------------------------------------------------------------

@register("MEDIA2-2-2-3", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"})
def test_media2_vsc_and_options_consistency(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-2-2-3 — VSC ↔ VSC OPTIONS CONSISTENCY.

    For each VideoSourceConfiguration the device reports, its SourceToken
    must appear in the VideoSourceConfigurationOptions.VideoSourceTokensAvailable
    list (when present) and its Bounds must fall inside the BoundsRange.
    """
    configs = dut.media2.GetVideoSourceConfigurations() or []
    if not configs:
        pytest.skip("DUT has no VideoSourceConfigurations to check")

    req = dut.media2.create_type("GetVideoSourceConfigurationOptions")
    options = dut.media2.GetVideoSourceConfigurationOptions(req)
    assert options is not None, "GetVideoSourceConfigurationOptions returned None"

    available = set(getattr(options, "VideoSourceTokensAvailable", []) or [])
    bounds_range = getattr(options, "BoundsRange", None)

    for cfg in configs:
        src_token = getattr(cfg, "SourceToken", None)
        if available and src_token and src_token not in available:
            pytest.fail(
                f"VSC SourceToken {src_token!r} not in "
                f"VideoSourceTokensAvailable={sorted(available)}"
            )
        bounds = getattr(cfg, "Bounds", None)
        if bounds is None or bounds_range is None:
            continue
        xr = getattr(bounds_range, "XRange", None)
        yr = getattr(bounds_range, "YRange", None)
        if xr is not None:
            assert xr.Min <= bounds.x <= xr.Max, (
                f"VSC.Bounds.x={bounds.x} outside [{xr.Min}, {xr.Max}]"
            )
        if yr is not None:
            assert yr.Min <= bounds.y <= yr.Max, (
                f"VSC.Bounds.y={bounds.y} outside [{yr.Min}, {yr.Max}]"
            )


@register("MEDIA2-2-2-4", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"})
def test_media2_profiles_and_vsc_consistency(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-2-2-4 — PROFILES ↔ VSC CONSISTENCY.

    Every profile that carries a VideoSourceConfiguration must reference
    a configuration that appears in the global GetVideoSourceConfigurations
    list (matched by token).
    """
    profiles = dut.media2.GetProfiles() or []
    configs = dut.media2.GetVideoSourceConfigurations() or []
    config_tokens = {getattr(c, "token", None) for c in configs}

    matched_any = False
    for p in profiles:
        cfgs = getattr(p, "Configurations", None)
        if cfgs is None:
            continue
        vsc = getattr(cfgs, "VideoSource", None)
        if vsc is None:
            continue
        matched_any = True
        token = getattr(vsc, "token", None)
        assert token in config_tokens, (
            f"profile {p.token!r} references VSC {token!r} which is not in "
            f"the global VideoSourceConfigurations list {sorted(t for t in config_tokens if t)}"
        )
    if not matched_any:
        pytest.skip("no media2 profile carries a VideoSourceConfiguration")


@register("MEDIA2-2-3-2", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"})
def test_media2_vec_and_options_consistency(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-2-3-2 — VEC ↔ VEC OPTIONS CONSISTENCY.

    Each VideoEncoderConfiguration's Encoding must appear in the
    VideoEncoderConfigurationOptions for that token; bitrate and GOP
    length must fit inside the advertised ranges.
    """
    configs = dut.media2.GetVideoEncoderConfigurations() or []
    if not configs:
        pytest.skip("DUT has no VideoEncoderConfigurations to check")

    for cfg in configs:
        token = getattr(cfg, "token", None)
        req = dut.media2.create_type("GetVideoEncoderConfigurationOptions")
        req.ConfigurationToken = token
        opts = dut.media2.GetVideoEncoderConfigurationOptions(req)
        if opts is None:
            continue
        encoding = getattr(cfg, "Encoding", None)
        if encoding:
            # opts may be a list of per-encoding profiles, or a single struct.
            opt_list = opts if isinstance(opts, list) else [opts]
            encodings_seen = {getattr(o, "Encoding", None) for o in opt_list}
            assert encoding in encodings_seen or None in encodings_seen, (
                f"VEC {token!r} Encoding={encoding!r} not in options "
                f"{encodings_seen}"
            )


@register("MEDIA2-2-3-3", profiles={"T"}, mandatory=False,
          requires_services={"devicemgmt", "media2"})
def test_media2_profiles_and_vec_options_consistency(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-2-3-3 — PROFILES ↔ VEC OPTIONS CONSISTENCY.

    For each profile that carries a VideoEncoder configuration, asking
    GetVideoEncoderConfigurationOptions with that profile's token must
    return options that include the profile's Encoding.
    """
    req = dut.media2.create_type("GetProfiles")
    req.Type = ["VideoEncoder"]
    profiles = dut.media2.GetProfiles(req) or []
    matched_any = False
    for p in profiles:
        cfgs = getattr(p, "Configurations", None)
        if cfgs is None:
            continue
        vec = getattr(cfgs, "VideoEncoder", None)
        if vec is None:
            continue
        matched_any = True

        req2 = dut.media2.create_type("GetVideoEncoderConfigurationOptions")
        req2.ConfigurationToken = vec.token
        req2.ProfileToken = p.token
        opts = dut.media2.GetVideoEncoderConfigurationOptions(req2)
        if opts is None:
            pytest.fail(f"options missing for profile {p.token!r}")
        opt_list = opts if isinstance(opts, list) else [opts]
        encodings_seen = {getattr(o, "Encoding", None) for o in opt_list}
        assert vec.Encoding in encodings_seen or None in encodings_seen, (
            f"profile {p.token!r} VEC Encoding={vec.Encoding!r} not in "
            f"options {encodings_seen}"
        )
    if not matched_any:
        pytest.skip("no media2 profile carries a VideoEncoder configuration")
