"""Parse the ONVIF Test Specification HTML corpus into TestCase records.

The specifications ship as DocBook embedded inside HTML — every test
case is a ``<section id="tc.SOMETHING" version="X.YY">``. We rely only
on lxml; no schema downloads, no network.

Public entry points:

* :func:`parse_corpus` — walk a directory of .html files.
* :func:`parse_file`   — parse one file.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from lxml import html

from .models import TestCase, TestStep

# A *top-level* test ID looks like ``tc.SECTION-N-N-N`` (optionally with a
# ``-vXX.YY`` version suffix). Step IDs add a further ``.N.N…`` tail.
_TEST_ID_RE = re.compile(
    r"^tc\.([A-Z][A-Z0-9_]+-[0-9]+-[0-9]+-[0-9]+(?:-v[0-9.]+)?)$"
)

# Labels we recognise on the bold-emph lead of a `<para>`.
_KNOWN_LABELS = {
    "Test Case ID",
    "Specification Coverage",
    "Feature Under Test",
    "WSDL Reference",
    "Test Purpose",
    "Pre-Requisite",
    "Pre-Requisites",
    "Test Configuration",
    "Test Procedure",
    "Test Result",
    "Note",
}


def _normalise(text: str | None) -> str:
    """Collapse whitespace and trim — DocBook output is full of indent noise."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _bold_lead(p) -> tuple[str | None, str]:
    """Return ``(label, value_text)`` for a `<para>` that starts with a bold
    label like ``<emphasis role="bold">Test Purpose:</emphasis>``. ``label``
    is ``None`` if the paragraph doesn't have that shape.
    """
    first = next(iter(p), None)
    if first is None or first.tag != "emphasis" or first.get("role") != "bold":
        return None, ""
    label = _normalise(first.text_content()).rstrip(":")
    # value = everything after the emphasis tail + remaining siblings
    parts: list[str] = []
    tail = first.tail or ""
    parts.append(tail)
    sib = first.getnext()
    while sib is not None:
        parts.append(sib.text_content())
        if sib.tail:
            parts.append(sib.tail)
        sib = sib.getnext()
    return label, _normalise("".join(parts))


def _ops_in(node) -> list[str]:
    """Collect ONVIF operation names — bold emphasis tokens that look like
    a SOAP operation (CamelCase, no spaces)."""
    ops: list[str] = []
    for em in node.iter("emphasis"):
        if em.get("role") != "bold":
            continue
        txt = _normalise(em.text_content())
        # Skip the leading "Label:" emphases we've already parsed.
        if txt.endswith(":") and txt[:-1] in _KNOWN_LABELS:
            continue
        # Heuristic: SOAP op names are CamelCase, start with an uppercase
        # letter, contain no spaces.
        if re.fullmatch(r"[A-Z][A-Za-z0-9_]+", txt):
            ops.append(txt)
    # de-dup, preserve order
    seen: set[str] = set()
    return [o for o in ops if not (o in seen or seen.add(o))]


def _vars_in(node) -> list[str]:
    """Collect italic-emphasis variable names from a procedure node."""
    vs: list[str] = []
    for em in node.iter("emphasis"):
        if em.get("role") != "italic":
            continue
        txt = _normalise(em.text_content())
        if txt and " " not in txt:
            vs.append(txt)
    seen: set[str] = set()
    return [v for v in vs if not (v in seen or seen.add(v))]


def _direct_listitems(ol_or_orderedlist):
    """Yield the direct ``<listitem>`` children of an ordered-list node.

    The HTML rendering wraps an ``<ol>`` around a DocBook ``<orderedlist>``;
    the ``<listitem>`` children live one level deeper. Handle both shapes.
    """
    for child in ol_or_orderedlist.iterchildren():
        if child.tag == "listitem":
            yield child
        elif child.tag == "orderedlist":
            yield from _direct_listitems(child)
        elif child.tag == "ol":
            yield from _direct_listitems(child)


def _nested_lists(li):
    """Find ordered/unordered lists nested *directly* inside a listitem
    (i.e. for sub-steps). We descend through wrapping ``<para>`` and
    ``<div>`` nodes that DocBook-html injects."""
    found = []
    for ol in li.iter("ol"):
        # only count it if no other ol/orderedlist is between it and li
        a = ol.getparent()
        while a is not None and a is not li:
            if a.tag in ("ol", "orderedlist") and a is not ol:
                break
            a = a.getparent()
        else:
            found.append(ol)
    return found


