"""SystemReboot-cycle tests — DEVICE-3-1-8 + DISCOVERY-1-1-2.

These take 30–120 seconds each and TAKE THE DUT OFFLINE for the whole
window. They're gated behind ``requires_reboot=True``, which only fires
if the operator passes ``--allow-reboot`` (a separate flag beyond
``--allow-writes`` — picking those checkboxes shouldn't get you a
device outage).
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from ..registry import register
from ..runtime.dut import DUT, DUTConfig

log = logging.getLogger(__name__)


def _wait_for_recovery(config: DUTConfig, timeout_s: int = 120,
                       poll_every_s: float = 2.0) -> DUT:
    """Poll ``GetDeviceInformation`` on a fresh DUT until the device
    answers (it's back online), or fail after ``timeout_s``.

    Returns the new DUT object on success — the old one's zeep
    connections are stale after the reboot.
    """
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            fresh = DUT(config)
            fresh.devicemgmt.GetDeviceInformation()
            log.info("DUT recovered after %d attempt(s)", attempts)
            return fresh
        except Exception as exc:  # noqa: BLE001 — anything counts as "still down"
            last_exc = exc
            time.sleep(poll_every_s)
    pytest.fail(
        f"DUT did not recover within {timeout_s}s after SystemReboot "
        f"(last error: {type(last_exc).__name__}: {last_exc})"
    )


# ---------------------------------------------------------------------------
# DEVICE-3-1-8 — reboot + post-reboot identity check
# ---------------------------------------------------------------------------

@register("DEVICE-3-1-8",
          profiles={"S", "T", "G", "A", "D", "M"},
          mandatory=True,
          requires_services={"devicemgmt"},
          requires_writes=True,
          requires_reboot=True)
def test_system_reboot(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-3-1-8 — SYSTEM COMMAND REBOOT.

    Procedure:
      1. Capture baseline via GetDeviceInformation
         (Manufacturer / Model / SerialNumber / HardwareId).
      2. Invoke SystemReboot; verify a SystemRebootResponse with a
         non-empty Message comes back.
      3. Wait up to 120 s for the device to come back online (poll
         GetDeviceInformation on a fresh DUT).
      4. Verify the same Manufacturer / Model / SerialNumber /
         HardwareId — the device that came back must be the device
         that went down.
    """
    baseline = dut.devicemgmt.GetDeviceInformation()

    log.info("invoking SystemReboot on %s", dut.config.host)
    resp = dut.devicemgmt.SystemReboot()
    # SystemRebootResponse carries a Message string per spec.
    assert resp is not None, "SystemReboot returned None"
    msg = getattr(resp, "Message", None) or resp
    assert msg, "SystemRebootResponse.Message empty"

    # Give the device a short grace period before we start polling —
    # many cameras respond to SystemReboot *then* drop the network.
    time.sleep(3.0)

    fresh = _wait_for_recovery(dut.config, timeout_s=120)
    after = fresh.devicemgmt.GetDeviceInformation()
    for field in ("Manufacturer", "Model", "SerialNumber", "HardwareId"):
        b = getattr(baseline, field, None)
        a = getattr(after, field, None)
        assert a == b, (
            f"{field} changed across reboot: before={b!r} after={a!r} "
            "(device that came back is not the device that went down)"
        )


# ---------------------------------------------------------------------------
# DISCOVERY-1-1-2 — Hello message after reboot
# ---------------------------------------------------------------------------

@register("DISCOVERY-1-1-2",
          profiles={"S", "T"},
          mandatory=True,
          requires_services={"devicemgmt"},
          requires_writes=True,
          requires_reboot=True)
def test_hello_after_reboot(dut: DUT, spec) -> None:
    """BASE.html#tc.DISCOVERY-1-1-2 — HELLO MESSAGE VALIDATION.

    Spec procedure:
      1. Start a WS-Discovery listener.
      2. Reboot the device.
      3. Capture the multicast Hello message it emits on boot.
      4. Verify it carries the expected EndpointReference + XAddrs +
         Types/Scopes.

    We use python-WSDiscovery's listener with a `helloCallback` so the
    Hello received during recovery gets stashed and inspected.

    Caveats:
      - Multicast may not reach the test runner in some CI / containerised
        networks; if no Hello arrives within the window, we *don't* fail —
        we skip with a clear reason. (The recovery itself is verified by
        DEVICE-3-1-8 separately.)
      - Many cheap cameras emit Hello with `Types=tds:Device` only, and
        scopes covering the profile string. We assert the EndpointReference
        is present and the XAddrs include something with the DUT's IP.
    """
    try:
        from wsdiscovery import WSDiscovery
    except ImportError:  # pragma: no cover
        pytest.skip("python-WSDiscovery not installed")

    captured: list = []
    captured_lock = threading.Lock()

    def _on_hello(service):
        with captured_lock:
            captured.append(service)

    wsd = WSDiscovery()
    try:
        wsd.setRemoteServiceHelloCallback(_on_hello)
        wsd.start()
    except Exception as exc:
        pytest.skip(
            f"WSDiscovery listener could not start (multicast may be blocked): {exc}"
        )

    try:
        # Reboot AFTER the listener is armed.
        dut.devicemgmt.SystemReboot()
        time.sleep(3.0)

        # Wait until the device is back AND a Hello has arrived,
        # whichever takes longer (within the timeout).
        deadline = time.monotonic() + 120
        fresh: DUT | None = None
        while time.monotonic() < deadline:
            if fresh is None:
                try:
                    candidate = DUT(dut.config)
                    candidate.devicemgmt.GetDeviceInformation()
                    fresh = candidate
                except Exception:
                    pass
            with captured_lock:
                hello_seen = bool(captured)
            if fresh is not None and hello_seen:
                break
            time.sleep(2.0)

        if fresh is None:
            pytest.fail("DUT did not recover within 120 s after SystemReboot")

        with captured_lock:
            hellos = list(captured)
        if not hellos:
            pytest.skip(
                "no Hello observed during recovery window — multicast may "
                "be blocked between DUT and runner (recovery itself is "
                "verified by DEVICE-3-1-8)"
            )

        # Inspect the most recent Hello. Validate EPR + XAddrs.
        # wsdiscovery Service objects expose: .getEPR(), .getXAddrs(),
        # .getScopes(), .getTypes().
        host = dut.config.host
        matched = False
        for svc in hellos:
            xaddrs = list(getattr(svc, "getXAddrs", lambda: [])())
            if any(host in str(x) for x in xaddrs):
                epr = svc.getEPR() if hasattr(svc, "getEPR") else None
                assert epr, "Hello message carries no EndpointReference"
                matched = True
                break
        assert matched, (
            f"Hello message(s) observed but none carry XAddr containing the "
            f"DUT's IP {host}: {[svc.getXAddrs() for svc in hellos]}"
        )
    finally:
        try:
            wsd.stop()
        except Exception:
            pass
