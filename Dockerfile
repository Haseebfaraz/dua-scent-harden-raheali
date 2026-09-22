# Production image (Phase 7). Python 3.12, dependencies installed ONLY from the hash-locked
# requirements.txt (never re-resolved at build time), non-root user, read-only application code,
# no access log (capability tokens travel in query strings on the App Proxy).
FROM python:3.12-slim AS build

WORKDIR /build
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir --require-hashes -r requirements.txt

COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir --no-deps .

FROM python:3.12-slim

RUN groupadd --system app && useradd --system --gid app --create-home app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# migrations/ is copied for OPERATOR use (`psql -f`) only; nothing in the image applies them.
WORKDIR /app
COPY --chown=root:root migrations ./migrations
USER app

ENV PORT=8000 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000

# Proxy headers are NOT trusted by uvicorn; the application reads the client address itself via
# TRUSTED_PROXY_HOPS (app/api/client_identity.py). --no-access-log: see above.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log --no-server-header"]
