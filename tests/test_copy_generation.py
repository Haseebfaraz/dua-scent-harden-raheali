import json

import httpx
import pytest

from app.services import copy_generation as cg


def _mock_ok_response(content: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})


def _make_item(**overrides) -> dict:
    proposal = {
        "confidence": "medium",
        "evidenceScope": "limited",
        "customerFacingDescription": "FALLBACK_DESC",
        "customerFacingWhySuits": "FALLBACK_WHY",
        "canonicalKey": "fallback-key",
        **overrides,
    }
    return {"proposal": proposal, "notesByRole": {"Freshness": ["Bergamot", "Lime"]}}


PROFILE_FIELDS = {"likes": ["Fresh"], "dislikes": []}


@pytest.mark.asyncio
async def test_rejects_response_with_real_catalog_title_no_retry(monkeypatch):
    calls = 0

    async def _post(url, json_payload, headers):
        nonlocal calls
        calls += 1
        return _mock_ok_response({"description": "A blend built around Secret Real Product", "whySuits": "Great for you"})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, ["secret real product"])

    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"
    assert item["proposal"]["customerFacingWhySuits"] == "FALLBACK_WHY"
    assert calls == 1


@pytest.mark.asyncio
async def test_rejects_response_that_names_the_brand(monkeypatch):
    calls = 0

    async def _post(url, json_payload, headers):
        nonlocal calls
        calls += 1
        return _mock_ok_response({"description": "A signature DUA blend for evenings", "whySuits": "Great for you"})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])

    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"
    assert item["proposal"]["customerFacingWhySuits"] == "FALLBACK_WHY"
    assert calls == 1  # brand-name leak is treated as a hard no-retry failure, like a catalog title


@pytest.mark.asyncio
async def test_rejects_internal_id_pattern(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "cabc123456789012345678xyz is lovely", "whySuits": "Great for you"})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"
    assert item["proposal"]["customerFacingWhySuits"] == "FALLBACK_WHY"


@pytest.mark.asyncio
async def test_rejects_unearned_exact_note_no_retry(monkeypatch):
    calls = 0

    async def _post(url, json_payload, headers):
        nonlocal calls
        calls += 1
        return _mock_ok_response({"description": "A blend built for apple lovers", "whySuits": "Your love for apple shines through here."})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item(missingExactNotes=["apple"])
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])

    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"
    assert item["proposal"]["customerFacingWhySuits"] == "FALLBACK_WHY"
    assert calls == 1


@pytest.mark.asyncio
async def test_never_flags_copy_that_never_mentions_missing_note(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "Bright citrus lift", "whySuits": "Matches your love of fresh scents."})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item(missingExactNotes=["apple"])
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "Bright citrus lift"


@pytest.mark.asyncio
async def test_never_false_positives_pineapple_for_apple(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "A pineapple-forward tropical blend", "whySuits": "Bright and juicy."})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item(missingExactNotes=["apple"])
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "A pineapple-forward tropical blend"


@pytest.mark.asyncio
async def test_rejects_unmatched_family_then_accepts_clean_retry(monkeypatch):
    call_count = 0

    async def _post(url, json_payload, headers):
        nonlocal call_count
        call_count += 1
        is_retry = "RETRY" in json_payload["messages"][0]["content"]
        if not is_retry:
            return _mock_ok_response({"description": "A floral-forward blend", "whySuits": "Designed around your preference for floral scents."})
        return _mock_ok_response({"description": "Bright and crisp", "whySuits": "Matches your love of fresh scents."})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item(matchedPreferenceFamilies=["fresh"], missingPreferenceFamilies=["floral"])
    await cg.apply_customer_facing_copy([item], {"likes": ["Fresh", "Floral"], "dislikes": []}, [])

    assert call_count == 2
    assert item["proposal"]["customerFacingWhySuits"] == "Matches your love of fresh scents."


@pytest.mark.asyncio
async def test_falls_back_when_retry_also_claims_missing_family(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "A floral-forward blend", "whySuits": "Designed around your preference for floral scents."})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item(matchedPreferenceFamilies=[], missingPreferenceFamilies=["floral"])
    await cg.apply_customer_facing_copy([item], {"likes": ["Floral"], "dislikes": []}, [])

    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"
    assert item["proposal"]["customerFacingWhySuits"] == "FALLBACK_WHY"


@pytest.mark.asyncio
async def test_never_flags_legitimate_matched_family_copy(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "Bright citrus lift", "whySuits": "Matches your love of fresh scents."})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item(matchedPreferenceFamilies=["fresh"], missingPreferenceFamilies=["floral"])
    await cg.apply_customer_facing_copy([item], {"likes": ["Fresh", "Floral"], "dislikes": []}, [])
    assert item["proposal"]["customerFacingWhySuits"] == "Matches your love of fresh scents."


