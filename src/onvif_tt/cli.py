"""``onvif-tt`` command-line entry point.

Subcommands:

* ``list``     — print catalog of test cases (filterable, JSON or human).
* ``show``     — print one test case's full record.
* ``corpus``   — manage the parsed corpus JSON cache.
* ``run``      — execute registered tests against a target (delegates to pytest).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

from . import __version__
from .registry import REGISTRY, discover
from .specs.parser import cases_to_json, parse_corpus


# ---------------------------------------------------------------------------
# Locating the corpus directory
# ---------------------------------------------------------------------------

def _default_corpus_dir() -> Path:
    """Find ``corpus/html`` relative to the installed package or repo root."""
    here = Path(__file__).resolve()
    # Layout: <repo>/src/onvif_tt/cli.py → <repo>/corpus/html
    candidate = here.parent.parent.parent / "corpus" / "html"
    if candidate.is_dir():
        return candidate
    # Editable install fallback — also search cwd.
    cwd_candidate = Path.cwd() / "corpus" / "html"
    if cwd_candidate.is_dir():
        return cwd_candidate
    return candidate  # return the first, even if absent — error handling later


def _default_cache_path() -> Path:
    return _default_corpus_dir().parent / "parsed.json"


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------

def _load_catalog(corpus_dir: Path | None = None):
    """Try the JSON cache first; fall back to parsing on the fly."""
    cd = corpus_dir or _default_corpus_dir()
    cache = cd.parent / "parsed.json"
    if cache.exists():
        with open(cache) as fh:
            data = json.load(fh)
        return data
    cases = parse_corpus(cd)
    return cases_to_json(cases)


# ---------------------------------------------------------------------------
# `list` subcommand
# ---------------------------------------------------------------------------

def _cmd_list(args) -> int:
    discover()
    catalog = _load_catalog()
    implemented = set(REGISTRY.keys())

    def _passes(c):
        if args.profile_area and c["profile_area"] not in args.profile_area:
            return False
        if args.id_glob and not any(
            fnmatch.fnmatch(c["id"], g) for g in args.id_glob
        ):
            return False
        if args.implemented and c["id"] not in implemented:
            return False
        if args.missing and c["id"] in implemented:
            return False
        return True

    rows = [c for c in catalog if _passes(c)]

    if args.format == "json":
        if args.compact:
            out = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "profile_area": r["profile_area"],
                    "wsdl_reference": r["labels"].get("WSDL Reference", ""),
                    "implemented": r["id"] in implemented,
                }
                for r in rows
            ]
        else:
            for r in rows:
                r["implemented"] = r["id"] in implemented
            out = rows
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for r in rows:
            mark = "*" if r["id"] in implemented else " "
            print(f"{mark} {r['id']:32s} {r['profile_area']:15s} {r['title']}")
        print(f"\n{len(rows)} test cases (implemented marked with *)")
    return 0


# ---------------------------------------------------------------------------
# `show` subcommand
# ---------------------------------------------------------------------------

def _cmd_show(args) -> int:
    discover()
    catalog = _load_catalog()
    rec = next((c for c in catalog if c["id"] == args.test_id), None)
    if rec is None:
        print(f"unknown test id: {args.test_id!r}", file=sys.stderr)
        return 2
    rec = dict(rec)
    rec["implemented"] = args.test_id in REGISTRY
    if args.test_id in REGISTRY:
        impl = REGISTRY[args.test_id]
        rec["impl"] = {
            "qualname": impl.qualname,
            "profiles": sorted(impl.profiles),
            "mandatory": impl.mandatory,
            "requires_services": sorted(impl.requires_services),
        }
    if args.format == "json":
        json.dump(rec, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_human(rec)
    return 0


def _print_human(rec) -> None:
    print(f"{rec['id']}  (spec {rec['spec_file']}, version {rec['version']})")
    print(f"Title: {rec['title']}")
    for k, v in rec.get("labels", {}).items():
        print(f"  {k}: {v}")
    print(f"  Implemented: {rec.get('implemented', False)}")
    if "impl" in rec:
        print(f"    qualname: {rec['impl']['qualname']}")
        print(f"    profiles: {rec['impl']['profiles']}")
        print(f"    mandatory: {rec['impl']['mandatory']}")
    print("\nProcedure:")
    _print_steps(rec.get("procedure", []), indent=2)
    if rec.get("pass_criteria"):
        print(f"\nPASS: {rec['pass_criteria']}")
    if rec.get("fail_criteria"):
        print(f"FAIL: {rec['fail_criteria']}")


def _print_steps(steps, indent=0) -> None:
    pad = " " * indent
    for s in steps:
        sid = f"[{s['step_id']}] " if s.get("step_id") else ""
        ops = f"  ops={s['operations']}" if s.get("operations") else ""
        print(f"{pad}- {sid}{s['text']}{ops}")
        if s.get("sub_steps"):
            _print_steps(s["sub_steps"], indent + 4)


# ---------------------------------------------------------------------------
# `corpus` subcommand
# ---------------------------------------------------------------------------

def _cmd_corpus(args) -> int:
    if args.corpus_cmd == "refresh":
        cd = _default_corpus_dir()
        if not cd.is_dir():
            print(f"corpus html dir not found: {cd}", file=sys.stderr)
            return 2
        cache = _default_cache_path()
        cases = parse_corpus(cd)
        with open(cache, "w") as fh:
            json.dump(cases_to_json(cases), fh, indent=2, sort_keys=True)
        print(f"wrote {cache} ({len(cases)} test cases)")
        return 0
    if args.corpus_cmd == "stats":
        catalog = _load_catalog()
        by_area: dict[str, int] = {}
        for c in catalog:
            by_area[c["profile_area"]] = by_area.get(c["profile_area"], 0) + 1
        for area, n in sorted(
            by_area.items(), key=lambda kv: kv[1], reverse=True
        ):
            print(f"  {area:20s} {n}")
        print(f"  {'TOTAL':20s} {len(catalog)}")
        return 0
    return 2


# ---------------------------------------------------------------------------
# `run` subcommand — wraps pytest
# ---------------------------------------------------------------------------

def _cmd_run(args, unknown_args) -> int:
    import pytest

    # Point pytest at the dispatch module bundled with the package, NOT at
    # tests/ — those are parser unit tests, not for the dispatch loop.
    from .runner import dispatch as _dispatch_mod
    pytest_args = [
        _dispatch_mod.__file__,
        f"--target={args.target}",
        f"--user={args.user}",
        f"--password={args.password}",
    ]
    for p in args.profile or []:
        pytest_args.append(f"--profile={p}")
    for g in args.id_glob or []:
        pytest_args.append(f"--id-glob={g}")
    if args.mandatory_only:
        pytest_args.append("--mandatory-only")
    if args.allow_writes:
        pytest_args.append("--allow-writes")
    if args.junit_xml:
        pytest_args.append(f"--junit-xml={args.junit_xml}")
    if args.json_report:
        pytest_args.append(f"--json-report={args.json_report}")
    if args.corpus_dir:
        pytest_args.append(f"--corpus-dir={args.corpus_dir}")
    # Let users append raw pytest args after `--`.
    pytest_args.extend(unknown_args)
    return pytest.main(pytest_args)


# ---------------------------------------------------------------------------
# argparse setup
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="onvif-tt", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="list catalog entries")
    lp.add_argument("--profile-area", action="append",
                    help="filter to profile area (BASE, MEDIA2, PTZ, ...)")
    lp.add_argument("--id-glob", action="append",
                    help="filter by fnmatch ID pattern; repeatable")
    lp.add_argument("--implemented", action="store_true",
                    help="show only IDs with an implementation")
    lp.add_argument("--missing", action="store_true",
                    help="show only IDs without an implementation")
    lp.add_argument("--format", choices=("human", "json"), default="human")
    lp.add_argument("--compact", action="store_true",
                    help="JSON: emit only id/title/profile_area/wsdl/implemented")
    lp.set_defaults(func=_cmd_list)

    sp = sub.add_parser("show", help="print one test case in detail")
    sp.add_argument("test_id")
    sp.add_argument("--format", choices=("human", "json"), default="human")
    sp.set_defaults(func=_cmd_show)

    cp = sub.add_parser("corpus", help="manage the parsed-spec cache")
    cp_sub = cp.add_subparsers(dest="corpus_cmd", required=True)
    cp_sub.add_parser("refresh", help="re-parse corpus/html/*.html")
    cp_sub.add_parser("stats", help="counts by profile area")
    cp.set_defaults(func=_cmd_corpus)

    rp = sub.add_parser("run", help="run registered tests against a DUT")
    rp.add_argument("--target", required=True, help="host[:port]")
    rp.add_argument("--user", default="")
    rp.add_argument("--password", default="")
    rp.add_argument("--profile", action="append")
    rp.add_argument("--id-glob", action="append")
    rp.add_argument("--mandatory-only", action="store_true")
    rp.add_argument("--allow-writes", action="store_true",
                    help="enable tests that mutate DUT state (motors, "
                         "config, reboot, factory reset)")
    rp.add_argument("--junit-xml")
    rp.add_argument("--json-report")
    rp.add_argument("--corpus-dir")
    rp.set_defaults(func=_cmd_run)

    repp = sub.add_parser(
        "report",
        help="render a results.json into a PDF Test Report",
    )
    repp.add_argument("results_json", help="results.json file from `onvif-tt run`")
    repp.add_argument("output_pdf", help="output PDF path")
    repp.set_defaults(func=_cmd_report)

    return p


def _cmd_report(args) -> int:
    from .runner.pdf_reporter import write_pdf
    out = write_pdf(args.results_json, args.output_pdf)
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, unknown = parser.parse_known_args(argv)
    if args.cmd == "run":
        return args.func(args, unknown)
    if unknown:
        # No other subcommand accepts unknown args.
        parser.error(f"unknown arguments: {unknown}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
