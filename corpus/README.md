# corpus/

This directory holds the **ONVIF Test Specification** HTML corpus that
the tool parses into its catalog.

## Files

* `html/` — 22 specification documents, one per ONVIF service or
  profile (BASE, MEDIA2, PTZ, EVENT, IMAGING, RECORDING, …).
* `parsed.json` — machine-readable cache produced by
  `onvif-tt corpus refresh`. Committed so the catalog is available
  without a Python install / corpus dir scan.

## Provenance and license

The HTML in `html/` is the rendered DocBook source of the public
**ONVIF Test Specification** documents (version 20.12, December 2020).
Each file carries the ONVIF copyright header verbatim:

> Recipients of this document may copy, distribute, publish, or
> display this document so long as this copyright notice, license and
> disclaimer are retained with all copies of the document. No license
> is granted to modify this document.

We redistribute them unmodified, with the copyright notices intact, as
permitted. The exact same documents are available as PDFs at
<https://www.onvif.org/profiles/conformance/> — these are simply the
cross-linked HTML rendering bundled with the closed-source ONVIF
Device Test Tool installer.

The **tool** that consumes these documents (`src/onvif_tt/`) is MIT
licensed and is *not* an ONVIF deliverable. ONVIF makes no warranty
about it, and `onvif-tt` cannot itself produce a formal Declaration of
Conformance — only the official ONVIF Device Test Tool can.

## Refreshing the cache

`parsed.json` must match what `onvif_tt.specs.parser` produces today.
Regenerate after any parser change:

```bash
onvif-tt corpus refresh
```

The deterministic-parse test in `tests/test_parser.py` will fail in CI
if the committed `parsed.json` drifts.
