"""Startup smoke check (Phase 7). Imports the application and builds the ASGI app with fake
settings, proving that import-time configuration validation passes, that no import performs a
network request or a migration, and that every optional integration is in its documented safe
disabled state. Prints nothing but the outcome; never prints a setting value."""

import os
import socket
import sys


def main() -> int:
    for key, value in {"DATABASE_URL": "postgresql://u:p@127.0.0.1:1/startup-check", "OPENAI_API_KEY": "startup-check-placeholder", "OPENAI_MODEL": "startup-check"}.items():
        os.environ.setdefault(key, value)

    attempted = []

    def _blocked(self, address, *a, **kw):
        attempted.append(str(address))
        raise OSError("startup check: network is disabled")

    socket.socket.connect = _blocked  # type: ignore[method-assign]
    from app.config import settings
    from app.main import app

    problems = []
    if not app.routes:
        problems.append("no routes registered")
    if settings.odoo_inventory_url or settings.odoo_ping_url:
        problems.append("Odoo destination set by default")
    if settings.shared_data_deletion_reviewed or settings.retention_execution_enabled:
        problems.append("a destructive gate is on by default")
    if settings.trusted_proxy_hops != 0:
        problems.append("proxy hops trusted by default")
    if not settings.shopify_api_version[:4].isdigit():
        problems.append("shopify api version is not a stable release")
    if attempted:
        problems.append(f"import attempted {len(attempted)} network connection(s)")
    for problem in problems:
        print("FAIL", problem)
    print("startup check ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
