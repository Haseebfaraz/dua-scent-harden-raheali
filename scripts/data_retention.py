"""Retention maintenance command (Phase 6, finding F11).

    python -m scripts.data_retention                 # DRY RUN (default): counts only, zero writes
    python -m scripts.data_retention --execute       # deletes, ONLY if RETENTION_EXECUTION_ENABLED=true

* Dry-run is the default and the only possible mode while RETENTION_EXECUTION_ENABLED is false, so
  a mistyped flag can never delete anything before an operator has reviewed the policy.
* It is a command, not an endpoint: nothing in the web application can trigger it.
* It is NOT scheduled by this repository. Scheduling is an operator decision
  (docs/DATA_RETENTION_AND_DELETION.md section 7).
* Output is a single JSON object of COUNTS. It never prints an id, a name, an email, a message,
  a token or a database URL, including on failure.
* Exit code: 0 on success, 1 if any item failed or the run itself failed, 2 if another retention
  run holds the lock.
"""

import argparse
import asyncio
import json
import sys


async def _main(execute: bool, batch_size: int | None, max_batches: int) -> int:
    from app.db.session import SessionLocal
    from app.services.data_lifecycle import run_retention

    try:
        async with SessionLocal() as session:
            report = await run_retention(session, execute=execute, batch_size=batch_size, max_batches=max_batches)
    except Exception as err:  # noqa: BLE001 -- never print the exception text (it can carry a connection string)
        print(json.dumps({"ok": False, "errorType": type(err).__name__}))
        return 1
    summary = report.as_dict()
    summary["ok"] = report.failed == 0
    summary["executionRequested"] = execute
    print(json.dumps(summary, sort_keys=True))
    if report.held.get("anotherRetentionRunActive"):
        return 2
    return 0 if report.failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the data retention policy. Dry-run unless --execute AND RETENTION_EXECUTION_ENABLED=true.")
    parser.add_argument("--execute", action="store_true", help="really delete (also requires RETENTION_EXECUTION_ENABLED=true)")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--max-batches", type=int, default=50)
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.execute, args.batch_size, max(1, min(args.max_batches, 1000))))


if __name__ == "__main__":
    sys.exit(main())
