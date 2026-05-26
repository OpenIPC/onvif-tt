"""Render an onvif-tt results.json into a PDF Test Report.

Output shape mirrors the ONVIF Device Test Tool's "Test Report" PDF
(without the conformance-signing footer): header with target + run
summary, per-test detail table, and a failures/xfails appendix that
quotes longrepr inline.

Not a conformance report — it's a human-readable / auditor-friendly
artefact. The JUnit XML and JSON outputs remain the machine-readable
sources of truth.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    Preformatted,
)


_STATUS_COLOR = {
    "passed":  colors.HexColor("#2e7d32"),
    "failed":  colors.HexColor("#c62828"),
    "xfailed": colors.HexColor("#f9a825"),
    "xpassed": colors.HexColor("#1565c0"),
    "skipped": colors.HexColor("#616161"),
    "error":   colors.HexColor("#ad1457"),
}


def _styles():
    base = getSampleStyleSheet()
    return {
        "title":  base["Title"],
        "h2":     base["Heading2"],
        "h3":     ParagraphStyle(
            "h3-tight", parent=base["Heading3"], spaceBefore=8, spaceAfter=2,
        ),
        "body":   base["BodyText"],
        "small":  ParagraphStyle(
            "small", parent=base["BodyText"], fontSize=8, leading=10,
        ),
        "mono":   ParagraphStyle(
            "mono", parent=base["BodyText"],
            fontName="Courier", fontSize=7, leading=8,
        ),
    }


def write_pdf(results_path: str | Path, pdf_path: str | Path) -> Path:
    """Read ``results.json`` at ``results_path`` and write a PDF to
    ``pdf_path``. Returns the resolved output path.
    """
    results_path = Path(results_path)
    pdf_path = Path(pdf_path)
    data = json.loads(results_path.read_text())

    st = _styles()
    story = []
    doc = SimpleDocTemplate(
        str(pdf_path), pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"onvif-tt report — {data.get('target', '?')}",
        author="onvif-tt",
    )

    # ------------------------------------------------------------------ head
    story.append(Paragraph("ONVIF Test Report", st["title"]))
    target = data.get("target") or "(no target)"
    when = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    story.append(Paragraph(
        f"Target: <b>{target}</b><br/>"
        f"Generated: {when}<br/>"
        f"Source: <font name='Courier' size='8'>{results_path.name}</font>",
        st["body"]
    ))
    story.append(Spacer(1, 6 * mm))

    # ------------------------------------------------------------------ summary
    story.append(Paragraph("Summary", st["h2"]))
    summary = data.get("summary", {}) or {}
    summary_rows = [["Status", "Count"]]
    for key in ("total", "passed", "failed", "skipped",
                "error", "xfailed", "xpassed"):
        if key in summary:
            summary_rows.append([key, str(summary[key])])
    table = Table(summary_rows, colWidths=[40 * mm, 25 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474f")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN",      (1, 0), (1, -1), "RIGHT"),
        ("GRID",       (0, 0), (-1, -1), 0.25, colors.grey),
        ("FONTSIZE",   (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
            [colors.white, colors.HexColor("#f5f5f5")]),
    ]))
    story.append(table)
    story.append(Spacer(1, 6 * mm))

    # ------------------------------------------------------------------ per-test table
    results = data.get("results", []) or []
    story.append(Paragraph(f"Per-test outcomes ({len(results)})", st["h2"]))

    rows = [["Test ID", "Status", "Duration (s)"]]
    rows_meta = []  # parallel index of (status,) for colouring
    for r in sorted(results, key=lambda x: x.get("id", "")):
        rid = r.get("id", "?")
        status = r.get("status", "?")
        dur = r.get("duration_s", 0) or 0
        rows.append([rid, status, f"{dur:.3f}"])
        rows_meta.append(status)

    body_table = Table(
        rows, colWidths=[80 * mm, 25 * mm, 25 * mm], repeatRows=1,
    )
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474f")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 8),
        ("FONTNAME",   (0, 1), (0, -1), "Courier"),
        ("ALIGN",      (2, 1), (2, -1), "RIGHT"),
        ("GRID",       (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
            [colors.white, colors.HexColor("#f5f5f5")]),
    ]
    for i, status in enumerate(rows_meta, start=1):
        c = _STATUS_COLOR.get(status, colors.black)
        style.append(("TEXTCOLOR", (1, i), (1, i), c))
        style.append(("FONTNAME",  (1, i), (1, i), "Helvetica-Bold"))
    body_table.setStyle(TableStyle(style))
    story.append(body_table)

    # ------------------------------------------------------------------ failures appendix
    interesting = [
        r for r in results
        if r.get("status") in ("failed", "error", "xfailed", "xpassed")
        or r.get("wsa_violations")
    ]
    if interesting:
        story.append(PageBreak())
        story.append(Paragraph(
            f"Detail: failures, expected-failures, and WSA findings "
            f"({len(interesting)})",
            st["h2"]
        ))
        for r in sorted(interesting, key=lambda x: (x["status"], x["id"])):
            story.append(Paragraph(
                f"<font name='Courier'>{r['id']}</font> — "
                f"<font color='{_STATUS_COLOR.get(r['status'], colors.black).hexval()}'>"
                f"<b>{r['status']}</b></font>",
                st["h3"],
            ))
            if r.get("xfail_reason"):
                story.append(Paragraph(
                    f"<i>xfail reason:</i> {r['xfail_reason']}",
                    st["small"],
                ))
            if r.get("longrepr"):
                # Trim very long tracebacks but keep the actionable bit.
                lr = r["longrepr"]
                if len(lr) > 1800:
                    lr = lr[:900] + "\n…(truncated)…\n" + lr[-800:]
                story.append(Preformatted(lr, st["mono"]))
            wsa = r.get("wsa_violations") or []
            if wsa:
                story.append(Paragraph(
                    f"<b>{len(wsa)} WS-Addressing / SOAP violation(s):</b>",
                    st["small"],
                ))
                for v in wsa[:10]:
                    story.append(Paragraph(
                        f"  • [{v['code']}] {v['operation']}: {v['detail']}",
                        st["small"],
                    ))
            story.append(Spacer(1, 2 * mm))

    doc.build(story)
    return pdf_path
