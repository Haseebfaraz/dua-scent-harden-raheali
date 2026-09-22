"""Phase 7 (F10 / F12): Shopify Admin GraphQL contract and failure regressions.

Every request here is answered by a synthetic response at the transport (`admin_client._post`) or
one level up (`admin_graphql`); no packet leaves (the suite-wide network guard would fail the
test). What these tests prove: the operations this application sends use only inputs and fields
that exist in the selected API version (checked by hand against shopify.dev on 2026-09-21, see
docs/PLATFORM_MODERNIZATION.md), and that every failure shape Shopify can return leads to a
refusal or an ambiguous outcome, never to a false success, a customer-visible raw error, or an
unsafe follow-up write. What they do NOT prove: behaviour against a live store.
"""

import json
import re

import httpx
import pytest

from app.config import settings
from app.shopify import admin_client, builds, products, publishing
from app.shopify.admin_client import ShopifyApiVersionMismatch, ShopifyTransportError, admin_graphql
from app.shopify.products import ShopifyGraphqlError

SHOP = "test-shop.myshopify.com"


class _Db:
    async def scalar(self, *a, **kw):
        return None


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    async def _fake(session, shop):
        return "fake-admin-token", "client_credentials"

    monkeypatch.setattr(admin_client, "get_admin_access_token", _fake)


def _respond(monkeypatch, *, status=200, body=None, text=None, headers=None):
    async def _fake_post(url, token, query, variables):
        request = httpx.Request("POST", url)
        hdrs = {"X-Shopify-API-Version": settings.shopify_api_version, **(headers or {})}
        if text is not None:
            return httpx.Response(status, request=request, text=text, headers=hdrs)
        return httpx.Response(status, request=request, json=body, headers=hdrs)

    monkeypatch.setattr(admin_client, "_post", _fake_post)


# ---------------------------------------------------------------------------
# 1. Version handling
# ---------------------------------------------------------------------------

def test_configured_version_is_a_released_stable_version_not_unstable_or_a_candidate():
    assert re.fullmatch(r"20\d\d-(01|04|07|10)", settings.shopify_api_version), settings.shopify_api_version
    assert settings.shopify_api_version >= "2025-10"  # the oldest version supported on the verification date


async def test_request_targets_the_versioned_endpoint_and_carries_the_token(monkeypatch):
    seen = {}

    async def _fake_post(url, token, query, variables):
        seen.update(url=url, token=token)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {}}, headers={"X-Shopify-API-Version": settings.shopify_api_version})

    monkeypatch.setattr(admin_client, "_post", _fake_post)
    await admin_graphql(_Db(), SHOP, "query { shop { name } }")
    assert seen["url"] == f"https://{SHOP}/admin/api/{settings.shopify_api_version}/graphql.json" and seen["token"] == "fake-admin-token"


async def test_a_different_served_version_is_refused_not_validated(monkeypatch):
    """Shopify answers an unsupported version with the oldest supported one and says so in the
    response header. That is never treated as success for the requested version."""
    _respond(monkeypatch, body={"data": {"productCreate": {"product": {"id": "gid://shopify/Product/1", "handle": "x", "status": "DRAFT"}, "userErrors": []}}}, headers={"X-Shopify-API-Version": "2099-01"})
    with pytest.raises(ShopifyApiVersionMismatch) as err:
        await products.create_product(_Db(), SHOP, title="t", description_html="", vendor="v", template_suffix="s", product_options=[], metafields=[])
    assert err.value.requested == settings.shopify_api_version and err.value.served == "2099-01"
    assert "2099-01" not in str(err.value) and str(err.value) == "api_version_mismatch"


async def test_a_missing_version_header_is_tolerated_but_a_matching_one_passes(monkeypatch):
    async def _no_header(url, token, query, variables):
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {"shop": {"name": "x"}}})

    monkeypatch.setattr(admin_client, "_post", _no_header)
    assert (await admin_graphql(_Db(), SHOP, "query { shop { name } }"))["data"]["shop"]["name"] == "x"


async def test_version_mismatch_after_a_mutation_is_ambiguous_never_a_definitive_rejection():
    assert builds._is_definitive_rejection(ShopifyApiVersionMismatch("a", "b")) is False
    assert builds._is_definitive_rejection(ShopifyTransportError("throttled", throttled=True)) is False
    assert builds._is_definitive_rejection(ShopifyTransportError("graphql_errors")) is False
    assert builds._is_definitive_rejection(ShopifyGraphqlError("product_create_rejected")) is True


