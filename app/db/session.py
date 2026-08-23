from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings


def _to_asyncpg(raw_url: str) -> tuple[str, dict]:
    """asyncpg takes SSL as a connect kwarg, not a `sslmode` query param the way psycopg2 does --
    translate Node's DATABASE_URL (postgresql://...?sslmode=require) into the equivalent asyncpg
    connection so both apps can point at the literal same DATABASE_URL value.
    """
    parts = urlsplit(raw_url)
    query_pairs = dict(parse_qsl(parts.query))
    sslmode = query_pairs.pop("sslmode", None)
    new_query = urlencode(query_pairs)
    scheme = "postgresql+asyncpg" if parts.scheme.startswith("postgresql") else parts.scheme
    new_url = urlunsplit((scheme, parts.netloc, parts.path, new_query, parts.fragment))
    connect_args = {"ssl": True} if sslmode in ("require", "prefer", "verify-ca", "verify-full") else {}
    return new_url, connect_args


_url, _connect_args = _to_asyncpg(settings.database_url)
# NullPool: a real connection pool holds asyncio-bound sockets that outlive the event loop they
# were opened on. pytest-asyncio gives each test function its own loop by default, so a pooled
# connection from test N crashes trying to close itself under test N+1's loop. NullPool opens a
# fresh connection per checkout and tears it down on release -- no cross-loop reuse, no crash.
# Revisit for production (a real pool, one long-lived event loop under uvicorn) if this becomes a
# throughput concern; it is not a correctness issue there.
engine = create_async_engine(_url, connect_args=_connect_args, poolclass=NullPool)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
