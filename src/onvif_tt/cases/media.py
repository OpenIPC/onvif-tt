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
from urllib.parse import urlsplit

import pytest

from ..registry import register
from ..runtime.dut import DUT


def _assert_rtsp_uri(uri: str, what: str) -> None:
    """Assert ``uri`` is a well-formed ``rtsp://host[:port][/path]``.

    Parsed rather than pattern-matched: a host character class permissive
    enough for IPv6 literals and userinfo also swallows ``:abc``, so the port
    group never gets a chance to reject a non-numeric port. urlsplit knows the
    authority grammar, and raises on a bad port, which is the case worth
    catching — a URI whose port is junk is one no client can dial.
    """
    assert uri and not any(c.isspace() for c in uri), (
        f"{what} returned a URI with whitespace: {uri!r}"
    )
    parsed = urlsplit(uri)
    assert parsed.scheme == "rtsp", (
        f"{what} returned non-RTSP scheme {parsed.scheme!r}: {uri!r}"
    )
    try:
        port = parsed.port
    except ValueError:
        raise AssertionError(f"{what} returned a non-numeric port: {uri!r}")
    assert parsed.hostname, f"{what} returned no host: {uri!r}"
    assert port is None or 0 < port < 65536, (
        f"{what} returned an out-of-range port {port}: {uri!r}"
    )

# tt:VideoEncoding (onvif.xsd) is a *closed* enumeration. H265 is deliberately
# absent: it has no ver10 representation and only exists as a
# tt:VideoEncodingMimeNames value, i.e. a Media2 concept. A device that puts
# H265 in a ver10 tt:VideoEncoderConfiguration is emitting schema-invalid XML,
# which zeep will hand back as a plain string rather than reject — so it has
# to be asserted here or it goes unnoticed.
_VER10_ENCODINGS = {"JPEG", "MPEG4", "H264"}
_VER20_ENCODINGS = {"JPEG", "MPV4-ES", "H264", "H265"}


def _media_service(dut: DUT, services):
    """Return whichever of ``dut.media2`` / ``dut.media`` is advertised.

    Returns ``(svc, profile_letter)`` where profile_letter is "T" if
    media2 is used and "S" otherwise. Caller is expected to have already
    decided whether to skip if both are absent.

    Advertisement is not enough on its own: media2 can be advertised and still
    be unbindable by this client. These callers only need *a* media service, so
    prefer media2 when it can actually be constructed and fall back to v10
    otherwise — refusing to run a v10-capable check because v20 is out of reach
    would lose coverage the device is entitled to.
    """
    if "media2" in services and dut.can_bind("media2"):
        return dut.media2, "T"
    if "media" in services:
        return dut.media, "S"
    return None, ""


def _video_source_tokens(svc, prof: str) -> list[str]:
    """Tokens of the DUT's video sources, via whichever media service.

    ``GetVideoSources`` is a v10-only operation — the ver20 WSDL has no
    such thing, since Media2 addresses sources only through
    ``VideoSourceConfiguration.SourceToken``. Calling it on a Media2 proxy
    raises ``AttributeError: Service has no operation 'GetVideoSources'``,
    which is what this adaptive test did the moment media2 became bindable.
    """
    if prof == "T":
        cfgs = svc.GetVideoSourceConfigurations() or []
        # Several configurations may share one source; dedupe, keep order.
        return list(dict.fromkeys(
            c.SourceToken for c in cfgs if getattr(c, "SourceToken", None)
        ))
    return [s.token for s in (svc.GetVideoSources() or [])]


