"""pytest plugin — options + selection filtering + JSON reporter.

The actual test parametrisation lives in :mod:`onvif_tt.runner.dispatch`.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from ..registry import REGISTRY

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# pytest CLI options
# ---------------------------------------------------------------------------

def pytest_addoption(parser):  # noqa: D401
    group = parser.getgroup("onvif-tt", "ONVIF Test Tool options")
    group.addoption("--target", default="", help="DUT host[:port]")
    group.addoption("--user", default="", help="ONVIF username")
    group.addoption("--password", default="", help="ONVIF password")
    group.addoption(
        "--profile", action="append", default=[],
        help="Profile filter (S, T, ...). Repeatable; OR semantics.",
    )
    group.addoption(
        "--id-glob", action="append", default=[],
        help="fnmatch glob of test IDs to keep. Repeatable.",
    )
    group.addoption(
        "--mandatory-only", action="store_true",
        help="Only run tests flagged mandatory.",
    )
    group.addoption(
        "--allow-writes", action="store_true",
        help="Opt in to tests that mutate persistent DUT state "
             "(motor actuation, configuration change, reboot, factory "
             "reset). Off by default — tests flagged requires_writes "
             "skip unless this flag is given.",
    )
    group.addoption("--json-report", default="", help="Path to JSON result file.")
    group.addoption(
        "--corpus-dir",
        default=str(
            Path(__file__).resolve().parent.parent.parent.parent
            / "corpus" / "html"
        ),
        help="Directory holding the ONVIF spec HTML files.",
    )


# ---------------------------------------------------------------------------
# Filter parametrised items by --profile / --id-glob / --mandatory-only
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):
    profiles = set(config.getoption("--profile") or [])
    globs = config.getoption("--id-glob") or []
    mandatory_only = config.getoption("--mandatory-only")

    if not (profiles or globs or mandatory_only):
        return

    keep, drop = [], []
    for item in items:
        tid = _id_for_item(item)
        if tid is None:
            keep.append(item)
            continue
        impl = REGISTRY.get(tid)
        if impl is None:
            keep.append(item)
            continue
        if profiles and not (impl.profiles & profiles):
            drop.append(item)
            continue
        if mandatory_only and not impl.mandatory:
            drop.append(item)
            continue
        if globs and not any(fnmatch.fnmatch(tid, g) for g in globs):
            drop.append(item)
            continue
        keep.append(item)
    items[:] = keep
    if drop:
        config.hook.pytest_deselected(items=drop)


def _id_for_item(item) -> str | None:
    """Return the ONVIF test_id parametrised on an item, or None."""
    cs = getattr(item, "callspec", None)
    if cs is None:
        return None
    return cs.params.get("test_id")


# ---------------------------------------------------------------------------
# JSON reporter
# ---------------------------------------------------------------------------

_results: list[dict[str, Any]] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Worker-side: stash SOAP envelopes on the item's user_properties
    so they travel back to master under xdist (workers and master are
    separate processes; the global ``_results`` list isn't shared)."""
    outcome = yield
    rep = outcome.get_result()
    if rep.when != "call":
        return
    funcargs = getattr(item, "funcargs", None) or {}
    dut = funcargs.get("dut")
    if dut is not None:
        # user_properties is the supported channel for worker→master
        # data under xdist. Truncate envelopes to keep memory bounded.
        req = (dut.last_request or "")[:8000] or None
        resp = (dut.last_response or "")[:8000] or None
        item.user_properties.append(("onvif_tt_last_request", req))
        item.user_properties.append(("onvif_tt_last_response", resp))


def pytest_runtest_logreport(report):
    """Master-side hook (also runs in single-process mode). Receives
    the report from the worker, including user_properties.

    Records once per item:
      - 'call' phase for tests that ran (pass / fail / xfail / xpass)
      - 'setup' phase for tests that skipped or errored before call
    """
    if report.when == "call":
        pass
    elif report.when == "setup" and report.outcome != "passed":
        pass
    else:
        return

    tid = _id_for_report(report)
    if tid is None:
        return

    wasxfail = bool(getattr(report, "wasxfail", False))
    if wasxfail and report.outcome == "skipped":
        status = "xfailed"
    elif wasxfail and report.outcome == "passed":
        status = "xpassed"
    else:
        status = report.outcome

    rec: dict[str, Any] = {
        "id": tid,
        "status": status,
        "duration_s": report.duration,
        "longrepr": str(report.longrepr) if report.longrepr else "",
    }
    if wasxfail:
        rec["xfail_reason"] = str(getattr(report, "wasxfail", "")) or None
    impl = REGISTRY.get(tid)
    if impl:
        rec["profiles"] = sorted(impl.profiles)
        rec["mandatory"] = impl.mandatory

    # Recover SOAP envelopes from user_properties (set worker-side).
    props = dict(getattr(report, "user_properties", []) or [])
    if "onvif_tt_last_request" in props:
        rec["last_request"] = props["onvif_tt_last_request"]
    if "onvif_tt_last_response" in props:
        rec["last_response"] = props["onvif_tt_last_response"]

    _results.append(rec)


def _id_for_report(report) -> str | None:
    """Extract the parametrised ``test_id`` from a TestReport's nodeid.

    Format is ``…dispatch.py::test_onvif_case[DEVICE-1-1-2]``. Returns
    None for parser unit tests and other non-onvif items.
    """
    nodeid = getattr(report, "nodeid", "") or ""
    if "test_onvif_case[" not in nodeid:
        return None
    start = nodeid.index("[") + 1
    end = nodeid.rindex("]")
    return nodeid[start:end]


def pytest_sessionfinish(session, exitstatus):  # noqa: D401
    path = session.config.getoption("--json-report")
    if not path:
        return
    summary = {
        "total": 0, "passed": 0, "failed": 0,
        "skipped": 0, "error": 0,
        "xfailed": 0, "xpassed": 0,
    }
    for r in _results:
        summary["total"] += 1
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    with open(path, "w") as fh:
        json.dump(
            {
                "target": session.config.getoption("--target") or None,
                "summary": summary,
                "results": _results,
            },
            fh,
            indent=2,
        )
