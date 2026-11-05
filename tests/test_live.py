"""Optional tests against the real PRIM API.

Skipped unless IDFM_API_KEY is set (so CI never needs a key). They check that the real
responses parse into our models, i.e. that the synthetic fixtures match reality.
Run with: uv run pytest -m live
Uses ~5 requests of your daily quota.
"""

import os

import pytest
from dotenv import load_dotenv

from idfm_mcp.client import IdfmClient

load_dotenv()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("IDFM_API_KEY"), reason="IDFM_API_KEY not set"),
]


@pytest.fixture
async def live():
    async with IdfmClient.from_env() as c:
        yield c


async def test_live_search_and_departures(live):
    stops = await live.search_stops("Châtelet")
    assert stops and stops[0].id.startswith("stop_area:")
    deps = await live.next_departures(stops[0].id)
    assert isinstance(deps, list)  # may be empty at night


async def test_live_lines_and_disruptions(live):
    lines = await live.search_lines("RER B")
    assert lines and lines[0].id.startswith("line:")
    assert isinstance(await live.disruptions(lines[0].id), list)


async def test_live_journey(live):
    journeys = await live.journeys("stop_area:IDFM:71264", "stop_area:IDFM:71517")
    assert journeys and journeys[0].duration_s > 0