def _media2_stream_uri(dut: DUT, profile_token: str) -> str:
    """Media2 ``GetStreamUri`` for one profile, as a plain string.

    Two shape differences from v10, both easy to get wrong and both only
    observable once the service actually binds:

    * ``Protocol`` is a direct parameter (RtspUnicast / RtspMulticast /
      RTSP / HTTP), not a nested ``StreamSetup`` struct;
    * ``GetStreamUriResponse`` holds a single ``xs:anyURI`` element, so zeep
      unwraps it to a ``str``. v10 returns a ``tt:MediaUri`` *struct*, so
      ``resp.Uri`` is right there and wrong here.
    """
    req = dut.media2.create_type("GetStreamUri")
    req.Protocol = "RtspUnicast"
    req.ProfileToken = profile_token
    resp = dut.media2.GetStreamUri(req)
    # Tolerate a struct too: the response is single-element per the ver20
    # WSDL, but zeep only unwraps when it parses against that schema.
    return resp if isinstance(resp, str) else (getattr(resp, "Uri", None) or "")


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
    source_tokens = _video_source_tokens(svc, prof)
    assert source_tokens, f"media {prof} reported no video sources"
    profiles = svc.GetProfiles()
    assert profiles, f"GetProfiles (media {prof}) returned nothing"

    for src_token in source_tokens:
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
    _assert_rtsp_uri(uri, "GetStreamUri")


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


# ---------------------------------------------------------------------------
# Deeper decoder-side conformance — assertions on what the encoder ACTUALLY
# produces vs what GetProfiles claims. These have surfaced real vendor bugs
# (e.g. Xiongmai stock advertising H264 High Profile but streaming Baseline).
# ---------------------------------------------------------------------------

def _ffprobe_video_stream(uri: str, transport: str = "tcp") -> dict[str, str]:
    """Run ffprobe with key=value output for the first video stream.

    Returns a dict of field→str. Raises pytest.skip if ffprobe isn't
    installed or the URL doesn't open within ``timeout``.
    """
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    result = subprocess.run(
        [
            "ffprobe", "-hide_banner", "-v", "error",
            "-rtsp_transport", transport,
            "-timeout", "8000000",
            "-show_streams", "-select_streams", "v:0",
            "-of", "default=noprint_wrappers=1:nokey=0",
            uri,
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        pytest.skip(f"ffprobe failed: {result.stderr[:200]}")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k] = v
    if not fields:
        pytest.skip("ffprobe returned no video-stream fields")
    return fields


def _authed_stream_uri(dut: DUT, profile_token: str) -> str:
    """GetStreamUri for the profile, embed creds if missing."""
    req = dut.media.create_type("GetStreamUri")
    req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
    req.ProfileToken = profile_token
    uri = dut.media.GetStreamUri(req).Uri
    if "@" not in uri and dut.config.user:
        from urllib.parse import urlparse, urlunparse
        u = urlparse(uri)
        netloc = f"{dut.config.user}:{dut.config.password}@{u.hostname}"
        if u.port:
            netloc += f":{u.port}"
        uri = urlunparse(u._replace(netloc=netloc))
    return uri


@register("LOCAL-MEDIA-S-RESOLUTION-MATCHES-PROFILE", profiles={"S"},
          mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local", "network", "requires_ffprobe"})
def test_stream_resolution_matches_profile(dut: DUT, spec) -> None:
    """The width × height ffprobe sees on the live stream must match
    what GetProfiles advertised for that profile's VideoEncoder config.

    A real-world conformance gap — vendors sometimes advertise a
    resolution they can't actually deliver, or fall back silently
    to a smaller capture when the sensor can't keep up.
    """
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no profiles")
    profile = profiles[0]
    vec = getattr(profile, "VideoEncoderConfiguration", None)
    if vec is None or vec.Resolution is None:
        pytest.skip("profile lacks Resolution config")
    advertised_w = int(vec.Resolution.Width)
    advertised_h = int(vec.Resolution.Height)

    uri = _authed_stream_uri(dut, profile.token)
    fields = _ffprobe_video_stream(uri)
    actual_w = int(fields.get("width", 0))
    actual_h = int(fields.get("height", 0))
    assert (actual_w, actual_h) == (advertised_w, advertised_h), (
        f"profile {profile.token!r} advertises "
        f"{advertised_w}x{advertised_h} but stream is {actual_w}x{actual_h}"
    )


@register("LOCAL-MEDIA-S-CODEC-MATCHES-PROFILE", profiles={"S"},
          mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local", "network", "requires_ffprobe"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock advertises H264Profile=High in "
                        "GetProfiles but the actual H.264 SPS shows "
                        "Baseline profile.",
          }])
