"""Vendored ONVIF schema store.

``python-onvif-zeep`` bundles an ``onvif.xsd`` pinned at version 2.4.2 and
no ver20 media WSDL at all, so Media2 could not be bound and the ver20
types it references (``tt:StringList``, ``tt:VideoEncoder2Configuration``,
…) did not exist. Rather than patch around the library's copy we ship our
own, current, complete set and point every zeep client at it.

Two properties matter and both are enforced mechanically:

**Byte-identical.** The ONVIF copyright notice grants permission to copy,
distribute and display these documents, but states plainly: "No license is
granted to modify this document." So we do not rewrite ``schemaLocation``
attributes, which is the usual trick for making a schema set load offline.
Instead the tree mirrors the source URL path — ``www.onvif.org/ver20/media/
wsdl/media.wsdl`` — so ``media.wsdl``'s relative ``../../../ver10/schema/
onvif.xsd`` resolves on disk exactly as it does on the web, and the
remaining absolute-URL imports are served by :class:`VendoredSchemaTransport`.
``verify()`` re-hashes every file against the manifest, so drift is a test
failure rather than a mystery.

**Offline and complete.** :class:`VendoredSchemaTransport` raises
:class:`SchemaNotVendored` for any http(s) document not in the manifest
instead of quietly fetching it. A conformance tool that silently depended
on network reachability to decide what a device must implement would be
reporting on the wrong thing.

Regenerate with ``onvif-tt schemas refresh``; check with
``onvif-tt schemas verify``. Same committed-and-regenerable contract as
``corpus/parsed.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from lxml import etree
from zeep.transports import Transport

log = logging.getLogger(__name__)

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schemas"
MANIFEST_PATH = SCHEMA_ROOT / "MANIFEST.json"

_XSD_NS = "http://www.w3.org/2001/XMLSchema"
_WSDL_NS = "http://schemas.xmlsoap.org/wsdl/"

# Element → attribute holding the location of a referenced document.
_REFS: tuple[tuple[str, str], ...] = (
    (f"{{{_XSD_NS}}}import", "schemaLocation"),
    (f"{{{_XSD_NS}}}include", "schemaLocation"),
    (f"{{{_XSD_NS}}}redefine", "schemaLocation"),
    (f"{{{_WSDL_NS}}}import", "location"),
)

_USER_AGENT = "onvif-tt schema fetcher (+https://github.com/OpenIPC/onvif-tt)"


class SchemaNotVendored(RuntimeError):
    """A schema document was requested that the vendored store doesn't have.

    Deliberately fatal rather than falling back to the network: a run whose
    schema set depends on what a web server felt like serving that morning
    is not reproducible, and a conformance verdict has to be.
    """


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _key(url: str) -> str:
    """Normalise a URL to its store key.

    Drops the scheme, so the ``http://`` and ``https://`` spellings of
    ``www.w3.org/2001/xml.xsd`` — both of which appear in the ONVIF schema
    set — resolve to the same vendored file.
    """
    parsed = urlparse(url)
    return f"{parsed.netloc.lower()}{parsed.path}".rstrip("/")


def _load_manifest() -> dict[str, dict]:
    """Return ``{store key: entry}``. Empty if the store isn't populated."""
    try:
        raw = json.loads(MANIFEST_PATH.read_text())
    except FileNotFoundError:
        log.warning("schema manifest missing at %s — run `onvif-tt schemas "
                    "refresh`", MANIFEST_PATH)
        return {}
    return {_key(e["url"]): e for e in raw["documents"]}


_MANIFEST: dict[str, dict] | None = None


def manifest() -> dict[str, dict]:
    """The manifest, loaded once."""
    global _MANIFEST
    if _MANIFEST is None:
        _MANIFEST = _load_manifest()
    return _MANIFEST


def local_path(url: str) -> Path | None:
    """Filesystem path of the vendored copy of ``url``, or ``None``."""
    entry = manifest().get(_key(url))
    return SCHEMA_ROOT / entry["path"] if entry else None


# ---------------------------------------------------------------------------
# zeep transport
# ---------------------------------------------------------------------------