# ---------------------------------------------------------------------------
# 2. Transport failure shapes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body, reason", [
    ({"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}, "throttled"),
    ({"errors": [{"message": "Field 'input' doesn't exist on type 'Mutation'"}]}, "graphql_errors"),
    ({"errors": [{"message": "Access denied for productCreate", "extensions": {"code": "ACCESS_DENIED"}}], "data": {"productCreate": None}}, "graphql_errors"),
    ({"something": "else"}, "missing_data"),
    ([], "malformed_json"),
    ("a string", "malformed_json"),
], ids=["throttled", "invalid_query", "access_denied_with_data", "no_data_key", "json_array", "json_string"])
async def test_http_200_with_graphql_errors_or_bad_shape_is_a_transport_error_with_a_safe_reason(monkeypatch, body, reason):
    _respond(monkeypatch, body=body)
    with pytest.raises(ShopifyTransportError) as err:
        await admin_graphql(_Db(), SHOP, "mutation { x }")
    assert err.value.reason == reason and str(err.value) == reason
    assert "Throttled" not in str(err.value) and "Access denied" not in str(err.value)
    assert err.value.throttled is (reason == "throttled")


async def test_non_json_body_is_a_transport_error(monkeypatch):
    _respond(monkeypatch, text="<html>502 Bad Gateway</html>")
    with pytest.raises(ShopifyTransportError) as err:
        await admin_graphql(_Db(), SHOP, "query { shop { name } }")
    assert err.value.reason == "malformed_json"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
async def test_http_errors_still_raise_status_errors(monkeypatch, status):
    _respond(monkeypatch, status=status, body={"errors": "x"})
    with pytest.raises(httpx.HTTPStatusError):
        await admin_graphql(_Db(), SHOP, "query { shop { name } }")


# ---------------------------------------------------------------------------
# 3. Each operation: request shape against the selected version, and result validation
# ---------------------------------------------------------------------------

def _capture(monkeypatch, body):
    calls = []

    async def _fake(session, shop, query, variables=None):
        calls.append({"query": " ".join(query.split()), "variables": variables})
        return body

    monkeypatch.setattr(products, "admin_graphql", _fake)
    monkeypatch.setattr(publishing, "admin_graphql", _fake)
    return calls


async def test_product_create_uses_the_current_input_and_requires_a_draft_result(monkeypatch):
    calls = _capture(monkeypatch, {"data": {"productCreate": {"product": {"id": "gid://shopify/Product/1", "handle": "h", "status": "DRAFT"}, "userErrors": []}}})
    await products.create_product(_Db(), SHOP, title="t", description_html="<p>d</p>", vendor="The Dua Brand", template_suffix="custom-scent",
                                  product_options=[{"name": "Top Note", "values": [{"name": "Rose (50%)"}]}], metafields=[{"namespace": "custom", "key": "k", "type": "json", "value": "{}"}])
    call = calls[0]
    assert "productCreate(product: $product)" in call["query"] and "$product: ProductCreateInput!" in call["query"]
    assert set(call["variables"]) == {"product"} and set(call["variables"]["product"]) == {"title", "descriptionHtml", "vendor", "status", "templateSuffix", "productOptions", "metafields"}
    assert call["variables"]["product"]["status"] == "DRAFT"
    for bad in ({"data": {"productCreate": {"product": {"id": "gid://shopify/Product/1", "handle": "h", "status": "ACTIVE"}, "userErrors": []}}},
                {"data": {"productCreate": {"product": {"id": "gid://shopify/Product/1", "status": "DRAFT"}, "userErrors": []}}},
                {"data": {"productCreate": {"product": None, "userErrors": []}}},
                {"data": {"productCreate": None}}, {"data": None}):
        _capture(monkeypatch, bad)
        with pytest.raises(ShopifyGraphqlError):
            await products.create_product(_Db(), SHOP, title="t", description_html="", vendor="v", template_suffix="s", product_options=[], metafields=[])


async def test_activate_and_rename_use_product_update_input_and_check_errors(monkeypatch):
    calls = _capture(monkeypatch, {"data": {"productUpdate": {"product": {"id": "gid://shopify/Product/1", "status": "ACTIVE"}, "userErrors": []}}})
    await products.activate_product(_Db(), SHOP, "gid://shopify/Product/1")
    await products.rename_product(_Db(), SHOP, "gid://shopify/Product/1", " New ")
    for call in calls:
        assert "productUpdate(product: $product)" in call["query"] and "$product: ProductUpdateInput!" in call["query"] and set(call["variables"]) == {"product"}
    assert calls[0]["variables"]["product"] == {"id": "gid://shopify/Product/1", "status": "ACTIVE"} and calls[1]["variables"]["product"] == {"id": "gid://shopify/Product/1", "title": "New"}
    for bad in ({"data": {"productUpdate": {"product": {"id": "x", "status": "DRAFT"}, "userErrors": []}}},
                {"data": {"productUpdate": {"product": None, "userErrors": [{"field": "status", "message": "nope"}]}}}, {"data": {}}):
        _capture(monkeypatch, bad)
        with pytest.raises(ShopifyGraphqlError):
            await products.activate_product(_Db(), SHOP, "gid://shopify/Product/1")
    _capture(monkeypatch, {"data": {"productUpdate": {"userErrors": [{"field": "title", "message": "too long"}]}}})
    with pytest.raises(ShopifyGraphqlError):
        await products.rename_product(_Db(), SHOP, "gid://shopify/Product/1", "x")


