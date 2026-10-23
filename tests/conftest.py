import pytest
from fastmcp import Client

from studyspot import service
from studyspot.server import mcp

NOW = "2026-10-22T10:00:00+03:00"   # Thursday morning, Beirut
TOMORROW = "2026-10-23"
USER = "amir.d"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Fresh database, pinned clock and a signed-in user for every test."""
    db = tmp_path / "studyspot.db"
    monkeypatch.setenv("STUDYSPOT_DB", str(db))
    monkeypatch.setenv("STUDYSPOT_NOW", NOW)
    monkeypatch.setenv("STUDYSPOT_USER", USER)
    service.init_db()
    return db


@pytest.fixture
async def client():
    """In-memory MCP client: real protocol, schema validation and error mapping, no subprocess."""
    async with Client(mcp) as c:
        yield c


def booking_count() -> int:
    with service.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]


async def call(client, tool, **args):
    """Call a tool without raising; returns the CallToolResult."""
    return await client.call_tool(tool, args, raise_on_error=False)


def error_text(result) -> str:
    assert result.is_error, "expected a tool error"
    return result.content[0].text
