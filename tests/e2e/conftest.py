"""E2E test fixtures.

Uses real service with fake in-memory clients to test full workflows
end-to-end through the API and MCP layers.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentic_service.api import app as fastapi_app
from agentic_service.service import GaramaticService
from tests.fakes import FakeMQ, FakeOdooClient, FakeTicketClient


@pytest.fixture
def fake_ticket_client():
    """Create a fresh FakeTicketClient for each test."""
    return FakeTicketClient()


@pytest.fixture
def fake_odoo_client():
    """Create a fresh FakeOdooClient for each test."""
    return FakeOdooClient()


@pytest.fixture
def fake_mq():
    """Create a fresh FakeMQ for each test."""
    return FakeMQ()


@pytest.fixture
def e2e_service(fake_ticket_client, fake_odoo_client, fake_mq):
    """Create a GaramaticService backed by fake clients."""
    from agentic_service.approval import ApprovalStore
    store = ApprovalStore(db_path=":memory:")
    return GaramaticService(fake_ticket_client, fake_odoo_client, fake_mq, approval_store=store)


@pytest_asyncio.fixture
async def api_client(e2e_service):
    """Create FastAPI test client with fake-backed service."""
    fastapi_app.state.service = e2e_service
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    fastapi_app.state.service = None


@pytest_asyncio.fixture
async def mcp_client(e2e_service):
    """Create an MCP client context for testing MCP tools.

    Returns a context object that wraps the MCP tool functions with
    a fake Context that has the service injected.
    """
    from fastmcp import Context

    ctx = MagicMock(spec=Context)
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = {"service": e2e_service}
    ctx.info = AsyncMock()
    return ctx
