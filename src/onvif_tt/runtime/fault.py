"""Helpers for asserting that a DUT operation returns a SOAP Fault.

``python-onvif-zeep``'s service_wrapper catches ``zeep.exceptions.Fault``
and re-raises it as ``onvif.exceptions.ONVIFError(fault.message)`` —
**no exception chaining**. So our test code can't simply
``except zeep.exceptions.Fault`` and expect to catch the conformant
case; it also needs to recognise the wrapped form.

The wrap drops the type info but preserves the fault message, so we
distinguish "Fault returned" from "transport blew up" by looking at the
message: transport errors carry markers like "End of file", "timed
out", "connection refused", etc.; SOAP faults read like the
fault's <Reason><Text>… or "Sender/Receiver" subcode strings.
"""

from __future__ import annotations

import pytest
import zeep.exceptions

try:  # local import — the onvif package only exists in installed envs
    from onvif.exceptions import ONVIFError
except ImportError:  # pragma: no cover
    ONVIFError = type("ONVIFError", (Exception,), {})  # placeholder

# Substrings that indicate the failure was *transport-level*, not a
# SOAP Fault. If any of these appear in the ONVIFError message we
# treat it as "device did NOT return a fault" — the assertion fails.
_TRANSPORT_MARKERS = (
    "end of file",
    "no input",
    "operation interrupted",
    "timed out",
    "connection reset",
    "connection refused",
    "connection aborted",
    "could not connect",
    "name or service not known",
    "no route to host",
    "network is unreachable",
)


def looks_like_soap_fault(exc: BaseException) -> bool:
    """True if ``exc`` was raised by a SOAP Fault response."""
    if isinstance(exc, zeep.exceptions.Fault):
        return True
    if isinstance(exc, ONVIFError):
        msg = str(exc).lower()
        return not any(m in msg for m in _TRANSPORT_MARKERS)
    return False


def assert_soap_fault(callable_, *args, **kwargs) -> None:
    """Run ``callable_(*args, **kwargs)`` and assert it raised a SOAP
    Fault (possibly wrapped in ``ONVIFError`` by python-onvif-zeep).

    Use in tests like::

        from ..runtime.fault import assert_soap_fault
        assert_soap_fault(
            dut.devicemgmt.GetCapabilities, "ThisIsNotARealCategory"
        )
    """
    try:
        callable_(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if looks_like_soap_fault(exc):
            return
        pytest.fail(
            f"expected SOAP Fault, got {type(exc).__name__}: "
            f"{str(exc)[:300]}"
        )
    pytest.fail("DUT did not fault on invalid input (no exception raised)")