async def test_variant_operations_send_only_supported_inputs_and_never_inventory_quantities(monkeypatch):
    calls = _capture(monkeypatch, {"data": {"productVariantsBulkCreate": {"userErrors": [], "productVariants": [{"id": "gid://shopify/ProductVariant/2", "price": "60.00"}]},
                                            "productVariantsBulkUpdate": {"productVariants": [{"id": "gid://shopify/ProductVariant/1", "price": "60.00"}], "userErrors": []}}})
    await products.create_variant(_Db(), SHOP, "gid://shopify/Product/1", "60.00", [{"optionName": "Top Note", "name": "Rose (50%)"}])
    await products.set_variant_price(_Db(), SHOP, "gid://shopify/Product/1", "gid://shopify/ProductVariant/1", "60.00")
    create, update = calls
    assert "productVariantsBulkCreate(productId: $productId, variants: $variants)" in create["query"] and "$variants: [ProductVariantsBulkInput!]!" in create["query"]
    assert set(create["variables"]["variants"][0]) == {"price", "optionValues", "inventoryItem"} and create["variables"]["variants"][0]["inventoryItem"] == {"tracked": False}
    assert "productVariantsBulkUpdate(productId: $productId, variants: $variants)" in update["query"]
    assert set(update["variables"]["variants"][0]) == {"id", "price", "inventoryItem"}
    # The 2026-04 inventory `changeFromQuantity` requirement applies to inventoryQuantities, which this app never sends.
    for call in calls:
        assert "inventoryQuantities" not in json.dumps(call["variables"]) and "quantityAdjustments" not in json.dumps(call["variables"])


async def test_inventory_item_untrack_is_confirmed_by_the_result(monkeypatch):
    calls = _capture(monkeypatch, {"data": {"inventoryItemUpdate": {"inventoryItem": {"id": "gid://shopify/InventoryItem/1", "tracked": False}, "userErrors": []}}})
    await products.set_inventory_item_untracked(_Db(), SHOP, "gid://shopify/InventoryItem/1")
    assert "inventoryItemUpdate(id: $id, input: $input)" in calls[0]["query"] and calls[0]["variables"] == {"id": "gid://shopify/InventoryItem/1", "input": {"tracked": False}}
    for bad in ({"data": {"inventoryItemUpdate": {"inventoryItem": {"id": "x", "tracked": True}, "userErrors": []}}},
                {"data": {"inventoryItemUpdate": {"inventoryItem": None, "userErrors": [{"field": "tracked", "message": "x"}]}}}):
        _capture(monkeypatch, bad)
        with pytest.raises(ShopifyGraphqlError):
            await products.set_inventory_item_untracked(_Db(), SHOP, "gid://shopify/InventoryItem/1")


async def test_publication_uses_publishable_publish_with_publication_ids_and_checks_errors(monkeypatch):
    calls = _capture(monkeypatch, {"data": {"publications": {"nodes": [{"id": "gid://shopify/Publication/1"}, {"id": "gid://shopify/Publication/2"}]}, "publishablePublish": {"userErrors": []}}})
    await publishing.publish_to_all_channels(_Db(), SHOP, "gid://shopify/Product/1")
    assert "publications(first: 25) { nodes { id } }" in calls[0]["query"]
    assert "publishablePublish(id: $id, input: $input)" in calls[1]["query"] and "$input: [PublicationInput!]!" in calls[1]["query"]
    assert calls[1]["variables"] == {"id": "gid://shopify/Product/1", "input": [{"publicationId": "gid://shopify/Publication/1"}, {"publicationId": "gid://shopify/Publication/2"}]}
    _capture(monkeypatch, {"data": {"publications": {"nodes": [{"id": "gid://shopify/Publication/1"}]}, "publishablePublish": {"userErrors": [{"field": "id", "message": "x"}]}}})
    with pytest.raises(ShopifyGraphqlError):
        await publishing.publish_to_all_channels(_Db(), SHOP, "gid://shopify/Product/1")


