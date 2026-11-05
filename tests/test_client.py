from datetime import UTC, datetime

import httpx
import pytest

from idfm_mcp.client import (
    PARIS,
    ApiUnavailableError,
    AuthError,
    IdfmClient,
    InvalidRequestError,
    MissingApiKeyError,
    RateLimitError,
    line_code,
    to_navitia_line_id,
    to_siri_line_ref,
    to_siri_stop_ref,
)
from tests.conftest import load

# ------------------------------------------------------------------ ID conversion


@pytest.mark.parametrize(
    "raw",
    ["stop_area:IDFM:63244", "IDFM:63244", "63244", " 63244 ", "STIF:StopArea:SP:63244:"],
)
def test_stop_ref_conversion(raw):
    assert to_siri_stop_ref(raw) == "STIF:StopArea:SP:63244:"


def test_platform_ref_passes_through():
    assert to_siri_stop_ref("STIF:StopPoint:Q:463158:") == "STIF:StopPoint:Q:463158:"


@pytest.mark.parametrize("bad", ["Gentilly", "", "stop_area:IDFM:abc", "stop_point:IDFM:1"])
def test_bad_stop_ids_rejected(bad):
    with pytest.raises(InvalidRequestError):
        to_siri_stop_ref(bad)


def test_line_conversions():
    assert line_code("line:IDFM:C01743") == "C01743"
    assert to_siri_line_ref("line:IDFM:C01743") == "STIF:Line::C01743:"
    assert to_navitia_line_id("STIF:Line::c01743:") == "line:IDFM:C01743"
    with pytest.raises(InvalidRequestError):
        line_code("RER B")


# ------------------------------------------------------------------ auth + errors


async def test_missing_key_raises_before_any_request(api):
    async with IdfmClient(None) as c:
        with pytest.raises(MissingApiKeyError, match="IDFM_API_KEY"):
            await c.search_stops("Gentilly")
    assert not api.calls


async def test_from_env(monkeypatch):
    monkeypatch.setenv("IDFM_API_KEY", "from-env")
    async with IdfmClient.from_env() as c:
        assert c._api_key == "from-env"
    monkeypatch.delenv("IDFM_API_KEY")
    async with IdfmClient.from_env() as c:
        assert c._api_key is None


async def test_sends_apikey_header(api, client):
    route = api.get("/v2/navitia/places").respond(json={"places": []})
    await client.search_stops("x")
    assert route.calls.last.request.headers["apikey"] == "test-key"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthError),
        (403, AuthError),
        (429, RateLimitError),
        (500, ApiUnavailableError),
        (503, ApiUnavailableError),
        (400, InvalidRequestError),
        (404, InvalidRequestError),
    ],
)
async def test_http_errors_are_typed(api, client, status, error):
    api.get("/v2/navitia/places").respond(status, json={"message": "nope"})
    with pytest.raises(error):
        await client.search_stops("x")


async def test_timeout(api, client):
    api.get("/v2/navitia/places").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ApiUnavailableError, match="did not respond in time"):
        await client.search_stops("x")


async def test_network_error(api, client):
    api.get("/v2/navitia/places").mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(ApiUnavailableError, match="Could not reach"):
        await client.search_stops("x")


async def test_non_json_response(api, client):
    api.get("/v2/navitia/places").respond(200, text="<html>maintenance</html>")
    with pytest.raises(ApiUnavailableError, match="non-JSON"):
        await client.search_stops("x")


async def test_navitia_error_payload(api, client):
    api.get("/v2/navitia/journeys").respond(
        404, json={"error": {"id": "unknown_object", "message": "Invalid id : foo"}}
    )
    with pytest.raises(InvalidRequestError, match="Invalid id"):
        await client.journeys("foo", "bar")


# ------------------------------------------------------------------ search_stops


async def test_search_stops(api, client):
    route = api.get("/v2/navitia/places").respond(json=load("places_gentilly.json"))
    stops = await client.search_stops("Gentilly")

    params = route.calls.last.request.url.params
    assert params["q"] == "Gentilly"
    assert params["type[]"] == "stop_area"

    assert [s.id for s in stops] == ["stop_area:IDFM:63244", "stop_area:IDFM:71061"]
    assert stops[0].name == "Gentilly"
    assert stops[0].city == "Gentilly"
    assert stops[0].lines == ["Bus 57", "RER B"]
    assert stops[1].lines == []


async def test_search_lines(api, client):
    api.get("/v2/navitia/pt_objects").respond(json=load("pt_objects_metro7.json"))
    lines = await client.search_lines("metro 7")
    assert len(lines) == 1
    assert lines[0].id == "line:IDFM:C01377"
    assert lines[0].code == "7"
    assert lines[0].mode == "Métro"


