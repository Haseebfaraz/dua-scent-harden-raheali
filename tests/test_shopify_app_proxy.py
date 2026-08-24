import hashlib
import hmac as _hmac
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
from app.shopify.app_proxy import verified_shop

SECRET = "app-proxy-test-secret"


def _make_request(query_params: dict) -> Request:
    scope = {"type": "http", "method": "GET", "query_string": urlencode(query_params).encode(), "headers": []}
    return Request(scope)


def _sign(params: dict, secret: str) -> str:
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return _hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def test_verified_shop_returns_shop_for_a_valid_signature(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    params = {"shop": "real-shop.myshopify.com", "timestamp": "1"}
    signature = _sign(params, SECRET)
    request = _make_request({**params, "signature": signature})
    assert verified_shop(request) == "real-shop.myshopify.com"


def test_verified_shop_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    request = _make_request({"shop": "real-shop.myshopify.com", "timestamp": "1", "signature": "wrong"})
    with pytest.raises(HTTPException) as exc_info:
        verified_shop(request)
    assert exc_info.value.status_code == 400


def test_verified_shop_rejects_missing_shop_param(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    params = {"timestamp": "1"}
    signature = _sign(params, SECRET)
    request = _make_request({**params, "signature": signature})
    with pytest.raises(HTTPException) as exc_info:
        verified_shop(request)
    assert exc_info.value.status_code == 400