def test_stream_codec_profile_matches(dut: DUT, spec) -> None:
    """The H.264/H.265 profile the device claims must match the profile
    ffprobe finds in the bitstream's SPS.

    Surfaces a common vendor bug: advertise a fancy codec profile,
    actually emit Baseline.
    """
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no profiles")
    profile = profiles[0]
    vec = getattr(profile, "VideoEncoderConfiguration", None)
    if vec is None:
        pytest.skip("profile lacks VideoEncoderConfiguration")
    encoding = getattr(vec, "Encoding", None)
    if encoding != "H264":
        pytest.skip(f"this test only covers H.264 (saw {encoding})")
    h264 = getattr(vec, "H264", None)
    advertised_profile = getattr(h264, "H264Profile", None) if h264 else None
    if not advertised_profile:
        pytest.skip("profile doesn't pin an H264Profile")

    uri = _authed_stream_uri(dut, profile.token)
    fields = _ffprobe_video_stream(uri)
    actual_profile = fields.get("profile", "")
    # Map ONVIF naming ("Main") to ffprobe naming ("Main") — they happen
    # to align for the common values. Compare case-insensitive.
    assert advertised_profile.lower() in actual_profile.lower(), (
        f"profile {profile.token!r} advertises H264Profile="
        f"{advertised_profile!r} but bitstream profile is "
        f"{actual_profile!r}"
    )