# ------------------------------------------------------------------ next_departures


async def test_next_departures(api, client):
    route = api.get("/stop-monitoring").respond(json=load("stop_monitoring_gentilly.json"))
    deps = await client.next_departures("stop_area:IDFM:63244", line_id="line:IDFM:C01743")

    params = route.calls.last.request.url.params
    assert params["MonitoringRef"] == "STIF:StopArea:SP:63244:"
    assert params["LineRef"] == "STIF:Line::C01743:"

    # Sorted by expected time: the CDG train (08:26) before the delayed one (08:33).
    assert [d.destination for d in deps] == ["Aéroport CDG 2 TGV", "Saint-Rémy-lès-Chevreuse"]
    first, second = deps
    assert first.at_stop is True
    assert first.line_id == "C01743"
    assert second.status == "delayed"
    assert second.platform == "2"
    # SIRI times are UTC and must stay timezone-aware.
    assert second.expected == datetime(2026, 11, 5, 8, 33, tzinfo=UTC)
    assert second.aimed == datetime(2026, 11, 5, 8, 31, tzinfo=UTC)


async def test_next_departures_without_line_filter(api, client):
    route = api.get("/stop-monitoring").respond(json=load("stop_monitoring_gentilly.json"))
    await client.next_departures("63244")
    assert "LineRef" not in route.calls.last.request.url.params


async def test_next_departures_siri_error(api, client):
    api.get("/stop-monitoring").respond(400, json=load("stop_monitoring_error.json"))
    with pytest.raises(InvalidRequestError, match="MonitoringRef inconnu"):
        await client.next_departures("99999")


async def test_next_departures_empty(api, client):
    payload = load("stop_monitoring_gentilly.json")
    payload["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"][0]["MonitoredStopVisit"] = []
    api.get("/stop-monitoring").respond(json=payload)
    assert await client.next_departures("63244") == []


async def test_invalid_stop_id_makes_no_request(api, client):
    with pytest.raises(InvalidRequestError, match="search_stop"):
        await client.next_departures("Gentilly")
    assert not api.calls


# ------------------------------------------------------------------ journeys


async def test_journeys(api, client):
    route = api.get("/v2/navitia/journeys").respond(json=load("journeys.json"))
    when = datetime(2026, 11, 5, 8, 30, tzinfo=UTC)  # = 09:30 in Paris (winter, UTC+1)
    [j] = await client.journeys("stop_area:IDFM:63244", "stop_area:IDFM:71517", when=when)

    assert route.calls.last.request.url.params["datetime"] == "20261105T093000"
    assert j.duration_s == 1920
    assert j.nb_transfers == 1
    assert j.departure == datetime(2026, 11, 5, 9, 30, tzinfo=PARIS)
    assert [s.type for s in j.sections] == [
        "street_network",
        "public_transport",
        "transfer",
        "public_transport",
    ]
    rer = j.sections[1]
    assert (rer.mode, rer.line_code, rer.to_name) == ("RER", "B", "Châtelet les Halles")
    assert j.sections[0].mode == "walking"


async def test_journeys_no_solution_is_empty(api, client):
    api.get("/v2/navitia/journeys").respond(json=load("journeys_no_solution.json"))
    assert await client.journeys("a", "b") == []


async def test_search_places(api, client):
    api.get("/v2/navitia/places").respond(json=load("places_gentilly.json"))
    assert await client.search_places("Gentilly") == "stop_area:IDFM:63244"
    api.get("/v2/navitia/places").respond(json={"places": []})
    assert await client.search_places("zzz") is None


# ------------------------------------------------------------------ disruptions


async def test_disruptions_for_line(api, client):
    route = api.get("/v2/navitia/lines/line:IDFM:C01377/line_reports").respond(
        json=load("line_reports_metro7.json")
    )
    items = await client.disruptions("C01377")
    assert route.called
    works, lift = items

    assert works.title == "Métro 7 : trafic perturbé"
    # HTML stripped and entities decoded
    assert "<" not in works.message
    assert "Place d'Italie" in works.message
    assert works.effect == "REDUCED_SERVICE"
    assert works.affected_lines == ["Métro 7"]
    assert works.start == datetime(2026, 11, 5, 6, 0, tzinfo=PARIS)

    assert "Ascenseur" in lift.tags
    assert lift.message is None


async def test_disruptions_whole_network(api, client):
    route = api.get("/v2/navitia/line_reports").respond(json={"disruptions": []})
    assert await client.disruptions() == []
    assert route.called
