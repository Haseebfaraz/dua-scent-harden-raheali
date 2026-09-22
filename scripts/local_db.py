"""Start/stop an embedded disposable PostgreSQL 16 for local test runs (never for real data)."""

import os
import signal
import sys
import time

STATE = os.path.join(".local-db", "state")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "start"
    os.makedirs(".local-db", exist_ok=True)
    if command == "stop":
        if os.path.exists(STATE):
            pid = int(open(STATE).read().split()[0])
            os.kill(pid, signal.SIGTERM)
            os.remove(STATE)
        return 0
    import pgserver

    server = pgserver.get_server(os.path.abspath(os.path.join(".local-db", "pgdata")))
    open(STATE, "w").write(f"{os.getpid()} {server.get_uri()}")
    print(f"export DATABASE_URL='{server.get_uri()}'")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