@pytest.mark.asyncio
async def test_detects_this_opener_retries_once_accepts_clean_retry(monkeypatch):
    call_count = 0

    async def _post(url, json_payload, headers):
        nonlocal call_count
        call_count += 1
        is_retry = "RETRY" in json_payload["messages"][0]["content"]
        if not is_retry:
            return _mock_ok_response({"description": "Rich vanilla warmth", "whySuits": "This blend suits your sweet tooth perfectly."})
        return _mock_ok_response({"description": "Vanilla-forward and cozy", "whySuits": "Your sweet tooth will love this blend."})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])

    assert call_count == 2
    assert item["proposal"]["customerFacingDescription"] == "Vanilla-forward and cozy"
    assert item["proposal"]["customerFacingWhySuits"] == "Your sweet tooth will love this blend."


@pytest.mark.asyncio
async def test_falls_back_when_retry_also_violates_this_rule(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "This one is lovely", "whySuits": "This is perfect for you"})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"
    assert item["proposal"]["customerFacingWhySuits"] == "FALLBACK_WHY"


@pytest.mark.asyncio
async def test_fallback_on_network_error(monkeypatch):
    async def _post(url, json_payload, headers):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"


@pytest.mark.asyncio
async def test_fallback_on_timeout(monkeypatch):
    async def _post(url, json_payload, headers):
        raise httpx.TimeoutException("The operation was aborted")

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"


@pytest.mark.asyncio
async def test_fallback_on_non_ok_response(monkeypatch):
    async def _post(url, json_payload, headers):
        return httpx.Response(500)

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"


@pytest.mark.asyncio
async def test_fallback_on_malformed_json(monkeypatch):
    async def _post(url, json_payload, headers):
        return httpx.Response(200, json={"choices": [{"message": {"content": "not valid json {{{"}}]})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"


@pytest.mark.asyncio
async def test_fallback_on_empty_field(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "", "whySuits": "Something real"})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"


@pytest.mark.asyncio
async def test_fallback_on_oversized_field(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "x" * 300, "whySuits": "Something real"})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"


@pytest.mark.asyncio
async def test_fallback_on_non_string_field(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "Fine", "whySuits": 12345})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "FALLBACK_DESC"


@pytest.mark.asyncio
async def test_differentiated_retry_angles_for_colliding_items(monkeypatch):
    item_a = _make_item(customerFacingDescription="FALLBACK_A", customerFacingWhySuits="FALLBACK_WHY_A")
    item_b = _make_item(customerFacingDescription="FALLBACK_B", customerFacingWhySuits="FALLBACK_WHY_B")
    item_b["notesByRole"] = {"Sweetness": ["Vanilla"]}
    item_c = _make_item(customerFacingDescription="FALLBACK_C", customerFacingWhySuits="FALLBACK_WHY_C")
    item_c["notesByRole"] = {"Floral bridge": ["Jasmine"]}

    retry_system_prompts = []

    async def _post(url, json_payload, headers):
        system_content = json_payload["messages"][0]["content"]
        is_retry = "RETRY" in system_content
        if not is_retry:
            return _mock_ok_response({"description": "Zesty citrus burst", "whySuits": "Zesty and bright, just for you."})
        retry_system_prompts.append(system_content)
        user_payload = json.loads(json_payload["messages"][1]["content"])
        notes_json = json.dumps(user_payload["notesByRole"])
        if "Vanilla" in notes_json:
            return _mock_ok_response({"description": "Warm vanilla comfort", "whySuits": "Built around your cozy side."})
        return _mock_ok_response({"description": "Delicate floral lift", "whySuits": "A gentle match for your taste."})

    monkeypatch.setattr(cg, "_http_post", _post)
    await cg.apply_customer_facing_copy([item_a, item_b, item_c], PROFILE_FIELDS, [])

    assert item_a["proposal"]["customerFacingDescription"] == "Zesty citrus burst"
    assert len(retry_system_prompts) == 2

    import re

    def angle_of(prompt):
        m = re.search(r"use this distinct angle: (.+)", prompt)
        return m.group(1) if m else None

    angle_b = angle_of(retry_system_prompts[0])
    angle_c = angle_of(retry_system_prompts[1])
    assert angle_b
    assert angle_c
    assert angle_b != angle_c
    assert item_b["proposal"]["customerFacingDescription"] == "Warm vanilla comfort"
    assert item_c["proposal"]["customerFacingDescription"] == "Delicate floral lift"


@pytest.mark.asyncio
async def test_happy_path_no_retry(monkeypatch):
    async def _post(url, json_payload, headers):
        return _mock_ok_response({"description": "Bright citrus lift", "whySuits": "Matches your love of fresh scents."})

    monkeypatch.setattr(cg, "_http_post", _post)
    item = _make_item()
    await cg.apply_customer_facing_copy([item], PROFILE_FIELDS, [])
    assert item["proposal"]["customerFacingDescription"] == "Bright citrus lift"
    assert item["proposal"]["customerFacingWhySuits"] == "Matches your love of fresh scents."


@pytest.mark.asyncio
async def test_does_nothing_on_empty_item_list(monkeypatch):
    called = False

    async def _post(url, json_payload, headers):
        nonlocal called
        called = True
        return _mock_ok_response({"description": "x", "whySuits": "y"})

    monkeypatch.setattr(cg, "_http_post", _post)
    await cg.apply_customer_facing_copy([], PROFILE_FIELDS, [])
    assert called is False
