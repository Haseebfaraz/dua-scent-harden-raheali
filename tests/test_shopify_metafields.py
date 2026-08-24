import json

from app.shopify.metafields import (
    build_customer_identity_metafields,
    build_internal_components_metafield,
    build_note_composition_metafield,
)


def test_note_composition_metafield_shape():
    mf = build_note_composition_metafield("rec_1", "HYBRID", [{"position": "top", "quantityMl": 5.0}])
    assert mf["namespace"] == "custom"
    assert mf["key"] == "note_composition"
    assert mf["type"] == "json"
    assert json.loads(mf["value"]) == {"recommendationId": "rec_1", "combinationType": "HYBRID", "layers": [{"position": "top", "quantityMl": 5.0}]}


def test_internal_components_metafield_keeps_only_title_and_contribution():
    mf = build_internal_components_metafield([{"title": "Rose Oud", "contribution": "anchor", "notes": ["rose"]}])
    assert json.loads(mf["value"]) == [{"title": "Rose Oud", "contribution": "anchor"}]


def test_internal_components_metafield_handles_empty_list():
    mf = build_internal_components_metafield([])
    assert json.loads(mf["value"]) == []


def test_customer_identity_metafields_default_to_empty_string():
    fields = build_customer_identity_metafields(None, None)
    values = {f["key"]: f["value"] for f in fields}
    assert values == {"customer_name": "", "customer_email": ""}


def test_customer_identity_metafields_use_provided_values():
    fields = build_customer_identity_metafields("Jane Doe", "jane@example.com")
    values = {f["key"]: f["value"] for f in fields}
    assert values == {"customer_name": "Jane Doe", "customer_email": "jane@example.com"}