class VendoredSchemaTransport(Transport):
    """A zeep transport that serves schema documents from the vendored tree.

    Only :meth:`load` — which zeep uses for WSDL/XSD retrieval — is
    overridden. SOAP requests still go over the wire through the inherited
    ``post``/``post_xml``, so timeouts, sessions and TLS behave normally.
    """

    def load(self, url: str) -> bytes:
        if urlparse(url).scheme in ("http", "https"):
            path = local_path(url)
            if path is None:
                raise SchemaNotVendored(
                    f"{url} is not in the vendored schema store. onvif-tt "
                    f"does not fetch schemas at runtime — a conformance run "
                    f"must not depend on network reachability. Add it with "
                    f"`onvif-tt schemas refresh` and commit the result."
                )
            return path.read_bytes()
        # Local paths: relative imports inside the mirrored tree resolve to
        # real files (zeep/loader.py joins them against the parent doc).
        return super().load(url)


# ---------------------------------------------------------------------------
# verify / refresh
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify() -> list[str]:
    """Check the tree against the manifest. Returns a list of problems."""
    problems: list[str] = []
    entries = manifest()
    if not entries:
        return [f"no manifest at {MANIFEST_PATH}"]

    expected_paths = set()
    for entry in entries.values():
        path = SCHEMA_ROOT / entry["path"]
        expected_paths.add(path)
        if not path.is_file():
            problems.append(f"missing: {entry['path']}")
            continue
        data = path.read_bytes()
        if _sha256(data) != entry["sha256"]:
            problems.append(
                f"modified: {entry['path']} (sha256 differs from manifest — "
                f"vendored ONVIF documents must stay byte-identical)"
            )
        elif len(data) != entry["bytes"]:
            problems.append(f"size mismatch: {entry['path']}")

    for path in sorted(SCHEMA_ROOT.rglob("*")):
        if path.is_file() and path != MANIFEST_PATH and path not in expected_paths:
            problems.append(f"untracked: {path.relative_to(SCHEMA_ROOT)}")

    return problems


def _referenced(doc_url: str, data: bytes) -> list[str]:
    """Absolute URLs of every document ``data`` imports or includes."""
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise RuntimeError(f"{doc_url} is not well-formed XML: {exc}") from exc

    out: list[str] = []
    for tag, attr in _REFS:
        for el in root.iter(tag):
            loc = el.get(attr)
            if loc:
                out.append(urljoin(doc_url, loc))
    return out


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        return resp.read()


def refresh(seeds: list[str] | None = None) -> list[str]:
    """Re-fetch the schema closure from its canonical URLs.

    Crawls transitively from each service WSDL, so adding a service to
    ``runtime.services.SERVICES`` is enough — the documents it pulls in
    come along automatically and there is no hand-maintained URL list to
    drift. Returns the list of store keys written, sorted.
    """
    from . import services

    if seeds is None:
        seeds = [s.wsdl_url for s in services.SERVICES]

    documents: dict[str, tuple[str, bytes]] = {}  # key -> (url, data)
    queue = list(seeds)
    while queue:
        url = queue.pop(0)
        key = _key(url)
        if key in documents:
            continue
        log.info("fetching %s", url)
        data = _fetch(url)
        documents[key] = (url, data)
        queue.extend(_referenced(url, data))

    entries = []
    written = set()
    for key, (url, data) in documents.items():
        rel = Path(key)
        path = SCHEMA_ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.add(path)
        entries.append({
            "url": url,
            "path": str(rel),
            "sha256": _sha256(data),
            "bytes": len(data),
        })

    # Drop anything left over from a previous, larger closure.
    for path in sorted(SCHEMA_ROOT.rglob("*")):
        if path.is_file() and path != MANIFEST_PATH and path not in written:
            log.info("removing stale %s", path)
            path.unlink()

    entries.sort(key=lambda e: e["path"])
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps({"documents": entries}, indent=2, sort_keys=True) + "\n"
    )

    global _MANIFEST
    _MANIFEST = None  # force reload on next access
    return sorted(documents)
