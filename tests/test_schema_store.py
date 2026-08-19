"""Unit tests for the vendored ONVIF schema store.

No DUT required; runs on every CI matrix entry. These are the regression
net for issue #1: they prove the schema set is intact, complete, and
usable entirely offline, so a Media2 client can always be built.
"""

from __future__ import annotations

import pytest

from onvif_tt.runtime import schema_store, services


def test_manifest_is_populated():
    assert schema_store.manifest(), (
        "vendored schema store is empty — run `onvif-tt schemas refresh`"
    )


def test_tree_matches_manifest():
    """Every vendored file hashes to what MANIFEST.json recorded.

    ONVIF grants permission to redistribute these documents but not to
    modify them, so byte-drift is a licence problem as well as a
    correctness one. Same committed-and-regenerable contract as
    ``corpus/parsed.json``.
    """
    problems = schema_store.verify()
    assert not problems, "\n".join(problems)


def test_url_normalisation_ignores_scheme():
    """``xml.xsd`` is imported over both http and https by the ONVIF set.

    Both spellings have to resolve to the one vendored copy, or the
    schema set silently needs the network for one of them.
    """
    http = schema_store.local_path("http://www.w3.org/2001/xml.xsd")
    https = schema_store.local_path("https://www.w3.org/2001/xml.xsd")
    assert http is not None
    assert http == https


def test_transport_serves_vendored_document():
    t = schema_store.VendoredSchemaTransport()
    data = t.load("https://www.onvif.org/ver20/media/wsdl/media.wsdl")
    assert b"Media2Binding" in data


def test_transport_refuses_unvendored_url():
    """A miss must be fatal, not a quiet network fetch.

    A conformance verdict that depended on what a web server served that
    morning would not be reproducible.
    """
    t = schema_store.VendoredSchemaTransport()
    with pytest.raises(schema_store.SchemaNotVendored):
        t.load("https://example.invalid/some/schema.xsd")


@pytest.mark.parametrize("sd", services.SERVICES, ids=lambda s: s.short)
def test_service_builds_offline(sd):
    """Every service in the table builds a zeep client with no network.

    :class:`VendoredSchemaTransport` raises on any document it doesn't
    have, so reaching the end of this proves the vendored closure is
    complete for that service — including ``media2``, which is the whole
    point of issue #1.
    """
    from zeep import Client, Settings

    wsdl = schema_store.local_path(sd.wsdl_url)
    assert wsdl is not None, f"{sd.short}: {sd.wsdl_url} not vendored"

    client = Client(
        wsdl=str(wsdl),
        settings=Settings(strict=False, xml_huge_tree=True),
        transport=schema_store.VendoredSchemaTransport(),
    )
    # Raises if the binding named in the table isn't in the WSDL.
    client.create_service(sd.binding_name, "http://example.invalid/service")


def test_media2_exposes_the_operations_the_tests_call():
    """The ver20 types that python-onvif-zeep's onvif.xsd 2.4.2 lacked.

    ``tt:StringList`` was the concrete blocker: vendoring the ver20 WSDL
    alone failed to build because the bundled schema predated it.
    """
    from zeep import Client, Settings

    sd = services.get("media2")
    client = Client(
        wsdl=str(schema_store.local_path(sd.wsdl_url)),
        settings=Settings(strict=False, xml_huge_tree=True),
        transport=schema_store.VendoredSchemaTransport(),
    )
    ops = set(client.wsdl.bindings[sd.binding_name]._operations)
    assert {
        "GetProfiles", "GetStreamUri", "GetVideoSourceConfigurations",
        "GetVideoSourceConfigurationOptions", "GetVideoEncoderConfigurations",
        "GetVideoEncoderConfigurationOptions",
    } <= ops

    for type_name in ("StringList", "VideoEncoder2Configuration",
                      "VideoEncoder2ConfigurationOptions"):
        client.get_type(f"{{http://www.onvif.org/ver10/schema}}{type_name}")


def test_create_type_prefix_assumption_holds():
    """``ONVIFService.create_type`` hardcodes the ``ns0:`` prefix.

    Cases build request objects with ``svc.create_type("GetStreamUri")``,
    which python-onvif-zeep resolves as ``ns0:GetStreamUri``. That only
    works while zeep maps ``ns0`` to the WSDL's own target namespace. If a
    schema refresh ever reorders the imports, this catches it here rather
    than as a confusing LookupError against a device.
    """
    from zeep import Client, Settings

    for short in ("devicemgmt", "media", "media2", "imaging", "ptz"):
        sd = services.get(short)
        client = Client(
            wsdl=str(schema_store.local_path(sd.wsdl_url)),
            settings=Settings(strict=False, xml_huge_tree=True),
            transport=schema_store.VendoredSchemaTransport(),
        )
        assert client.wsdl.types.prefix_map.get("ns0") == sd.namespace, short
