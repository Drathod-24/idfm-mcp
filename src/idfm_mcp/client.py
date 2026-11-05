"""Async HTTP client for the Île-de-France Mobilités PRIM API.

Responsibilities:
- authentication (the `apikey` header) and timeouts
- turning every HTTP/network failure into one of a few typed exceptions
- converting between the two ID systems (Navitia vs SIRI)
- parsing raw JSON into the pydantic models in `models.py`

Docs: https://prim.iledefrance-mobilites.fr/en/aide-et-contact/documentation
"""

from __future__ import annotations

import html
import os
import re
from datetime import UTC, datetime
from typing import Any, Self
from zoneinfo import ZoneInfo

import httpx

from idfm_mcp.models import Departure, Disruption, Journey, Line, Section, Stop

BASE_URL = "https://prim.iledefrance-mobilites.fr/marketplace"
NAVITIA = "/v2/navitia"
DEFAULT_TIMEOUT = 10.0
PARIS = ZoneInfo("Europe/Paris")


# --------------------------------------------------------------------------- errors


class IdfmError(Exception):
    """Base class. Every message is written to be shown directly to the LLM/user."""


class MissingApiKeyError(IdfmError):
    def __init__(self) -> None:
        super().__init__(
            "IDFM_API_KEY is not set. Get a free key at https://prim.iledefrance-mobilites.fr "
            "(account menu > 'Mes jetons d'authentification') and add it to your .env file."
        )


class AuthError(IdfmError):
    """401/403: the key is wrong, revoked, or not subscribed to this API."""


class InvalidRequestError(IdfmError):
    """400/404 or an API-level error: bad stop ID, unknown line, etc."""


class RateLimitError(IdfmError):
    """429: per-second or daily quota exceeded."""


class ApiUnavailableError(IdfmError):
    """Timeouts, network failures and 5xx errors."""


# --------------------------------------------------------------------------- ID helpers

_DIGITS = re.compile(r"(\d+)")
_LINE_CODE = re.compile(r"(C\d{5})", re.IGNORECASE)


def to_siri_stop_ref(stop_id: str) -> str:
    """Convert any stop ID format to the SIRI MonitoringRef PRIM expects.

    Accepts 'stop_area:IDFM:71264', 'IDFM:71264', '71264', 'STIF:StopArea:SP:71264:'
    and platform-level 'STIF:StopPoint:Q:463158:' (passed through unchanged).
    """
    stop_id = stop_id.strip()
    if stop_id.startswith("STIF:StopPoint:Q:") or stop_id.startswith("STIF:StopArea:SP:"):
        return stop_id if stop_id.endswith(":") else stop_id + ":"
    if stop_id.startswith("stop_point:"):
        raise InvalidRequestError(
            f"'{stop_id}' is a platform ID. Use the station ID from search_stop "
            "(it starts with 'stop_area:')."
        )
    match = _DIGITS.search(stop_id)
    if not match or not re.fullmatch(r"(stop_area:)?(IDFM:)?\d+", stop_id):
        raise InvalidRequestError(
            f"'{stop_id}' is not a valid stop ID. Call search_stop first to get one "
            "(e.g. 'stop_area:IDFM:71264')."
        )
    return f"STIF:StopArea:SP:{match.group(1)}:"


def line_code(line_id: str) -> str:
    """Extract the IDFM line code ('C01743') from 'line:IDFM:C01743', 'STIF:Line::C01743:'..."""
    match = _LINE_CODE.search(line_id)
    if not match:
        raise InvalidRequestError(f"'{line_id}' is not a valid line ID (expected e.g. 'C01743').")
    return match.group(1).upper()


def to_siri_line_ref(line_id: str) -> str:
    return f"STIF:Line::{line_code(line_id)}:"


def to_navitia_line_id(line_id: str) -> str:
    return f"line:IDFM:{line_code(line_id)}"


# --------------------------------------------------------------------------- parse helpers


