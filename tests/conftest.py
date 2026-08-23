import pytest

from app.db.session import SessionLocal


@pytest.fixture
async def db_session():
    async with SessionLocal() as session:
        yield session
