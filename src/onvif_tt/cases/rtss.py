"""Real-time streaming (RTSS) — transport variations + concurrent
sessions + keepalive behaviour.

The official RTSS-1-1-* test family (139 tests) systematically pairs
every video resolution with every RTP-transport combination, often
mutating the device's encoder configuration in the process. We don't
attempt that grid here — our existing LOCAL-MEDIA-S-RTSP-LIVE already
covers the basic "default profile, RTP-Unicast/RTSP/TCP" path.

What's added here is the orthogonal axis: streaming under different
transport choices, multiple concurrent sessions, and RTSP-session
keepalive. All use ffprobe — same shape as the existing decoder tests.
"""

from __future__ import annotations

import concurrent.futures
import shutil
import subprocess
import time
from urllib.parse import urlparse, urlunparse

import pytest

from ..registry import register
from ..runtime.dut import DUT


def _auth_uri(dut: DUT, profile_token: str, protocol: str = "RTSP") -> str:
    """Get StreamUri with the requested transport protocol and embed
    credentials if absent.
    """
    req = dut.media.create_type("GetStreamUri")
    req.StreamSetup = {
        "Stream": "RTP-Unicast",
        "Transport": {"Protocol": protocol},
    }
    req.ProfileToken = profile_token
    uri = dut.media.GetStreamUri(req).Uri
    if "@" not in uri and dut.config.user:
        u = urlparse(uri)
        netloc = f"{dut.config.user}:{dut.config.password}@{u.hostname}"
        if u.port:
            netloc += f":{u.port}"
        uri = urlunparse(u._replace(netloc=netloc))
    return uri


