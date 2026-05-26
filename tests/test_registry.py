"""Sanity tests for the registry layer.

These run on every push under ``pytest tests/`` — no DUT required —
so the CI workflow doesn't need to inline brittle shell + Python
one-liners (which broke once when an f-string used a backslash in a
way that's only valid on Python ≥ 3.12).
"""

from __future__ import annotations

import json
from pathlib import Path

from onvif_tt.registry import REGISTRY, discover, match_xfail


REPO = Path(__file__).resolve().parent.parent


def test_discover_populates_registry():
    discover()
    assert len(REGISTRY) > 50, (
        f"REGISTRY only has {len(REGISTRY)} entries — module discovery "
        f"may not be picking up everything under cases/"
    )


def test_registered_specs_are_in_corpus():
    """Every non-LOCAL @register'd ID must exist in the parsed corpus.

    LOCAL-* IDs are tool-author additions with no corpus counterpart and
    are exempt; everything else must match a spec section we can show.
    """
    discover()
    corpus = json.loads((REPO / "corpus" / "parsed.json").read_text())
    by_id = {c["id"]: c for c in corpus}

    missing = []
    for impl in REGISTRY.values():
        if impl.test_id.startswith("LOCAL-"):
            continue
        if impl.test_id not in by_id:
            missing.append(impl.test_id)
    assert not missing, (
        "the following @register IDs are not in corpus/parsed.json — "
        "either fix the spelling or use a LOCAL-* prefix:\n  "
        + "\n  ".join(missing)
    )


def test_no_duplicate_registrations():
    """@register raises on duplicates, but verify we don't end up with
    two impls writing to the same key by some import-order accident."""
    discover()
    seen: dict[str, str] = {}
    for impl in REGISTRY.values():
        prev = seen.get(impl.test_id)
        assert prev is None, (
            f"duplicate impl for {impl.test_id!r}: {prev} and {impl.qualname}"
        )
        seen[impl.test_id] = impl.qualname


def test_match_xfail_round_trip():
    """match_xfail should match exactly one of the xfail_on entries it
    was constructed with, given a satisfying device-info dict."""
    discover()
    for impl in REGISTRY.values():
        for matcher in impl.xfail_on:
            # Build a device_info that satisfies this matcher; expect
            # match_xfail to return the matcher's reason.
            info = {}
            for k, v in matcher.items():
                if k == "reason":
                    continue
                # Skip callable predicates — we'd have to know what
                # they accept. The literal-string case is what we test.
                if callable(v):
                    info[k] = "satisfies"
                    break
                info[k] = v
            else:
                assert match_xfail(impl, info) == matcher.get("reason"), (
                    f"matcher round-trip failed for {impl.test_id!r}: "
                    f"matcher={matcher!r}, info={info!r}, "
                    f"got={match_xfail(impl, info)!r}"
                )
