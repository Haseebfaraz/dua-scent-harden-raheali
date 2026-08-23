"""ID generation for rows Python creates directly. Prisma's `@default(cuid())` is computed
client-side by the Prisma Client library, not by Postgres -- there is no DB-level default to reuse,
so Python needs its own opaque unique-string generator. Format doesn't matter (nothing parses it
as a cuid); only opacity and uniqueness do.
"""

import uuid


def new_id() -> str:
    return uuid.uuid4().hex