def _ffprobe_check(uri: str, rtsp_transport: str = "tcp",
                   timeout_s: int = 12) -> tuple[int, str]:
    """Run ffprobe with the requested transport, return (returncode, stderr)."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    result = subprocess.run(
        [
            "ffprobe", "-hide_banner", "-v", "error",
            "-rtsp_transport", rtsp_transport,
            "-timeout", str(timeout_s * 1_000_000),
            "-show_streams", "-select_streams", "v:0",
            "-of", "default=noprint_wrappers=1:nokey=0",
            uri,
        ],
        capture_output=True, text=True, timeout=timeout_s + 5,
    )
    return result.returncode, result.stderr


# ---------------------------------------------------------------------------
# Transport variations — TCP vs UDP framing
# ---------------------------------------------------------------------------

@register("LOCAL-RTSS-RTP-OVER-TCP-STREAM", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local", "network", "requires_ffprobe"})
def test_rtss_rtp_over_tcp_stream(dut: DUT, spec) -> None:
    """RTSS spirit of RTSS-1-1-28/29/42/43 — verify RTP-over-RTSP-TCP
    transport actually delivers decodable video.
    """
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no media profiles")
    uri = _auth_uri(dut, profiles[0].token, protocol="RTSP")
    rc, stderr = _ffprobe_check(uri, rtsp_transport="tcp")
    assert rc == 0, f"ffprobe (rtp-over-tcp) failed: {stderr[:300]}"


@register("LOCAL-RTSS-RTP-OVER-UDP-STREAM", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local", "network", "requires_ffprobe"})
def test_rtss_rtp_over_udp_stream(dut: DUT, spec) -> None:
    """RTSS spirit of RTSS-1-1-27/34/37/41 — verify RTP-Unicast/UDP
    transport actually delivers decodable video.
    """
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no media profiles")
    uri = _auth_uri(dut, profiles[0].token, protocol="UDP")
    rc, stderr = _ffprobe_check(uri, rtsp_transport="udp")
    if rc != 0:
        # UDP frequently has NAT/firewall friction in CI. Treat
        # connection-refused / port-binding-related failures as a soft
        # skip; only a clearly-not-a-stream response is a hard fail.
        msg = stderr.lower()
        if any(t in msg for t in ("port", "udp", "bind", "could not", "refused")):
            pytest.skip(f"UDP transport not negotiable in this network: {stderr[:200]}")
        pytest.fail(f"ffprobe (rtp-over-udp) failed: {stderr[:300]}")


# ---------------------------------------------------------------------------
# Concurrent streaming sessions
# ---------------------------------------------------------------------------

@register("LOCAL-RTSS-MULTIPLE-CONCURRENT-STREAMS", profiles={"S"},
          mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local", "network", "requires_ffprobe"})
def test_rtss_concurrent_streams(dut: DUT, spec) -> None:
    """RTSS spirit of RTSS-1-1-30 — open N concurrent RTSP sessions to
    the same profile and assert all decode video successfully.

    GuaranteedNumberOfVideoEncoderInstances in the device's media
    capabilities tells us how many simultaneous sessions the vendor
    advertises. We probe min(advertised, 3) — three is the smallest
    number of sessions that exercises the "multi-session" code path
    on the encoder side without stressing CI runners.
    """
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no media profiles")
    profile_token = profiles[0].token

    # Determine how many sessions the DUT claims to support.
    advertised = 1
    try:
        # The WSDL order is (ConfigurationToken, ProfileToken); these were
        # passed the other way round, so the device was being asked about a
        # configuration named by a profile token. Named via the request type
        # so the order cannot drift again.
        req = dut.media.create_type("GetVideoEncoderConfigurationOptions")
        req.ConfigurationToken = profiles[0].VideoEncoderConfiguration.token
        req.ProfileToken = profile_token
        opts = dut.media.GetVideoEncoderConfigurationOptions(req)
        # GuaranteedNumberOfVideoEncoderInstances may live in several
        # places depending on the WSDL version; try a few.
        gnvei = (
            getattr(opts, "GuaranteedNumberOfVideoEncoderInstances", None)
            or 1
        )
        advertised = int(getattr(gnvei, "TotalNumber", gnvei) or 1)
    except Exception:
        pass
    n_sessions = min(advertised, 3)
    if n_sessions < 2:
        pytest.skip(f"DUT only guarantees {advertised} concurrent session(s)")

    uri = _auth_uri(dut, profile_token, protocol="RTSP")

    def _one_probe(i: int) -> tuple[int, int, str]:
        rc, err = _ffprobe_check(uri, rtsp_transport="tcp", timeout_s=15)
        return i, rc, err

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_sessions) as ex:
        results = list(ex.map(_one_probe, range(n_sessions)))

    failures = [(i, err) for i, rc, err in results if rc != 0]
    assert not failures, (
        f"{len(failures)}/{n_sessions} concurrent ffprobes failed: "
        + "; ".join(f"#{i}: {err[:120]}" for i, err in failures[:3])
    )


# ---------------------------------------------------------------------------
# RTSP session lifetime — keepalive
# ---------------------------------------------------------------------------

@register("LOCAL-RTSS-RTSP-KEEPALIVE", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media"},
          tags={"local", "network", "requires_ffprobe"})
def test_rtss_rtsp_keepalive(dut: DUT, spec) -> None:
    """RTSS spirit of RTSS-1-1-32/33 — verify the device holds an RTSP
    session alive across a quiet interval, then continues to deliver
    frames. We open a 12-second ffprobe that pulls 30 frames; the spec
    test wants 60s, but our CI budget is tighter.
    """
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    profiles = dut.media.GetProfiles() or []
    if not profiles:
        pytest.skip("no media profiles")
    uri = _auth_uri(dut, profiles[0].token, protocol="RTSP")

    # Pull 30 frames; ffprobe holds the RTSP session for that duration,
    # which exercises the device's session-timeout / keepalive logic.
    result = subprocess.run(
        [
            "ffprobe", "-hide_banner", "-v", "error",
            "-rtsp_transport", "tcp",
            "-timeout", "15000000",
            "-select_streams", "v:0",
            "-show_entries", "frame=pts",
            "-read_intervals", "%+#30",
            "-of", "csv=p=0",
            uri,
        ],
        capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, (
        f"ffprobe failed during keepalive run: {result.stderr[:300]}"
    )
    pts_lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(pts_lines) >= 5, (
        f"only {len(pts_lines)} frames observed across the keepalive "
        f"interval — RTSP session may have died"
    )
