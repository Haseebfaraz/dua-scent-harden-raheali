from app.shopify import publishing
from app.shopify.publishing import publish_to_all_channels

SHOP = "test-shop.myshopify.com"


async def test_publish_to_all_channels_publishes_to_every_publication(monkeypatch):
    calls = []

    async def _fake(session, shop, query, variables=None):
        calls.append({"query": query, "variables": variables})
        if "getPublications" in query:
            return {"data": {"publications": {"nodes": [{"id": "gid://shopify/Publication/1"}, {"id": "gid://shopify/Publication/2"}]}}}
        return {"data": {"publishablePublish": {"userErrors": []}}}

    monkeypatch.setattr(publishing, "admin_graphql", _fake)
    await publish_to_all_channels(None, SHOP, "gid://shopify/Product/1")

    assert len(calls) == 2
    publish_call = calls[1]
    assert publish_call["variables"]["id"] == "gid://shopify/Product/1"
    assert publish_call["variables"]["input"] == [{"publicationId": "gid://shopify/Publication/1"}, {"publicationId": "gid://shopify/Publication/2"}]


async def test_publish_to_all_channels_skips_publish_call_when_no_publications(monkeypatch):
    calls = []

    async def _fake(session, shop, query, variables=None):
        calls.append(query)
        return {"data": {"publications": {"nodes": []}}}

    monkeypatch.setattr(publishing, "admin_graphql", _fake)
    await publish_to_all_channels(None, SHOP, "gid://shopify/Product/1")

    assert len(calls) == 1
