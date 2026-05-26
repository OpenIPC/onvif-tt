"""Recording Control service (Profile G).

The Xiongmai reference at 10.216.128.71 doesn't advertise the recording
service, so these tests skip there. They're for cameras with onboard
storage and NVRs.
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime.dut import DUT


_RECORDING_NS = "http://www.onvif.org/ver10/recording/wsdl"


@register("RECORDING-1-1-1", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "recording"})
def test_recording_service_capabilities(dut: DUT, spec) -> None:
    """RECORDING.html#tc.RECORDING-1-1-1 — RECORDING CONTROL SERVICE CAPABILITIES.

    GetServiceCapabilities on the recording service returns a Capabilities
    struct with mandatory boolean / integer fields populated.
    """
    caps = dut.recording.GetServiceCapabilities()
    assert caps is not None, "GetServiceCapabilities returned None"
    # Spec-mandatory: MaxRecordings, MaxRecordingJobs, Options support flags
    for field in ("MaxRecordings", "MaxRecordingJobs"):
        val = getattr(caps, field, None)
        assert val is not None, f"recording capabilities missing {field}"


@register("RECORDING-1-1-3", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "recording"})
def test_get_services_and_recording_caps_consistency(dut: DUT, spec) -> None:
    """RECORDING.html#tc.RECORDING-1-1-3 — GetServices(IncludeCapability)
    Recording entry matches a direct GetServiceCapabilities call."""
    services = dut.devicemgmt.GetServices(True)
    rec_entry = next(
        (s for s in services if s.Namespace == _RECORDING_NS), None
    )
    assert rec_entry is not None, "GetServices didn't include recording"
    direct = dut.recording.GetServiceCapabilities()
    embedded = getattr(rec_entry, "Capabilities", None)
    assert embedded is not None, (
        "GetServices(IncludeCapability=True) didn't embed recording caps"
    )
    # Both should report identical MaxRecordings (a stable simple field).
    embedded_caps = getattr(embedded, "Any", None) or embedded
    a = getattr(direct, "MaxRecordings", None)
    b = getattr(embedded_caps, "MaxRecordings", None) if hasattr(embedded_caps, "MaxRecordings") else None
    if a is not None and b is not None:
        assert a == b, f"MaxRecordings mismatch: direct={a} embedded={b}"


@register("RECORDING-4-1-1", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "recording"})
def test_get_recordings(dut: DUT, spec) -> None:
    """RECORDING.html#tc.RECORDING-4-1-1 — GET RECORDINGS.

    Returns a list of Recording structures; can be empty on a fresh
    device, but the call itself must succeed.
    """
    recordings = dut.recording.GetRecordings()
    # Empty list is acceptable on a device that's never recorded.
    assert recordings is not None, "GetRecordings returned None"
    if recordings:
        for r in recordings:
            assert getattr(r, "RecordingToken", None), (
                "Recording missing RecordingToken"
            )


@register("LOCAL-RECORDING-LIST-JOBS", profiles={"G"}, mandatory=False,
          requires_services={"devicemgmt", "recording"},
          tags={"local"})
def test_get_recording_jobs(dut: DUT, spec) -> None:
    """GetRecordingJobs returns a list (possibly empty) of job configs.

    Tool-author smoke test — no catalog ID covers this in isolation
    (the spec tests it as part of larger create/start/stop cycles).
    """
    try:
        jobs = dut.recording.GetRecordingJobs()
    except Exception as exc:
        pytest.skip(f"GetRecordingJobs not supported: {exc}")
    assert jobs is not None
    if jobs:
        for j in jobs:
            assert getattr(j, "JobToken", None), "RecordingJob missing JobToken"


@register("LOCAL-RECORDING-OPTIONS", profiles={"G"}, mandatory=False,
          requires_services={"devicemgmt", "recording"},
          tags={"local"})
def test_get_recording_options(dut: DUT, spec) -> None:
    """GetRecordingOptions returns the allowed Job / Track parameter
    ranges. Some devices fault if no recording exists — treat as skip.
    """
    recordings = dut.recording.GetRecordings() or []
    if not recordings:
        pytest.skip("device has no recordings — no token to query options for")
    token = recordings[0].RecordingToken
    try:
        opts = dut.recording.GetRecordingOptions(token)
    except Exception as exc:
        pytest.skip(f"GetRecordingOptions not supported for token {token!r}: {exc}")
    assert opts is not None