def _parse_siri_time(value: str | None) -> datetime | None:
    """SIRI times are ISO 8601 in UTC, e.g. '2026-11-05T08:31:00.000Z'."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _parse_navitia_time(value: str | None) -> datetime | None:
    """Navitia times are Paris local time without offset, e.g. '20261105T083000'."""
    if not value:
        return None
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=PARIS)


def _first_value(items: list[dict[str, Any]] | None) -> str | None:
    """SIRI wraps strings as [{'value': '...'}]."""
    if items and isinstance(items, list):
        return items[0].get("value")
    return None


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _line_label(line: dict[str, Any]) -> str:
    mode = (line.get("commercial_mode") or {}).get("name", "")
    code = line.get("code") or line.get("name", "")
    return f"{mode} {code}".strip()


# --------------------------------------------------------------------------- client


class IdfmClient:
    """Thin async wrapper over the PRIM endpoints this project uses.

    Use as an async context manager so the underlying connection pool is closed:

        async with IdfmClient.from_env() as client:
            stops = await client.search_stops("Gentilly")
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._http = http or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "idfm-mcp/0.1"},
        )

    @classmethod
    def from_env(cls, **kwargs: Any) -> Self:
        """Read IDFM_API_KEY from the environment (a missing key fails at call time,
        not at startup, so the MCP server still starts and can explain the problem)."""
        return cls(os.environ.get("IDFM_API_KEY") or None, **kwargs)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----------------------------------------------------------------- transport

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._api_key:
            raise MissingApiKeyError()
        try:
            resp = await self._http.get(path, params=params, headers={"apikey": self._api_key})
        except httpx.TimeoutException as e:
            raise ApiUnavailableError(
                "The IDFM API did not respond in time. Try again in a moment."
            ) from e
        except httpx.HTTPError as e:
            raise ApiUnavailableError(f"Could not reach the IDFM API ({type(e).__name__}).") from e

        status = resp.status_code
        if status in (401, 403):
            raise AuthError(
                "The IDFM API rejected the API key (HTTP "
                f"{status}). Check IDFM_API_KEY and that your PRIM account can use this API."
            )
        if status == 429:
            raise RateLimitError(
                "IDFM API quota exceeded (default: 5 requests/second, 1000/day). "
                "Wait a bit before retrying."
            )
        if status >= 500:
            raise ApiUnavailableError(f"The IDFM API is having problems (HTTP {status}).")

        try:
            data = resp.json()
        except ValueError as e:
            raise ApiUnavailableError(f"Unexpected non-JSON response (HTTP {status}).") from e

        # Navitia reports errors as {"error": {"id": ..., "message": ...}}, sometimes with 200.
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            err = data["error"]
            if err.get("id") == "no_solution":
                return {"journeys": []}
            raise InvalidRequestError(err.get("message") or f"API error: {err.get('id')}")
        if status >= 400 and not _siri_delivery(data):
            raise InvalidRequestError(f"The IDFM API refused the request (HTTP {status}).")
        return data

    # ----------------------------------------------------------------- endpoints

    async def search_stops(self, query: str, count: int = 5) -> list[Stop]:
        """Navitia /places, restricted to stop areas (stations)."""
        data = await self._get(
            f"{NAVITIA}/places",
            {"q": query, "type[]": "stop_area", "count": count, "depth": 2},
        )
        stops = []
        for place in data.get("places", []):
            area = place.get("stop_area") or {}
            regions = area.get("administrative_regions") or []
            lines = sorted({_line_label(ln) for ln in area.get("lines", [])})
            stops.append(
                Stop(
                    id=place.get("id", area.get("id", "")),
                    name=area.get("name") or place.get("name", ""),
                    city=regions[0].get("name") if regions else None,
                    lines=lines,
                )
            )
        return stops

    async def search_places(self, query: str) -> str | None:
        """Best-match place ID (stop area, address or POI) for journey planning."""
        data = await self._get(f"{NAVITIA}/places", {"q": query, "count": 1})
        places = data.get("places", [])
        return places[0]["id"] if places else None

    async def search_lines(self, query: str, count: int = 5) -> list[Line]:
        """Navitia /pt_objects restricted to lines, e.g. 'metro 7' or 'RER B'."""
        data = await self._get(
            f"{NAVITIA}/pt_objects", {"q": query, "type[]": "line", "count": count}
        )
        lines = []
        for obj in data.get("pt_objects", []):
            ln = obj.get("line") or {}
            if not ln.get("id"):
                continue
            lines.append(
                Line(
                    id=ln["id"],
                    code=ln.get("code", ""),
                    name=ln.get("name", obj.get("name", "")),
                    mode=(ln.get("commercial_mode") or {}).get("name", ""),
                )
            )
        return lines

    async def stop_area_lines(self, stop_id: str) -> list[Line]:
        """Lines serving a stop area, used to turn SIRI line codes into readable names."""
        number = to_siri_stop_ref(stop_id).rstrip(":").rsplit(":", 1)[-1]
        data = await self._get(f"{NAVITIA}/stop_areas/stop_area:IDFM:{number}/lines", {"count": 50})
        return [
            Line(
                id=ln["id"],
                code=ln.get("code", ""),
                name=ln.get("name", ""),
                mode=(ln.get("commercial_mode") or {}).get("name", ""),
            )
            for ln in data.get("lines", [])
            if ln.get("id")
        ]

    async def next_departures(self, stop_id: str, line_id: str | None = None) -> list[Departure]:
        """SIRI Lite /stop-monitoring. Times come back in UTC."""
        params = {"MonitoringRef": to_siri_stop_ref(stop_id)}
        if line_id:
            params["LineRef"] = to_siri_line_ref(line_id)
        data = await self._get("/stop-monitoring", params)

        delivery = _siri_delivery(data)
        if delivery is None:
            raise ApiUnavailableError("Unexpected response format from stop-monitoring.")
        if str(delivery.get("Status", "true")).lower() == "false":
            err = (delivery.get("ErrorCondition") or {}).get("ErrorInformation") or {}
            raise InvalidRequestError(
                f"Real-time data unavailable for this stop: {err.get('ErrorText', 'unknown error')}"
            )

        departures = []
        for visit in delivery.get("MonitoredStopVisit", []):
            mvj = visit.get("MonitoredVehicleJourney") or {}
            call = mvj.get("MonitoredCall") or {}
            line_ref = (mvj.get("LineRef") or {}).get("value", "")
            code = _LINE_CODE.search(line_ref)
            departures.append(
                Departure(
                    line_id=code.group(1) if code else None,
                    direction=_first_value(mvj.get("DirectionName")),
                    destination=_first_value(call.get("DestinationDisplay"))
                    or _first_value(mvj.get("DestinationName")),
                    expected=_parse_siri_time(
                        call.get("ExpectedDepartureTime") or call.get("ExpectedArrivalTime")
                    ),
                    aimed=_parse_siri_time(
                        call.get("AimedDepartureTime") or call.get("AimedArrivalTime")
                    ),
                    status=call.get("DepartureStatus") or call.get("ArrivalStatus"),
                    platform=(call.get("ArrivalPlatformName") or {}).get("value"),
                    at_stop=bool(call.get("VehicleAtStop", False)),
                )
            )
        departures.sort(key=lambda d: d.expected or d.aimed or datetime.max.replace(tzinfo=UTC))
        return departures

    async def journeys(
        self, from_id: str, to_id: str, when: datetime | None = None, count: int = 3
    ) -> list[Journey]:
        """Navitia /journeys. `when` is converted to Paris local time."""
        params: dict[str, Any] = {"from": from_id, "to": to_id, "count": count}
        if when:
            local = when.astimezone(PARIS) if when.tzinfo else when
            params["datetime"] = local.strftime("%Y%m%dT%H%M%S")
        data = await self._get(f"{NAVITIA}/journeys", params)

        result = []
        for j in data.get("journeys", []):
            sections = []
            for s in j.get("sections", []):
                info = s.get("display_informations") or {}
                sections.append(
                    Section(
                        type=s.get("type", ""),
                        mode=info.get("commercial_mode") or s.get("mode"),
                        line_code=info.get("code"),
                        direction=info.get("direction"),
                        from_name=(s.get("from") or {}).get("name"),
                        to_name=(s.get("to") or {}).get("name"),
                        departure=_parse_navitia_time(s.get("departure_date_time")),
                        arrival=_parse_navitia_time(s.get("arrival_date_time")),
                        duration_s=s.get("duration", 0),
                    )
                )
            result.append(
                Journey(
                    departure=_parse_navitia_time(j["departure_date_time"]),
                    arrival=_parse_navitia_time(j["arrival_date_time"]),
                    duration_s=j.get("duration", 0),
                    nb_transfers=j.get("nb_transfers", 0),
                    sections=sections,
                )
            )
        return result

    async def disruptions(self, line_id: str | None = None) -> list[Disruption]:
        """Navitia /line_reports, for one line or the whole network."""
        path = f"{NAVITIA}/line_reports"
        if line_id:
            path = f"{NAVITIA}/lines/{to_navitia_line_id(line_id)}/line_reports"
        data = await self._get(path, {"count": 100})

        # Map disruption ID -> names of lines it affects (from the line_reports section).
        affected: dict[str, set[str]] = {}
        for report in data.get("line_reports", []):
            label = _line_label(report.get("line") or {})
            for link in (report.get("line") or {}).get("links", []):
                if link.get("type") == "disruption":
                    affected.setdefault(link.get("id", ""), set()).add(label)

        result = []
        for d in data.get("disruptions", []):
            title = message = None
            for m in d.get("messages", []):
                channel = (m.get("channel") or {}).get("name", "")
                if channel == "titre":
                    title = _strip_html(m.get("text", ""))
                elif channel == "moteur" or (message is None and channel != "titre"):
                    message = _strip_html(m.get("text", ""))
            periods = d.get("application_periods") or [{}]
            severity = d.get("severity") or {}
            result.append(
                Disruption(
                    id=d.get("id", ""),
                    status=d.get("status"),
                    effect=severity.get("effect"),
                    severity=severity.get("name"),
                    title=title,
                    message=message,
                    start=_parse_navitia_time(periods[0].get("begin")),
                    end=_parse_navitia_time(periods[-1].get("end")),
                    tags=d.get("tags", []),
                    affected_lines=sorted(affected.get(d.get("id", ""), set())),
                )
            )
        return result


def _siri_delivery(data: Any) -> dict[str, Any] | None:
    """Return Siri.ServiceDelivery.StopMonitoringDelivery[0], or None if absent."""
    try:
        return data["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"][0]
    except (KeyError, IndexError, TypeError):
        return None
