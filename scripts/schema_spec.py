"""The expected database schema, read from migrations/*.sql (the single source of truth).

Parses the CREATE TABLE / ADD COLUMN / CREATE INDEX statements this repository's migrations use
(their own fixed style, not arbitrary SQL) into {table: {column: (information_schema data_type,
is_nullable)}} plus the expected index names. Used by scripts/verify_migrations.py (disposable
database) and scripts/check_schema_compat.py (read-only, any database).
"""

import glob
import os
import re

_TYPES = {
    "TEXT": "text", "BOOLEAN": "boolean", "TIMESTAMP(3)": "timestamp without time zone", "BIGINT": "bigint",
    "DOUBLE PRECISION": "double precision", "JSONB": "jsonb", "INTEGER": "integer",
}
_CREATE = re.compile(r'CREATE TABLE IF NOT EXISTS "(\w+)" \((.*?)\n\);', re.S)
_TYPE_PATTERN = r"(TEXT|BOOLEAN|TIMESTAMP\(3\)|BIGINT|DOUBLE PRECISION|JSONB|INTEGER)(?![\w(])"
_COLUMN = re.compile(r'^\s*"(\w+)"\s+' + _TYPE_PATTERN + r"(.*)$")
_ADD = re.compile(r'ALTER TABLE "(\w+)" ADD COLUMN IF NOT EXISTS "(\w+)" ' + _TYPE_PATTERN + r"([^;]*);")
_INDEX = re.compile(r'CREATE (?:UNIQUE )?INDEX IF NOT EXISTS "(\w+)"')

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] if not line.strip().startswith("--") else "" for line in sql.splitlines())


def migration_files() -> list[str]:
    return sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))


def expected_schema(files: list[str] | None = None) -> tuple[dict[str, dict[str, tuple[str, str]]], set[str]]:
    tables: dict[str, dict[str, tuple[str, str]]] = {}
    indexes: set[str] = set()
    for path in files or migration_files():
        sql = _strip_comments(open(path, encoding="utf-8").read())
        for table, body in _CREATE.findall(sql):
            columns = tables.setdefault(table, {})
            for line in body.splitlines():
                match = _COLUMN.match(line.rstrip(","))
                if match:
                    name, sql_type, rest = match.groups()
                    nullable = "NO" if ("NOT NULL" in rest or "PRIMARY KEY" in rest) else "YES"
                    columns[name] = (_TYPES[sql_type], nullable)
        for table, name, sql_type, rest in _ADD.findall(sql):
            tables.setdefault(table, {})[name] = (_TYPES[sql_type], "NO" if "NOT NULL" in rest else "YES")
        indexes.update(_INDEX.findall(sql))
    return tables, indexes


if __name__ == "__main__":
    t, i = expected_schema()
    for name, cols in sorted(t.items()):
        print(name, len(cols))
    print(len(i), "indexes")
