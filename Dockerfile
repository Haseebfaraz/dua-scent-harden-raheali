# Production image (Phase 7). Python 3.12, dependencies installed ONLY from the hash-locked
# requirements.txt (never re-resolved at build time), non-root user, read-only application code,
# no access log (capability tokens travel in query strings on the App Proxy).
FROM python:3.12.14-slim AS build

WORKDIR /build
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir --require-hashes -r requirements.txt

COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir --no-deps .

FROM python:3.12.14-slim AS runtime-base

RUN groupadd --system app && useradd --system --gid app --create-home app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# migrations/ is copied for OPERATOR use (`psql -f`) only; nothing in the image applies them.
WORKDIR /app
COPY --chown=root:root migrations ./migrations
USER app

ENV PORT=8000 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000

# ---------------------------------------------------------------------------
# Operator image (Phase 9): the SAME runtime plus the two operational entry points and psql.
# Build with `docker build --target ops -t dua-scent-ai:ops .`. It is not the serving image and
# publishes nothing; it exists so migrations and the retention job can run from a supplied,
# reproducible artifact instead of a developer checkout:
#   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f /app/migrations/0001_build_capability.sql   (0001..0004, in order)
#   python -m scripts.data_retention            (dry run; --execute also needs RETENTION_EXECUTION_ENABLED=true)
#   python -m scripts.verify_migrations         (DISPOSABLE databases only: it drops and recreates tables)
# ---------------------------------------------------------------------------
FROM runtime-base AS ops
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*
COPY --chown=root:root scripts/__init__.py scripts/data_retention.py scripts/verify_migrations.py ./scripts/
USER app
CMD ["python", "-m", "scripts.data_retention"]

# ---------------------------------------------------------------------------
# Serving image: LAST on purpose, so a plain `docker build .` (a hosting platform's default) yields
# this stage and never the operator stage.
# ---------------------------------------------------------------------------
FROM runtime-base AS serve
# Proxy headers are NOT trusted by uvicorn; the application reads the client address itself via
# TRUSTED_PROXY_HOPS (app/api/client_identity.py). --no-access-log: see above.
# `exec` makes uvicorn PID 1 so SIGTERM reaches it and shutdown is graceful (Phase 9: without it
# `sh` swallowed the signal and every stop ended in SIGKILL after the grace period).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log --no-server-header"]
