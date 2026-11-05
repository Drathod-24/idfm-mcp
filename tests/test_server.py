"""End-to-end tool tests: a real MCP client talks to our server in-process,
and respx fakes the PRIM API underneath."""

from datetime import UTC, datetime

import httpx
import pytest
from mcp import Client

from idfm_mcp import server
from tests.conftest import load

NOW = datetime(2026, 11, 5, 8, 25, tzinfo=UTC)  # 09:25 in Paris


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("IDFM_API_KEY", "test-key")
    monkeypatch.setattr(server, "_now", lambda: NOW)


async def call(tool: str, args: dict) -> tuple[str, bool]:
    async with Client(server.mcp) as c:
        result = await c.call_tool(tool, args)
    return result.content[0].text, result.is_error


async def test_lists_four_documented_read_only_tools():
    async with Client(server.mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"search_stop", "next_departures", "plan_journey", "traffic_disruptions"}
    for t in tools.values():
        assert len(t.description) > 80, f"{t.name} needs a real docstring"
        assert t.annotations.read_only_hint is True
    assert tools["next_departures"].input_schema["required"] == ["stop_id"]


# ------------------------------------------------------------------ search_stop


async def test_search_stop(api):
    api.get("/v2/navitia/places").respond(json=load("places_gentilly.json"))
    text, err = await call("search_stop", {"query": "Gentilly"})
    assert not err
    assert "1. Gentilly | id: stop_area:IDFM:63244 | lines: Bus 57, RER B" in text
    assert "Mairie de Gentilly (Gentilly)" in text


async def test_search_stop_no_results(api):
    api.get("/v2/navitia/places").respond(json={"places": []})
    text, err = await call("search_stop", {"query": "Atlantis"})
    assert not err
    assert "No stop found" in text


# ------------------------------------------------------------------ next_departures


async def test_next_departures_by_id(api):
    api.get("/v2/navitia/stop_areas/stop_area:IDFM:63244/lines").respond(
        json=load("stop_area_lines_gentilly.json")
    )
    api.get("/stop-monitoring").respond(json=load("stop_monitoring_gentilly.json"))
    text, err = await call("next_departures", {"stop_id": "stop_area:IDFM:63244"})
    assert not err
    assert "RER B → Aéroport CDG 2 TGV: now, 09:26" in text
    assert "RER B → Saint-Rémy-lès-Chevreuse: in 8 min, 09:33 (+2 min vs schedule, platform 2)" in (
        text
    )


async def test_next_departures_by_name_and_line(api):
    api.get("/v2/navitia/places").respond(json=load("places_gentilly.json"))
    api.get("/v2/navitia/pt_objects").respond(
        json={
            "pt_objects": [
                {
                    "line": {
                        "id": "line:IDFM:C01743",
                        "code": "B",
                        "name": "B",
                        "commercial_mode": {"name": "RER"},
                    }
                }
            ]
        }
    )
    sm = api.get("/stop-monitoring").respond(json=load("stop_monitoring_gentilly.json"))
    text, err = await call("next_departures", {"stop_id": "Gentilly", "line": "RER B"})
    assert not err
    assert sm.calls.last.request.url.params["LineRef"] == "STIF:Line::C01743:"
    assert text.startswith("Next departures at Gentilly (stop_area:IDFM:63244)")


async def test_line_names_lookup_failure_falls_back_to_codes(api):
    api.get("/v2/navitia/stop_areas/stop_area:IDFM:63244/lines").respond(500)
    api.get("/stop-monitoring").respond(json=load("stop_monitoring_gentilly.json"))
    text, err = await call("next_departures", {"stop_id": "63244"})
    assert not err
    assert "C01743 → Aéroport CDG 2 TGV" in text


async def test_next_departures_unknown_stop(api):
    api.get("/v2/navitia/stop_areas/stop_area:IDFM:99999/lines").respond(json={"lines": []})
    api.get("/stop-monitoring").respond(400, json=load("stop_monitoring_error.json"))
    text, err = await call("next_departures", {"stop_id": "stop_area:IDFM:99999"})
    assert err
    assert "MonitoringRef inconnu" in text


# ------------------------------------------------------------------ errors (shared path)


async def test_missing_api_key(monkeypatch, api):
    monkeypatch.delenv("IDFM_API_KEY")
    text, err = await call("search_stop", {"query": "Gentilly"})
    assert err
    assert "IDFM_API_KEY is not set" in text
    assert not api.calls


async def test_rate_limit(api):
    api.get("/v2/navitia/places").respond(429)
    text, err = await call("search_stop", {"query": "Gentilly"})
    assert err
    assert "quota exceeded" in text


async def test_timeout(api):
    api.get("/v2/navitia/line_reports").mock(side_effect=httpx.ConnectTimeout("slow"))
    text, err = await call("traffic_disruptions", {})
    assert err
    assert "did not respond in time" in text


# ------------------------------------------------------------------ plan_journey


async def test_plan_journey(api):
    api.get("/v2/navitia/places").respond(json=load("places_gentilly.json"))
    j = api.get("/v2/navitia/journeys").respond(json=load("journeys.json"))
    text, err = await call(
        "plan_journey",
        {"from_place": "Gentilly", "to_place": "Étoile", "departure_time": "09:30"},
    )
    assert not err
    assert j.calls.last.request.url.params["datetime"] == "20261105T093000"
    assert "Option 1: 09:30 → 10:02 (32 min, 1 transfer)" in text
    assert "RER B toward Aéroport CDG 2 TGV: Gentilly → Châtelet les Halles (09:34–09:45)" in text
    assert "Change: walk 5 min to Châtelet" in text


async def test_plan_journey_bad_time(api):
    text, err = await call(
        "plan_journey", {"from_place": "a", "to_place": "b", "departure_time": "tomorrow-ish"}
    )
    assert err
    assert "HH:MM" in text
    assert not api.calls


async def test_plan_journey_unknown_place(api):
    api.get("/v2/navitia/places").respond(json={"places": []})
    text, err = await call("plan_journey", {"from_place": "Atlantis", "to_place": "Bastille"})
    assert err
    assert "Atlantis" in text


# ------------------------------------------------------------------ traffic_disruptions


async def test_traffic_disruptions_for_line(api):
    api.get("/v2/navitia/pt_objects").respond(json=load("pt_objects_metro7.json"))
    api.get("/v2/navitia/lines/line:IDFM:C01377/line_reports").respond(
        json=load("line_reports_metro7.json")
    )
    text, err = await call("traffic_disruptions", {"line": "metro line 7"})
    assert not err
    assert text.startswith("1 disruption(s) for Métro 7:")
    assert "[Métro 7] Métro 7 : trafic perturbé — reduced service" in text
    assert "Place d'Italie" in text
    assert "1 elevator/escalator notices not shown" in text
    assert "Gare de l'Est" not in text


async def test_traffic_disruptions_none(api):
    api.get("/v2/navitia/line_reports").respond(json={"disruptions": []})
    text, err = await call("traffic_disruptions", {})
    assert not err
    assert "No current disruptions reported for the whole network" in text
