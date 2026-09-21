import pytest

from app.config import settings
from app.db.session import SessionLocal

# Phase 1 (security): every Shopify Admin call refuses to run without a configured trusted shop.
# Give the whole suite one fake trusted shop so tests that never cared about the shop keep
# working; tests that exercise the boundary itself override this explicitly via monkeypatch.
TEST_TRUSTED_SHOP = "test-shop.myshopify.com"


# Phase 2: the internal Node-adapter routes refuse requests unless INTERNAL_API_KEY is configured
# (fail closed). Give the suite a fake key so internal-route tests exercise the real check.
TEST_INTERNAL_API_KEY = "fake-internal-api-key-for-tests"


@pytest.fixture(autouse=True)
def _trusted_shop_for_tests(monkeypatch):
    if not settings.shopify_shop_domain:
        monkeypatch.setattr(settings, "shopify_shop_domain", TEST_TRUSTED_SHOP)
    if not settings.internal_api_key:
        monkeypatch.setattr(settings, "internal_api_key", TEST_INTERNAL_API_KEY)
    # Every TestClient shares one network identity, so the production abuse limits would trip
    # across the suite. Tests that exercise the limiter set their own thresholds explicitly.
    for name in ("rate_limit_conversation_create_per_ip", "rate_limit_chat_turn_per_conversation", "rate_limit_chat_turn_per_conversation_daily",
                 "rate_limit_chat_turn_per_ip", "rate_limit_history_read_per_conversation", "rate_limit_history_read_per_ip",
                 "rate_limit_security_denied_per_conversation", "rate_limit_security_denied_per_ip"):
        monkeypatch.setattr(settings, name, "100000/60")


@pytest.fixture
async def db_session():
    async with SessionLocal() as session:
        yield session


@pytest.fixture
def inventory_verified(monkeypatch):
    """Phase 5: for tests whose subject is ANOTHER invariant of the Shopify write layer (pricing,
    product identity, authorization ordering). It answers the commerce inventory gate positively
    and records each call. It is opt-in per module, never autouse; the gate itself is tested
    without any bypass in tests/security/test_commerce_inventory.py."""
    calls = []

    async def _verified(session, *, recommendation, ratios, quantity=1):
        calls.append({"recommendationId": getattr(recommendation, "id", None), "ratios": dict(ratios), "quantity": quantity})

    async def _still(session, decision, *, recommendation, ratios, quantity=1):
        calls.append({"recheck": True})

    monkeypatch.setattr("app.shopify.builds.require_commerce_inventory", _verified)
    monkeypatch.setattr("app.shopify.builds.ensure_still_satisfied", _still)
    return calls


# ---------------------------------------------------------------------------
# Phase 5A: default-deny network boundary for the deterministic suite
# ---------------------------------------------------------------------------
# Phase 5 disclosed that three new tests may have sent unauthenticated GET requests to a real Odoo
# sandbox host (the client reference they used was not mocked and config.py defaulted to a real
# hostname). Guarding one symbol or one hostname is not isolation, so the boundary is the socket
# layer itself, for every client and every import path:
#
#   * connect / connect_ex / sendto / getaddrinfo are intercepted for the whole session;
#   * the ONLY destination allowed is the disposable database named by DATABASE_URL (its exact
#     unix-socket directory, or its exact host:port). "localhost" is not a wildcard;
#   * anything else raises ExternalNetworkBlocked BEFORE the real call, so no packet (not even a
#     DNS query) leaves the machine, and the attempt is recorded;
#   * because application code may swallow the exception (the Odoo client turns any error into
#     "unavailable"), every test is additionally FAILED at teardown if it recorded an attempt,
#     unless it is marked `expects_network_block` (the guard's own negative tests);
#   * in-process test clients and fake httpx transports never open a socket and are unaffected;
#   * live suites stay opt-in: the guard is lifted only for a test marked `live_ai` AND only when
#     ALLOW_LIVE_NETWORK=1 is set explicitly. Nothing in the deterministic run can enable it.

