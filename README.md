# DUA Scent AI Core Backend (Python)

FastAPI service that owns the DUA Scent fragrance blend-builder: customer conversation
orchestration (OpenAI), deterministic candidate analysis and scoring, Hybrid/Tribrid/Quadbrid
combination generation, Odoo inventory feasibility, and recommendation persistence.

This is a from-scratch Python port of the original Node/Remix implementation. The **business
model is unchanged**: customers get custom-built blends from real catalog components, never a
plain "recommend an existing product" flow. See [`docs/MIGRATION_AUDIT.md`](docs/MIGRATION_AUDIT.md)
for the full file-by-file mapping back to the original JS implementation, which remains the
behavioral reference (a separate repository) for anything not obvious from this code alone.

## Architecture

```text
Shopify Chat Widget
        v
Node Shopify adapter (separate repo -- auth, App Proxy, preview page, product creation)
        v
This service (FastAPI)
        v
Conversation Orchestrator -> OpenAI -> fragrance/recommendation/Odoo tools -> Postgres
        v
preview_ready SSE event
        v
Node adapter -> Shopify preview page -> Save Build / Add to Cart -> Shopify product
```

Node calls this service server-to-server (never from a browser), authenticated with a shared
`X-Internal-Api-Key` header. This service and the Node app currently share one Postgres database
(SQLAlchemy/asyncpg here, Prisma there) -- no schema changes were made during the initial port.

```text
app/
├── main.py              FastAPI app, CORS, logging, router wiring
├── config.py             Settings (env-driven, see .env.example)
├── logging_config.py     Structured JSON logging + request-id middleware
├── api/                  HTTP routes (chat, recommendations, health)
├── ai/                   OpenAI client, system prompt, tool schemas/executor, conversation loop
├── fragrance/            Pure deterministic fragrance logic (normalization, scoring, formulas...)
├── services/             Profile/location/catalog/order-history/recommendation/conversation services
├── integrations/         Odoo REST client
├── schemas/              Pydantic request/response shapes
└── db/                   SQLAlchemy session + models (mirrors the existing Postgres schema)
```

## Local setup

Requires Python 3.12+.

```bash
python -m venv .venv
.venv/Scripts/activate   # or: source .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
cp .env.example .env     # then fill in real values -- see below
```

### Environment

Copy `.env.example` to `.env` and fill in real values. `DATABASE_URL` should point at the same
Postgres database the Node app already uses (no separate schema needed). `OPENAI_MODEL` and
`OPENAI_API_KEY` are required; everything else has a sensible default or is optional. Never commit
a real `.env`.

### Trusted shop (required)

`SHOPIFY_SHOP_DOMAIN` must be set to the one installed store, e.g. `your-store.myshopify.com`.
Every Shopify Admin call (client-credentials grant, GraphQL) is refused until it is set, and no
request header, query parameter, or body can ever select a different shop. See
`app/shopify/trusted_shop.py` and `docs/SECURITY_AUDIT.md` (finding F1).

### Database

Three additive migrations are required before running this code against any database, in order:
`migrations/0001_build_capability.sql` (build capabilities that authorize preview reads and
Shopify build mutations -- `docs/SHOPIFY_BUILD_SECURITY_CONTRACT.md`),
`migrations/0002_conversation_capability_and_rate_limits.sql` (conversation session secrets and
the shared rate-limit counters -- `docs/CHAT_SECURITY_CONTRACT.md`) and
`migrations/0003_message_security_classification.sql` (per-message scope/security classification
used to keep blocked customer turns out of model context -- `docs/AI_SECURITY_GATE.md`). Apply
them with `psql -f` against staging first. Without the first two every chat, preview, Save Build,
and Add to Cart flow fails closed; without the third the chat runs but screens every stored
customer turn deterministically on reload.

### Public chat contract

The storefront must bootstrap a conversation (`POST /chat/session` or a first `POST /chat`
without an id), keep the returned `conversationToken`, and send it on every later message and
history read. `INTERNAL_API_KEY` is mandatory for the Node-adapter routes. Rate limits, input
limits, and the trusted-proxy hop count are environment settings (see `.env.example`).

Everything else maps onto the existing schema with SQLAlchemy models that mirror it exactly (see
`app/db/models/__init__.py`). If you're pointing at a fresh/staging
database instead of the shared production one, its schema must already match production before
running the test suite (most tests assert against real catalog data: real product titles, real
`ExistingCombination` rows, etc.).

### Running the service

```bash
uvicorn app.main:app --reload --port 8000
```

`GET /health` returns `{"status": "ok", "service": "dua-scent-ai-python"}`.

### Running tests

```bash
pytest -q
```

Most tests hit the real database (read-only, or self-cleaning writes) rather than mocks, matching
the JS reference implementation's own testing convention. Expect the full suite to take a while
(minutes, not seconds) against a remote database -- see the `NullPool` comment in `app/db/session.py`
for why, and a note on revisiting it with a real connection pool once this runs against a local/CI
database with one long-lived event loop.

## Deployment (Render)

`render.yaml` defines a single web service. Push this repo to GitHub, connect it to Render, and
fill in the `sync: false` environment variables in the Render dashboard (never in the repo). The
service binds to Render's `$PORT` automatically via the start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

A `Dockerfile` is also provided for platform-independent deployment (runs as a non-root user,
same `$PORT` binding).

**Do not point the live Shopify storefront at this service until parity is proven** in
staging/integration testing. Keep the existing Node-only production path available until then.

## Connecting the existing Node/Shopify app

In the Node repo's `.env`, set:

```env
PYTHON_BACKEND_URL=https://<this-service>.onrender.com
INTERNAL_API_KEY=<same value as this service's INTERNAL_API_KEY>
```

`app/routes/chat.jsx` in the Node repo proxies `/chat` requests to this service's
`POST /internal/chat` and `GET /internal/chat/history`, streaming the SSE response straight
through to the storefront widget with no changes to the widget's contract.
