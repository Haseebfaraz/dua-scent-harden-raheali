import base64
import hashlib
import hmac as _hmac

from app.shopify.hmac import verify_app_proxy_signature, verify_webhook_hmac

SECRET = "test-secret"


def test_webhook_hmac_accepts_correctly_signed_body():
    body = b'{"id": 1}'
    digest = base64.b64encode(_hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    assert verify_webhook_hmac(body, digest, SECRET) is True


def test_webhook_hmac_rejects_wrong_signature():
    assert verify_webhook_hmac(b'{"id": 1}', "not-the-real-signature", SECRET) is False


def test_webhook_hmac_rejects_missing_header_or_secret():
    body = b'{"id": 1}'
    assert verify_webhook_hmac(body, None, SECRET) is False
    assert verify_webhook_hmac(body, "anything", "") is False


def test_webhook_hmac_rejects_tampered_body():
    body = b'{"id": 1}'
    digest = base64.b64encode(_hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    assert verify_webhook_hmac(b'{"id": 2}', digest, SECRET) is False


def _sign_app_proxy_params(params: dict, secret: str) -> str:
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return _hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def test_app_proxy_signature_accepts_correctly_signed_query():
    params = {"shop": "test-shop.myshopify.com", "timestamp": "12345", "recommendationId": "rec_1"}
    signature = _sign_app_proxy_params(params, SECRET)
    assert verify_app_proxy_signature({**params, "signature": signature}, SECRET) is True


def test_app_proxy_signature_rejects_wrong_signature():
    params = {"shop": "test-shop.myshopify.com", "timestamp": "12345", "signature": "deadbeef"}
    assert verify_app_proxy_signature(params, SECRET) is False


def test_app_proxy_signature_rejects_tampered_param():
    params = {"shop": "test-shop.myshopify.com", "timestamp": "12345"}
    signature = _sign_app_proxy_params(params, SECRET)
    tampered = {**params, "shop": "attacker-shop.myshopify.com", "signature": signature}
    assert verify_app_proxy_signature(tampered, SECRET) is False


def test_app_proxy_signature_rejects_missing_signature_or_secret():
    assert verify_app_proxy_signature({"shop": "x"}, SECRET) is False
    assert verify_app_proxy_signature({"shop": "x", "signature": "abc"}, "") is False