import os as _os
import socket as _socket
from urllib.parse import parse_qs as _parse_qs
from urllib.parse import urlparse as _urlparse


class ExternalNetworkBlocked(RuntimeError):
    """A deterministic test tried to open a network connection or resolve a hostname."""


def _allowed_database_destinations() -> tuple[set[str], set[tuple[str, int]]]:
    url = _os.environ.get("DATABASE_URL", "")
    parsed = _urlparse(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    unix_dirs: set[str] = set()
    tcp: set[tuple[str, int]] = set()
    query_host = (_parse_qs(parsed.query).get("host") or [None])[0]
    if query_host and query_host.startswith("/"):
        unix_dirs.add(_os.path.realpath(query_host))
    elif parsed.hostname:
        tcp.add((parsed.hostname, parsed.port or 5432))
    return unix_dirs, tcp


class _NetworkGuard:
    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.real_calls = 0  # how many times a REAL connect/getaddrinfo/sendto was let through
        self.enabled = True
        self.unix_dirs, self.tcp = _allowed_database_destinations()

    def allowed(self, family: int, address) -> bool:
        if family == getattr(_socket, "AF_UNIX", object()):
            path = address.decode() if isinstance(address, bytes) else str(address)
            return _os.path.realpath(_os.path.dirname(path)) in self.unix_dirs
        if isinstance(address, tuple) and len(address) >= 2:
            return (str(address[0]), int(address[1])) in self.tcp
        return False

    def check(self, family: int, address, what: str) -> None:
        if not self.enabled or self.allowed(family, address):
            self.real_calls += 1
            return
        self.attempts.append(f"{what} {address!r}")
        raise ExternalNetworkBlocked(f"deterministic tests may not use the network: {what} {address!r}")


_GUARD = _NetworkGuard()
_REAL = {"connect": _socket.socket.connect, "connect_ex": _socket.socket.connect_ex, "sendto": _socket.socket.sendto, "getaddrinfo": _socket.getaddrinfo}


def _guarded_connect(self, address):
    _GUARD.check(self.family, address, "connect")
    return _REAL["connect"](self, address)


def _guarded_connect_ex(self, address):
    _GUARD.check(self.family, address, "connect_ex")
    return _REAL["connect_ex"](self, address)


def _guarded_sendto(self, data, *args):
    _GUARD.check(self.family, args[-1], "sendto")
    return _REAL["sendto"](self, data, *args)


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    # Resolving a name is itself network traffic. Only the database host may be resolved.
    allowed_hosts = {h for h, _ in _GUARD.tcp}
    if _GUARD.enabled and host not in allowed_hosts:
        _GUARD.attempts.append(f"getaddrinfo {host!r}")
        raise ExternalNetworkBlocked(f"deterministic tests may not resolve hostnames: {host!r}")
    _GUARD.real_calls += 1
    return _REAL["getaddrinfo"](host, port, *args, **kwargs)


_socket.socket.connect = _guarded_connect
_socket.socket.connect_ex = _guarded_connect_ex
_socket.socket.sendto = _guarded_sendto
_socket.getaddrinfo = _guarded_getaddrinfo


@pytest.fixture
def network_guard():
    return _GUARD


@pytest.fixture(autouse=True)
def _default_deny_network(request):
    live = request.node.get_closest_marker("live_ai") is not None and _os.environ.get("ALLOW_LIVE_NETWORK") == "1"
    _GUARD.enabled = not live
    before = len(_GUARD.attempts)
    yield
    _GUARD.enabled = True
    attempts = _GUARD.attempts[before:]
    if attempts and request.node.get_closest_marker("expects_network_block") is None:
        pytest.fail(f"this test attempted network access that was blocked (and possibly swallowed by application code): {attempts}", pytrace=False)
