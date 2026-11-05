import json
from pathlib import Path

import pytest
import respx

from idfm_mcp.client import BASE_URL, IdfmClient

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def api():
    """Intercept all HTTP calls to PRIM. Any un-mocked request fails the test,
    so tests can never hit the real API by accident."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client():
    async with IdfmClient("test-key") as c:
        yield c
