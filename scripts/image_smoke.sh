#!/usr/bin/env bash
# Phase 9 (B8): build the ACTUAL images from the committed Dockerfile and run the serving image
# against a disposable PostgreSQL on an INTERNAL docker network (no route to Shopify, Odoo, the
# model provider or geocoding), with placeholder settings and every destructive feature disabled.
# Used identically by `make image-check` and the `image` job in .github/workflows/ci.yml.
#
# Requires: docker with BuildKit. Creates only resources prefixed with $PREFIX and removes them.
# Publishes nothing, deploys nothing, needs no application credential.
set -euo pipefail

PREFIX="${IMAGE_SMOKE_PREFIX:-duasmoke}"
TAG="${IMAGE_SMOKE_TAG:-dua-scent-ai:smoke}"
OPS_TAG="${IMAGE_SMOKE_OPS_TAG:-dua-scent-ai:smoke-ops}"
NET="${PREFIX}-int"
PG="${PREFIX}-pg"
APP="${PREFIX}-app"
PG_PASSWORD="disposable-not-a-secret"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

cleanup() {
  docker rm -f "$APP" "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "== build (serving stage) from the locked requirements"
DOCKER_BUILDKIT=1 docker build --quiet -t "$TAG" "$HERE" >/dev/null
echo "== build (ops stage)"
DOCKER_BUILDKIT=1 docker build --quiet --target ops -t "$OPS_TAG" "$HERE" >/dev/null

echo "== the blueprint selects this very build (B17), checked with the image's own interpreter"
docker run --rm --network none -v "$HERE:/src:ro" "$TAG" python /src/scripts/check_deploy_consistency.py /src

echo "== image facts"
docker image inspect "$TAG" --format 'id={{.Id}} user={{.Config.User}} ports={{.Config.ExposedPorts}} cmd={{json .Config.Cmd}}'
docker run --rm --network none "$TAG" python --version
test "$(docker run --rm --network none "$TAG" id -u)" != "0"
# dependency lock agreement: every pinned name==version in requirements.txt is installed (names compared case-insensitively, _ and - alike)
docker run --rm --network none "$TAG" python -m pip list --format=freeze 2>/dev/null | tr 'A-Z_' 'a-z-' | sort > /tmp/${PREFIX}-image.txt
grep -E '^[A-Za-z0-9_.-]+==' "$HERE/requirements.txt" | sed 's/ .*//' | tr 'A-Z_' 'a-z-' | sort > /tmp/${PREFIX}-lock.txt
if ! comm -23 /tmp/${PREFIX}-lock.txt /tmp/${PREFIX}-image.txt | grep -q .; then echo "lock: every pin present"; else echo "lock pins missing from image:"; comm -23 /tmp/${PREFIX}-lock.txt /tmp/${PREFIX}-image.txt; exit 1; fi
# nothing but the runtime enters the image
if docker run --rm --network none "$TAG" sh -c 'find / -xdev \( -name ".env*" -o -name "*.env" -o -name ".git" -o -name "tests" -o -name "*.sqlite*" -o -name ".local-db" -o -name "*.pem" -o -name "*.key" \) -not -path "/proc/*" -not -path "/etc/ssl/*" -not -path "/usr/lib/ssl/*" -not -path "/usr/local/lib/python3.12/*" -not -path "/usr/lib/python3*" 2>/dev/null | grep .'; then echo "unexpected files in image"; exit 1; fi
test "$(docker image inspect "$TAG" --format '{{json .Config.Cmd}}' | grep -c uvicorn)" = "1"  # the default build target is the serving stage
test "$(docker run --rm --network none "$TAG" sh -c 'ls /app/scripts 2>/dev/null | wc -l')" = "0"
echo "content: no env files, git data, tests, local databases or scripts in the serving image"
# missing required configuration fails at import, naming only the field
if docker run --rm --network none "$TAG" python -c "import app.main" >/dev/null 2>/tmp/${PREFIX}-noconfig.txt; then echo "started without required configuration"; exit 1; fi
grep -q "database_url" /tmp/${PREFIX}-noconfig.txt && echo "missing configuration: refused at import"

echo "== disposable PostgreSQL on an internal network"
docker network create --internal "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" -e POSTGRES_USER=dua -e "POSTGRES_PASSWORD=$PG_PASSWORD" -e POSTGRES_DB=smoke postgres:16 >/dev/null
for _ in $(seq 1 60); do docker exec "$PG" pg_isready -U dua -d smoke >/dev/null 2>&1 && break; sleep 1; done
docker exec "$PG" pg_isready -U dua -d smoke

DB_URL="postgresql://dua:${PG_PASSWORD}@${PG}:5432/smoke"
FAKE_ENV=(-e "DATABASE_URL=$DB_URL" -e OPENAI_API_KEY=smoke-placeholder-not-a-key -e OPENAI_MODEL=smoke-placeholder
  -e SHOPIFY_SHOP_DOMAIN=smoke-placeholder.myshopify.com -e SHOPIFY_API_SECRET=smoke-placeholder-not-a-secret -e INTERNAL_API_KEY=smoke-placeholder-internal-key
  -e SHARED_DATA_DELETION_REVIEWED=false -e RETENTION_EXECUTION_ENABLED=false
  -e ODOO_INVENTORY_URL= -e ODOO_INVENTORY_LOCATION_SCOPE= -e ODOO_INVENTORY_QUANTITY_SEMANTICS= -e ALLOWED_ORIGINS=)

start_app() {
  docker rm -f "$APP" >/dev/null 2>&1 || true
  docker run -d --name "$APP" --network "$NET" "${FAKE_ENV[@]}" "$TAG" >/dev/null
  for _ in $(seq 1 30); do docker exec "$APP" python -c 'import socket; socket.create_connection(("127.0.0.1", 8000), 1)' >/dev/null 2>&1 && break; sleep 1; done
  docker cp "$HERE/scripts/image_probe.py" "$APP:/tmp/image_probe.py"
}

echo "== serving image against the EMPTY database"
start_app
docker exec "$APP" python /tmp/image_probe.py health
docker exec "$APP" python /tmp/image_probe.py empty-database

echo "== migrations from the ops artifact"
# 1. synthetic base schema + 0001..0004 applied twice and verified (the script only accepts a
#    LOCAL database, so it runs inside the database container's network namespace)
LOCAL_DB_URL="postgresql://dua:${PG_PASSWORD}@127.0.0.1:5432/smoke"
docker run --rm --network "container:$PG" -e "DATABASE_URL=$LOCAL_DB_URL" -e OPENAI_API_KEY=x -e OPENAI_MODEL=x "$OPS_TAG" python -m scripts.verify_migrations
# 2. the documented operator path: psql -f from the artifact, in order, idempotent
docker run --rm --network "$NET" "$OPS_TAG" psql --version
for f in 0001_build_capability 0002_conversation_capability_and_rate_limits 0003_message_security_classification 0004_conversation_deletion; do
  docker run --rm --network "$NET" "$OPS_TAG" psql "$DB_URL" -q -v ON_ERROR_STOP=1 -f "/app/migrations/$f.sql"
done
echo "psql -f: 0001..0004 re-applied without error"
# 3. retention job from the artifact: dry run, execution disabled
docker run --rm --network "$NET" -e "DATABASE_URL=$DB_URL" -e OPENAI_API_KEY=x -e OPENAI_MODEL=x -e RETENTION_EXECUTION_ENABLED=false "$OPS_TAG" python -m scripts.data_retention | tail -1

echo "== serving image against the migrated database, model provider unreachable"
start_app
docker exec "$APP" python /tmp/image_probe.py health
docker exec "$APP" python /tmp/image_probe.py routes
docker exec "$APP" python /tmp/image_probe.py outbound-blocked
if docker logs "$APP" 2>&1 | grep -qi "traceback\|placeholder-not-a-key\|placeholder-not-a-secret\|$PG_PASSWORD"; then echo "unsafe log output"; exit 1; fi
echo "logs: no traceback, no credential value"

echo "== graceful shutdown (SIGTERM must reach uvicorn)"
docker stop -t 15 "$APP" >/dev/null
EXIT_CODE="$(docker inspect "$APP" --format '{{.State.ExitCode}}')"
echo "exit code after SIGTERM: $EXIT_CODE"
test "$EXIT_CODE" = "0"

echo "== IMAGE SMOKE PASSED ($TAG, $OPS_TAG)"
