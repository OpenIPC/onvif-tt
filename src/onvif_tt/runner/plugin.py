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
    outcome = yield
    rep = outcome.get_result()
    # Record once per item:
    #  - 'call' phase outcome for tests that ran (pass/fail)
    #  - 'setup' phase outcome for tests that skipped or errored before call
    if rep.when == "call":
        pass  # always record
    elif rep.when == "setup" and rep.outcome != "passed":
        pass
    else:
        return
    tid = _id_for_item(item)
    if tid is None:
        return
    rec: dict[str, Any] = {
        "id": tid,
        "status": rep.outcome,
        "duration_s": rep.duration,
        "longrepr": str(rep.longrepr) if rep.longrepr else "",
    }
    impl = REGISTRY.get(tid)
    if impl:
        rec["profiles"] = sorted(impl.profiles)
        rec["mandatory"] = impl.mandatory
    # SOAP traffic snapshot (best-effort).
    funcargs = getattr(item, "funcargs", None) or {}
    dut = funcargs.get("dut")
    if dut is not None:
        rec["last_request"] = dut.last_request
        rec["last_response"] = dut.last_response
    _results.append(rec)


def pytest_sessionfinish(session, exitstatus):  # noqa: D401
    path = session.config.getoption("--json-report")
    if not path:
        return
    summary = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "error": 0}
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