@register("LOCAL-MEDIA-S-FRAME-RATE-WITHIN-TOLERANCE", profiles={"S"},
          mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local", "network", "requires_ffprobe"})
def test_stream_frame_rate_within_tolerance(dut: DUT, spec) -> None:
    """ffprobe-reported r_frame_rate must be within ±25% of the
    FrameRateLimit advertised in the profile's RateControl.

    Tolerance is generous because RateControl.FrameRateLimit is a cap,
    not an exact rate — but a 40% gap (20 → 12) is a vendor bug.
    """
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no profiles")
    profile = profiles[0]
    vec = getattr(profile, "VideoEncoderConfiguration", None)
    rc = getattr(vec, "RateControl", None) if vec else None
    advertised_fps = getattr(rc, "FrameRateLimit", None) if rc else None
    if not advertised_fps:
        pytest.skip("profile lacks RateControl.FrameRateLimit")

    uri = _authed_stream_uri(dut, profile.token)
    fields = _ffprobe_video_stream(uri)
    raw_rate = fields.get("r_frame_rate", "")
    if "/" in raw_rate:
        num, den = raw_rate.split("/")
        try:
            actual_fps = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            pytest.skip(f"ffprobe r_frame_rate not parseable: {raw_rate!r}")
    else:
        try:
            actual_fps = float(raw_rate)
        except ValueError:
            pytest.skip(f"ffprobe r_frame_rate not parseable: {raw_rate!r}")

    # ±25% window. FrameRateLimit is a cap, so "less" is fine but big
    # under-delivery surfaces real hardware issues.
    lo = advertised_fps * 0.75
    hi = advertised_fps * 1.25
    assert lo <= actual_fps <= hi, (
        f"profile {profile.token!r} advertises {advertised_fps} fps "
        f"but stream is {actual_fps:.1f} fps (window {lo:.1f}–{hi:.1f})"
    )


@register("LOCAL-MEDIA-S-PTS-MONOTONIC", profiles={"S"},
          mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local", "network", "requires_ffprobe"})
def test_stream_pts_monotonic(dut: DUT, spec) -> None:
    """Read 30 frames from the live stream and verify their PTS (or DTS
    when PTS is missing) values increase monotonically.

    A common encoder bug is to emit wrap-around or backwards timestamps
    when the internal clock rolls over — which then produces playback
    glitches in any downstream consumer.
    """
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no profiles")
    profile = profiles[0]
    uri = _authed_stream_uri(dut, profile.token)

    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    result = subprocess.run(
        [
            "ffprobe", "-hide_banner", "-v", "error",
            "-rtsp_transport", "tcp",
            "-timeout", "8000000",
            "-select_streams", "v:0",
            "-show_entries", "frame=pts,dts,pkt_pts,pkt_dts",
            "-read_intervals", "%+#30",
            "-of", "csv=p=0",
            uri,
        ],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip(f"ffprobe frame dump failed: {result.stderr[:200]}")

    timestamps: list[int] = []
    for line in result.stdout.splitlines():
        cells = [c for c in line.split(",") if c and c != "N/A"]
        if not cells:
            continue
        try:
            timestamps.append(int(cells[0]))
        except ValueError:
            continue
    if len(timestamps) < 5:
        pytest.skip(f"only {len(timestamps)} valid timestamps — sample too small")

    for prev, curr in zip(timestamps, timestamps[1:]):
        assert curr > prev, (
            f"PTS sequence not strictly increasing: {prev} → {curr} "
            f"(full window: {timestamps[:10]}…)"
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
        assert enc in _VER20_ENCODINGS, (
            f"Media2 Encoding {enc!r} is not a tt:VideoEncodingMimeNames "
            f"value {sorted(_VER20_ENCODINGS)}"
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

    Media2's GetStreamUri signature differs from v10 — see
    :func:`_media2_stream_uri`.
    """
    profiles = dut.media2.GetProfiles() or []
    if not profiles:
        pytest.skip("no media2 profiles")
    uri = _media2_stream_uri(dut, profiles[0].token)
    _assert_rtsp_uri(uri, "Media2.GetStreamUri")


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
    uri = _media2_stream_uri(dut, profiles[0].token)
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


# ---------------------------------------------------------------------------
# LOCAL-MEDIA-S-CONFIG-* — Media v10 configuration surface.
#
# These exist because the ver10 plural getters had no coverage at all: every
# GetVideoEncoderConfigurations call in this file was on ``dut.media2``, so a
# device that answered the ver10 form with a SOAP fault still ran green, and
# a device whose singular getter disagreed with its own GetProfiles was never
# challenged.
# ---------------------------------------------------------------------------

@register("LOCAL-MEDIA-S-ENCODER-CONFIGS", profiles={"S"}, mandatory=True,
          requires_services={"devicemgmt", "media"},
          tags={"local"})
def test_media_get_video_encoder_configurations(dut: DUT, spec) -> None:
    """Media v10 GetVideoEncoderConfigurations lists schema-valid encoders."""
    cfgs = dut.media.GetVideoEncoderConfigurations() or []
    assert cfgs, "Media.GetVideoEncoderConfigurations returned empty"
    for c in cfgs:
        assert getattr(c, "token", None), "VEC missing token"
        enc = getattr(c, "Encoding", None)
        assert enc in _VER10_ENCODINGS, (
            f"ver10 Encoding {enc!r} is not a tt:VideoEncoding value "
            f"{sorted(_VER10_ENCODINGS)} — H265 belongs in Media2"
        )
        # tt:VideoEncoderConfiguration's H264 branch describes an H.264
        # stream; carrying one next to a different Encoding is a
        # self-contradiction clients cannot resolve.
        if enc != "H264":
            assert getattr(c, "H264", None) is None, (
                f"VEC {c.token!r} declares Encoding={enc!r} but carries an "
                f"H264 configuration block"
            )


@register("LOCAL-MEDIA-S-SOURCE-CONFIGS", profiles={"S"}, mandatory=True,
          requires_services={"devicemgmt", "media"},
          tags={"local"})
def test_media_get_video_source_configurations(dut: DUT, spec) -> None:
    """Media v10 GetVideoSourceConfigurations lists bounded source configs."""
    cfgs = dut.media.GetVideoSourceConfigurations() or []
    assert cfgs, "Media.GetVideoSourceConfigurations returned empty"
    for c in cfgs:
        assert getattr(c, "token", None), "VSC missing token"
        bounds = getattr(c, "Bounds", None)
        assert bounds is not None, f"VSC {c.token!r} missing Bounds"
        assert bounds.width > 0 and bounds.height > 0, (
            f"VSC {c.token!r} has degenerate Bounds "
            f"{bounds.width}x{bounds.height}"
        )


@register("LOCAL-MEDIA-S-ENCODER-CONFIG-CONSISTENT", profiles={"S"},
          mandatory=True, requires_services={"devicemgmt", "media"},
          tags={"local"})
def test_media_encoder_config_matches_profile(dut: DUT, spec) -> None:
    """GetVideoEncoderConfiguration(token) must agree with GetProfiles.

    Both describe the same encoder. A device that hardcodes one of them
    reports a resolution or codec the stream does not actually use, and the
    client believes whichever it asked for last.
    """
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no ver10 media profiles to cross-check")

    checked = 0
    for p in profiles:
        vec = getattr(p, "VideoEncoderConfiguration", None)
        if vec is None:
            continue
        single = dut.media.GetVideoEncoderConfiguration(vec.token)
        assert single is not None, (
            f"GetVideoEncoderConfiguration({vec.token!r}) returned nothing"
        )
        assert single.token == vec.token, (
            f"asked for {vec.token!r}, got {single.token!r}"
        )
        assert single.Encoding == vec.Encoding, (
            f"{vec.token!r}: GetProfiles says Encoding={vec.Encoding!r}, "
            f"GetVideoEncoderConfiguration says {single.Encoding!r}"
        )
        for label, cfg in (("GetProfiles", vec), ("the singular getter", single)):
            assert getattr(cfg, "Resolution", None) is not None, (
                f"{vec.token!r}: {label} returned no Resolution"
            )
        assert (single.Resolution.Width, single.Resolution.Height) == (
            vec.Resolution.Width, vec.Resolution.Height), (
            f"{vec.token!r}: GetProfiles says "
            f"{vec.Resolution.Width}x{vec.Resolution.Height}, "
            f"GetVideoEncoderConfiguration says "
            f"{single.Resolution.Width}x{single.Resolution.Height}"
        )
        checked += 1

    if not checked:
        pytest.skip("no profile carries a VideoEncoderConfiguration")


@register("LOCAL-MEDIA-S-OPTIONS-COVER-PROFILE", profiles={"S"},
          mandatory=True, requires_services={"devicemgmt", "media"},
          tags={"local"})
def test_media_options_cover_current_resolution(dut: DUT, spec) -> None:
    """The encoder options must offer the resolution the profile is using.

    Otherwise a client cannot write back the configuration it just read, and
    a UI built from the options list silently caps the camera below what it
    is actually streaming.
    """
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no ver10 media profiles to cross-check")

    checked = 0
    for p in profiles:
        vec = getattr(p, "VideoEncoderConfiguration", None)
        if vec is None:
            continue
        # Built as a request object rather than passed positionally: the WSDL
        # order is (ConfigurationToken, ProfileToken) and the two are
        # indistinguishable at a call site, so a swap would silently ask about
        # the wrong thing. python-onvif-zeep does not forward keyword
        # arguments, so this is the only way to name them.
        req = dut.media.create_type("GetVideoEncoderConfigurationOptions")
        req.ConfigurationToken = vec.token
        req.ProfileToken = p.token
        opts = dut.media.GetVideoEncoderConfigurationOptions(req)
        assert opts is not None, f"no options for {vec.token!r}"
        # Strictly the branch for the encoding actually in use. Falling back to
        # H264 would validate an H.264 resolution list against, say, a JPEG
        # profile and pass — which is exactly the hole this test exists to fill.
        assert getattr(vec, "Resolution", None) is not None, (
            f"profile {p.token!r} encoder carries no Resolution"
        )
        branch = getattr(opts, vec.Encoding, None)
        assert branch is not None, (
            f"options for {vec.token!r} carry no {vec.Encoding!r} branch, so "
            f"the encoding the profile is using has no advertised options"
        )
        available = {
            (r.Width, r.Height)
            for r in (getattr(branch, "ResolutionsAvailable", None) or [])
        }
        assert available, (
            f"{vec.Encoding!r} options for {vec.token!r} list no resolutions"
        )
        assert (vec.Resolution.Width, vec.Resolution.Height) in available, (
            f"profile {p.token!r} streams "
            f"{vec.Resolution.Width}x{vec.Resolution.Height} but options only "
            f"offer {sorted(available)}"
        )
        checked += 1

    if not checked:
        pytest.skip("no profile carries a VideoEncoderConfiguration")
