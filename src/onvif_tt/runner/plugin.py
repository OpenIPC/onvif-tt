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
    group.addoption(
        "--allow-reboot", action="store_true",
        help="Opt in to tests that REBOOT the DUT and wait for recovery. "
             "Implies --allow-writes. Skipped by default — these tests "
             "take ~60–120 s each and disrupt every other ONVIF client "
             "on the LAN. Explicit flag required to avoid accidents.",
    )
    group.addoption(
        "--auth", default="auto", choices=("auto", "digest", "text", "none"),
        help="WS-Security UsernameToken password type. 'auto' (default) "
             "tries PasswordDigest and falls back to PasswordText, recording "
             "which the device accepted — SECURITY-1-1-1 then reports a "
             "device that needed the fallback. 'none' for devices with "
             "authentication disabled.",
    )
    group.addoption(
        "--no-schema-validation", action="store_true",
        help="Don't validate responses against the ONVIF schema. On by "
             "default: a test whose response was missing a mandatory "
             "element cannot honestly claim a pass. Use this only to work "
             "around a validator bug — a device that is knowingly "
             "non-conformant should carry an xfail_on matcher instead.",
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
_auth_state: dict[str, Any] | None = None


def record_auth_state(auth) -> None:
    """Called once by the dut fixture, after negotiation has settled.

    Run-level rather than per-test: which password type the device accepted
    is a property of the session, and repeating it on 146 results would say
    nothing extra.
    """
    global _auth_state
    _auth_state = {
        "requested": auth.requested.value,
        "accepted": auth.accepted,
        "rejected": list(auth.rejected),
        "clock_offset_s": (auth.clock_offset.total_seconds()
                           if auth.clock_offset is not None else None),
        "clock_probe_refused": auth.clock_probe_refused,
    }


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Worker-side: stash SOAP envelopes on the item's user_properties
    so they travel back to master under xdist (workers and master are
    separate processes; the global ``_results`` list isn't shared).

    The stash must happen **before** the yield. ``TestReport.__init__`` does
    ``self.user_properties = list(user_properties or [])`` — a copy, taken
    when the report is built during the yield. Appending afterwards mutates
    the item's list, which nothing reads again, so the data silently never
    reaches the report. That is why ``last_request`` / ``last_response`` /
    ``wsa_violations`` were absent from every results.json despite being
    documented in docs/schemas/ and in docs/ai-readme.md.
    """
    if call.when == "call":
        _stash_dut_state(item)
    yield


def _stash_dut_state(item) -> None:
    funcargs = getattr(item, "funcargs", None) or {}
    dut = funcargs.get("dut")
    if dut is not None:
        # user_properties is the supported channel for worker→master
        # data under xdist. Truncate envelopes to keep memory bounded.
        req = (dut.last_request or "")[:8000] or None
        resp = (dut.last_response or "")[:8000] or None
        item.user_properties.append(("onvif_tt_last_request", req))
        item.user_properties.append(("onvif_tt_last_response", resp))

        # WS-Addressing violations recorded during this test by the
        # WSAValidator zeep plugin. Serialise as list of dicts so the
        # master-side reporter can drop them straight into JSON.
        wsa = [
            {"operation": v.operation, "code": v.code, "detail": v.detail}
            for v in (getattr(dut.session, "wsa_violations", []) or [])
        ]
        item.user_properties.append(("onvif_tt_wsa_violations", wsa))
        # Clear so the next test starts with a fresh slate (DUT is
        # session-scoped, the violation list otherwise accumulates).
        dut.session.wsa_violations.clear()

        # Schema violations recorded by the ResponseValidator plugin. Unlike
        # wsa_violations these are NOT cleared here — dispatch clears them
        # immediately before each test body, so that fixture-time traffic
        # isn't attributed to whichever test ran first.
        item.user_properties.append(("onvif_tt_schema_violations", [
            {"operation": v.operation, "code": v.code,
             "path": v.path, "detail": v.detail}
            for v in (getattr(dut.session, "schema_violations", []) or [])
        ]))


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

    # Recover SOAP envelopes + WS-Addressing violations from
    # user_properties (set worker-side).
    props = dict(getattr(report, "user_properties", []) or [])
    if "onvif_tt_last_request" in props:
        rec["last_request"] = props["onvif_tt_last_request"]
    if "onvif_tt_last_response" in props:
        rec["last_response"] = props["onvif_tt_last_response"]
    if "onvif_tt_wsa_violations" in props:
        wsa = props["onvif_tt_wsa_violations"] or []
        if wsa:
            rec["wsa_violations"] = wsa
    if "onvif_tt_schema_violations" in props:
        sv = props["onvif_tt_schema_violations"] or []
        if sv:
            rec["schema_violations"] = sv

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


def _distinct_schema_violations() -> list[dict[str, Any]]:
    """Collapse per-test schema violations into one entry per real defect.

    A single malformed response is hit by every test that calls the
    operation — on the reference camera one bad ``GetProfiles`` reddens 13
    tests. Failing all 13 is right (none of them verified what they claimed),
    but reading the run as 13 problems is not. Grouped on (code, path,
    detail) with the affected test IDs attached.
    """
    grouped: dict[tuple, dict[str, Any]] = {}
    for r in _results:
        for v in r.get("schema_violations", []):
            key = (v["code"], v["path"], v["detail"])
            entry = grouped.get(key)
            if entry is None:
                entry = grouped[key] = {
                    "code": v["code"], "path": v["path"],
                    "detail": v["detail"], "occurrences": 0, "tests": [],
                }
            entry["occurrences"] += 1
            if r["id"] not in entry["tests"]:
                entry["tests"].append(r["id"])
    return sorted(grouped.values(),
                  key=lambda e: (-e["occurrences"], e["path"]))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """One line stating how many *defects* the run found, not how many reds."""
    distinct = _distinct_schema_violations()
    if not distinct:
        return
    affected = {t for e in distinct for t in e["tests"]}
    terminalreporter.write_sep("-", "ONVIF schema violations")
    terminalreporter.write_line(
        f"{len(distinct)} distinct schema violation(s) across "
        f"{len(affected)} test(s):"
    )
    for e in distinct:
        terminalreporter.write_line(
            f"  x{e['occurrences']:<3} [{e['code']}] {e['path']}"
        )


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
    payload: dict[str, Any] = {
        "target": session.config.getoption("--target") or None,
        "summary": summary,
        "results": _results,
    }
    if _auth_state is not None:
        payload["auth"] = _auth_state
    distinct = _distinct_schema_violations()
    if distinct:
        payload["schema_violations"] = distinct
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