def _parse_step(li) -> TestStep:
    """Turn one DocBook ``<listitem>`` into a :class:`TestStep`."""
    # Addressable step id lives on a child ``<div id="tc.<test>.<step>">``.
    step_id: str | None = None
    for d in li.iter("div"):
        did = d.get("id")
        if did and did.startswith("tc."):
            step_id = did
            break

    operations = _ops_in(li)
    variables = _vars_in(li)

    # Sub-steps: walk nested ordered lists at our depth.
    sub_steps: list[TestStep] = []
    for ol in _nested_lists(li):
        for sub_li in _direct_listitems(ol):
            sub_steps.append(_parse_step(sub_li))

    # Body text — strip nested lists so we don't repeat sub-step text.
    from copy import deepcopy
    clone = deepcopy(li)
    for ol in clone.xpath(".//ol | .//orderedlist | .//ul | .//itemizedlist"):
        parent = ol.getparent()
        if parent is not None:
            parent.remove(ol)
    text = _normalise(clone.text_content())

    return TestStep(
        step_id=step_id,
        text=text,
        operations=operations,
        variables=variables,
        sub_steps=sub_steps,
    )


def _parse_procedure(section) -> list[TestStep]:
    """Find the procedure ordered-list and parse its top-level steps.

    The procedure ``<ol>`` lives inside the first ``<para>`` directly
    following the ``Test Procedure:`` label paragraph. We pick the
    *first* ordered list that has no ordered-list ancestor inside this
    section.
    """
    candidates = []
    for ol in section.iter("ol"):
        # ensure this ol isn't nested inside another ol (would be a sub-list)
        a = ol.getparent()
        nested = False
        while a is not None and a is not section:
            if a.tag in ("ol", "orderedlist"):
                nested = True
                break
            a = a.getparent()
        if not nested:
            candidates.append(ol)

    if not candidates:
        return []

    top = candidates[0]
    return [_parse_step(li) for li in _direct_listitems(top)]


def _parse_pass_fail(section) -> tuple[str, str]:
    """Extract the ``PASS –`` and ``FAIL –`` paragraph contents."""
    pass_text, fail_text = "", ""
    for p in section.findall("./para"):
        txt = _normalise(p.text_content())
        if txt.startswith("PASS"):
            # Strip leading "PASS –" prefix.
            pass_text = re.sub(r"^PASS\s*[–-]\s*", "", txt)
        elif txt.startswith("FAIL"):
            fail_text = re.sub(r"^FAIL\s*[–-]\s*", "", txt)
    return pass_text, fail_text


def _parse_section(section, spec_file: str, profile_area: str) -> TestCase:
    raw_id = section.get("id", "")
    m = _TEST_ID_RE.match(raw_id)
    assert m, f"called with non-top-level section {raw_id!r}"
    tid = m.group(1)
    version = section.get("version") or None

    # Title — first `<title>` child.
    titles = section.findall("./title")
    title = _normalise(titles[0].text_content()) if titles else ""

    labels: dict[str, str] = {}
    notes: list[str] = []
    for p in section.findall("./para"):
        label, value = _bold_lead(p)
        if label is None:
            continue
        if label == "Note":
            notes.append(value)
        elif label in _KNOWN_LABELS:
            # Don't clobber Procedure with its label-only "Test Procedure:".
            if label != "Test Procedure":
                labels[label] = value

    procedure = _parse_procedure(section)
    pass_text, fail_text = _parse_pass_fail(section)

    return TestCase(
        id=tid,
        version=version,
        profile_area=profile_area,
        spec_file=spec_file,
        title=title,
        labels=labels,
        procedure=procedure,
        pass_criteria=pass_text,
        fail_criteria=fail_text,
        notes=notes,
    )


def parse_file(path: str | os.PathLike[str]) -> list[TestCase]:
    """Parse one ONVIF spec HTML file into a list of TestCase records."""
    p = Path(path)
    profile_area = p.stem  # "BASE", "MEDIA2", "PTZ", ...
    tree = html.parse(str(p))
    sections = [
        s for s in tree.xpath("//section") if _TEST_ID_RE.match(s.get("id", ""))
    ]
    return [_parse_section(s, p.name, profile_area) for s in sections]


def parse_corpus(html_dir: str | os.PathLike[str]) -> list[TestCase]:
    """Parse every ``*.html`` file under ``html_dir``.

    Order is deterministic: files sorted by name, test cases sorted by id
    within each file. Stable order is what makes the JSON cache diff-able.
    """
    root = Path(html_dir)
    files = sorted(p for p in root.glob("*.html") if p.is_file())
    all_cases: list[TestCase] = []
    for f in files:
        cases = parse_file(f)
        cases.sort(key=lambda c: c.id)
        all_cases.extend(cases)
    return all_cases


def cases_to_json(cases: Iterable[TestCase]) -> list[dict]:
    """Serialise a sequence of TestCase records to JSON-safe dicts."""
    return [c.to_dict() for c in cases]
