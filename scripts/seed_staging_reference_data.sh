#!/usr/bin/env bash
# One-time staging seed: copies ONLY the four reference/catalog tables listed below from the
# existing (production) database into a new staging database. Read-only against the source --
# every source operation is `pg_dump`, never a write. Never copies OrderHistory, customer
# profiles, conversations, recommendations, Shopify sessions, or customer tokens.
#
# Prisma owns this schema (see prisma/migrations/) -- this script copies DATA ONLY, never DDL.
# It assumes the staging database already has the schema applied via
# `npx prisma migrate deploy` and does not create, alter, or drop anything.
#
# Usage:
#   PROD_DATABASE_URL=postgresql://...    (read-only use in this script)
#   STAGING_DATABASE_URL=postgresql://... (write target)
#   ./scripts/seed_staging_reference_data.sh
#
# Both variables must already be set in your shell environment -- this script never accepts a
# connection string as a command-line argument (arguments are visible in shell history / process
# listings; env vars set in your own shell session are not passed on the command line here).
# Nothing below ever echoes $PROD_DATABASE_URL or $STAGING_DATABASE_URL. If pg_dump/psql itself
# ever prints a connection string in an error message, redact it before sharing that output.

set -euo pipefail

: "${PROD_DATABASE_URL:?Set PROD_DATABASE_URL in your shell first (not as a script argument)}"
: "${STAGING_DATABASE_URL:?Set STAGING_DATABASE_URL in your shell first (not as a script argument)}"

# Import order: FragranceProduct first (OdooOilMapping.fragranceProductId references it
# *logically* -- there is no enforced Postgres FK on any of these four tables, confirmed by
# reading every CREATE TABLE statement in prisma/migrations/, but keeping this order makes the
# data meaningful at every intermediate step and costs nothing since there's no constraint to
# fight). ExistingCombination and ProductRegionSummary have no dependency on the others.
TABLES=(FragranceProduct OdooOilMapping ExistingCombination ProductRegionSummary)

count() {
  # $1=connection url  $2=table name -- read-only SELECT COUNT(*)
  psql "$1" -Atqc "SELECT COUNT(*) FROM \"$2\";"
}

echo "== Row counts BEFORE (source vs. staging) =="
declare -A BEFORE_SRC BEFORE_DST
for t in "${TABLES[@]}"; do
  BEFORE_SRC[$t]=$(count "$PROD_DATABASE_URL" "$t")
  BEFORE_DST[$t]=$(count "$STAGING_DATABASE_URL" "$t")
  printf '  %-24s source=%-8s staging=%-8s\n' "$t" "${BEFORE_SRC[$t]}" "${BEFORE_DST[$t]}"
  if [ "${BEFORE_DST[$t]}" -ne 0 ]; then
    echo "REFUSING TO CONTINUE: staging.\"$t\" already has ${BEFORE_DST[$t]} row(s)."
    echo "This script never truncates or overwrites automatically. If you want a clean re-seed,"
    echo "decide explicitly and run:  psql \"\$STAGING_DATABASE_URL\" -c 'TRUNCATE \"$t\";'"
    echo "then re-run this script. Exiting without touching anything."
    exit 1
  fi
done

echo
echo "== Copying (pg_dump --data-only from source | psql into staging), one table per transaction =="
for t in "${TABLES[@]}"; do
  echo "  -> $t"
  pg_dump "$PROD_DATABASE_URL" \
    --data-only \
    --table="\"$t\"" \
    --column-inserts \
    --rows-per-insert=500 \
    | psql "$STAGING_DATABASE_URL" -v ON_ERROR_STOP=1 -q
done

echo
echo "== Row counts AFTER =="
FAILED=0
for t in "${TABLES[@]}"; do
  after=$(count "$STAGING_DATABASE_URL" "$t")
  src="${BEFORE_SRC[$t]}"
  status="OK"
  if [ "$after" -ne "$src" ]; then status="MISMATCH"; FAILED=1; fi
  printf '  %-24s source=%-8s staging_after=%-8s %s\n' "$t" "$src" "$after" "$status"
done

if [ "$FAILED" -ne 0 ]; then
  echo "One or more tables did not match source row counts -- investigate before using this data."
  exit 1
fi
echo "All four tables copied with matching row counts. Source database was never written to."