async def test_reads_tolerate_null_products_without_inventing_results(monkeypatch):
    _capture(monkeypatch, {"data": {"product": None}})
    assert await products.get_default_variant_id(_Db(), SHOP, "gid://shopify/Product/1") is None
    assert await products.get_product_handle(_Db(), SHOP, "gid://shopify/Product/1") is None
    assert await products.get_product_for_pricing(_Db(), SHOP, "gid://shopify/Product/1") is None


# ---------------------------------------------------------------------------
# 4. Failures inside the build flow never become success or an unsafe next write
# ---------------------------------------------------------------------------

def _recommendation():
    from types import SimpleNamespace

    return SimpleNamespace(id="rec_7", conversationId="conv_7", combinationType="HYBRID", productsJson=[{"title": "A", "notes": ["Rose"]}, {"title": "B", "notes": ["Amber"]}],
                           customerProfileJson={"likes": ["Rose"]}, ratiosJson=[{"productTitle": "A", "ratioPercent": 60}, {"productTitle": "B", "ratioPercent": 40}], shopifyProductId=None)


@pytest.fixture
def build_env(monkeypatch, inventory_verified):
    async def _price(*a, **kw):
        return {"top": 20, "middle": 20, "base": 20}

    monkeypatch.setattr(builds, "compute_price_per_5ml_by_position", _price)


@pytest.mark.parametrize("failure, expect_ambiguous", [
    ("throttled_on_create", True), ("version_mismatch_on_create", True), ("user_error_on_create", False),
    ("throttled_on_price", True), ("errors_on_activate", True), ("timeout_on_activate", True),
])
async def test_creation_failures_never_publish_never_report_success_and_never_retry(monkeypatch, build_env, failure, expect_ambiguous):
    calls = []
    RATIOS = {"top": 34, "middle": 33, "base": 33}

    async def _graphql(session, shop, query, variables=None):
        name = " ".join(query.split())
        calls.append(name.split("(")[0].replace("mutation ", "").replace("query ", ""))
        if "productCreate" in name:
            if failure == "throttled_on_create":
                raise ShopifyTransportError("throttled", throttled=True)
            if failure == "version_mismatch_on_create":
                raise ShopifyApiVersionMismatch(settings.shopify_api_version, "2099-01")
            if failure == "user_error_on_create":
                return {"data": {"productCreate": {"product": None, "userErrors": [{"field": "title", "message": "raw shopify text"}]}}}
            return {"data": {"productCreate": {"product": {"id": "gid://shopify/Product/1", "handle": "h", "status": "DRAFT"}, "userErrors": []}}}
        if "productCreateMedia" in name:
            return {"data": {"productCreateMedia": {"mediaUserErrors": []}}}
        if "getVariants" in name:
            return {"data": {"product": {"variants": {"edges": [{"node": {"id": "gid://shopify/ProductVariant/1"}}]}}}}
        if "productVariantsBulkUpdate" in name:
            if failure == "throttled_on_price":
                raise ShopifyTransportError("throttled", throttled=True)
            return {"data": {"productVariantsBulkUpdate": {"productVariants": [{"id": "gid://shopify/ProductVariant/1", "price": variables["variants"][0]["price"]}], "userErrors": []}}}
        if "getProductForPricing" in name:
            return {"data": {"product": {"id": "gid://shopify/Product/1", "variants": {"edges": [{"node": {"id": "gid://shopify/ProductVariant/1", "price": "136.00"}}]}}}}
        if "productUpdate" in name:
            if failure == "errors_on_activate":
                raise ShopifyTransportError("graphql_errors")
            if failure == "timeout_on_activate":
                raise httpx.ReadTimeout("timed out")
            return {"data": {"productUpdate": {"product": {"id": "gid://shopify/Product/1", "status": "ACTIVE"}, "userErrors": []}}}
        if "publications" in name or "publishablePublish" in name:
            return {"data": {"publications": {"nodes": []}}}
        raise AssertionError(name)

    monkeypatch.setattr(products, "admin_graphql", _graphql)
    monkeypatch.setattr(publishing, "admin_graphql", _graphql)
    expected = builds.BuildWriteAmbiguous if expect_ambiguous else ShopifyGraphqlError
    with pytest.raises(expected) as err:
        await builds.create_shopify_build_product(None, SHOP, recommendation=_recommendation(), custom_name="My Blend", ratios=RATIOS, customer_name=None, customer_email=None)
    assert calls.count("createProduct") == 1  # never retried
    assert "publishToAllChannels" not in calls and "getPublications" not in calls
    if "activate" not in failure:
        assert "activateBuildProduct" not in calls
    assert "raw shopify text" not in str(err.value)
    if expect_ambiguous and "create" not in failure:
        assert err.value.product_id == "gid://shopify/Product/1"  # a created draft is recorded for reconciliation
