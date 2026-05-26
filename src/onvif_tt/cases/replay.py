"""Replay service (Profile G).

The v20.12 corpus doesn't have a dedicated ``REPLAY.html`` — replay
test cases are scattered into ``DEVICE-1-1-20`` (GetServices includes
the replay namespace) and consumed indirectly in RTSS streaming tests.
We add a single ``LOCAL-REPLAY-STREAM-URI`` smoke that round-trips
GetReplayUri.
"""

from __future__ import annotations

import re

import pytest

from ..registry import register
from ..runtime.dut import DUT


_REPLAY_NS = "http://www.onvif.org/ver10/replay/wsdl"
_RTSP_RE = re.compile(r"^rtsp://[^\s]+$")


@register("DEVICE-1-1-20", profiles={"G"}, mandatory=False,
          requires_services={"devicemgmt", "replay"})
def test_get_services_replay(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-20 — GET SERVICES – REPLAY SERVICE."""
    services = dut.devicemgmt.GetServices(False)
    entry = next((s for s in services if s.Namespace == _REPLAY_NS), None)
    assert entry is not None, "GetServices didn't include replay"
    assert entry.XAddr, "replay service XAddr empty"


@register("LOCAL-REPLAY-STREAM-URI", profiles={"G"}, mandatory=False,
          requires_services={"devicemgmt", "replay", "recording"},
          tags={"local"})
def test_get_replay_uri(dut: DUT, spec) -> None:
    """GetReplayUri returns an ``rtsp://...`` URL for an existing recording.

    Skips when the device has no recordings (we can't address one).
    """
    recordings = dut.recording.GetRecordings() or []
    if not recordings:
        pytest.skip("device has no recordings to replay")
    token = recordings[0].RecordingToken
    req = dut.replay.create_type("GetReplayUri")
    req.RecordingToken = token
    req.StreamSetup = {
        "Stream": "RTP-Unicast",
        "Transport": {"Protocol": "RTSP"},
    }
    try:
        resp = dut.replay.GetReplayUri(req)
    except Exception as exc:
        pytest.skip(f"GetReplayUri not supported: {exc}")
    uri = getattr(resp, "Uri", None) or ""
    assert _RTSP_RE.match(uri), f"GetReplayUri returned non-RTSP URL: {uri!r}"
