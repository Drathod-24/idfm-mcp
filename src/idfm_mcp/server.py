"""MCP server exposing Île-de-France Mobilités (Paris public transport) as 4 tools.

Each tool:
1. resolves human input (station names, "RER B") into API IDs,
2. calls the PRIM API through IdfmClient,
3. returns short plain text built by formatting.py,
4. converts any IdfmError into a ToolError, so the client gets `isError: true` and a
   clear message instead of a stack trace.
"""

import re
import unicodedata
from datetime import UTC, datetime

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from idfm_mcp import formatting
from idfm_mcp.client import (
    PARIS,
    IdfmClient,
    IdfmError,
    InvalidRequestError,
    line_code,
    to_navitia_line_id,
    to_siri_stop_ref,
)
from idfm_mcp.models import Line

INSTRUCTIONS = """\
Real-time public transport for Paris and Île-de-France (Metro, RER, Transilien, Tram, Bus).
Typical flow: search_stop to get a station ID, then next_departures with that ID.
plan_journey and traffic_disruptions accept plain names directly.
All times are Europe/Paris local time."""

mcp = MCPServer("idfm-mcp", instructions=INSTRUCTIONS, version="0.1.0")
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _now() -> datetime:
    """Wrapped so tests can freeze time."""
    return datetime.now(UTC)


def _client() -> IdfmClient:
    return IdfmClient.from_env()


# --------------------------------------------------------------------------- resolution helpers

_MODE_WORDS = {
    "metro": "metro",
    "rer": "rer",
    "tram": "tramway",
    "tramway": "tramway",
    "bus": "bus",
    "noctilien": "bus",
    "transilien": "train transilien",
    "train": "train transilien",
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return text.lower().strip()


async def _resolve_line(client: IdfmClient, line: str) -> Line:
    """Turn 'RER B', 'metro 7', 'ligne 13', 'T3a' or 'C01743' into a Line."""
    try:
        code = line_code(line)
        return Line(id=to_navitia_line_id(code), code=code, name=line, mode="")
    except InvalidRequestError:
        pass

    query = _norm(line)
    query = re.sub(r"\b(ligne|line|la|le|the)\b", " ", query).strip()
    words = query.split()
    wanted_mode = next((_MODE_WORDS[w] for w in words if w in _MODE_WORDS), None)
    wanted_code = next((w for w in reversed(words) if w not in _MODE_WORDS), None)
    if wanted_mode is None and wanted_code and wanted_code.isdigit():
        wanted_mode = "metro"  # bare "7" or "ligne 7" almost always means the Metro in Paris

    candidates = await client.search_lines(line)
    if not candidates:
        raise InvalidRequestError(
            f"No line found for '{line}'. Include the mode, e.g. 'Metro 7', 'RER B', 'Tram T3a'."
        )

    def score(ln: Line) -> int:
        s = 0
        if wanted_code and _norm(ln.code) == wanted_code:
            s += 2
        if wanted_mode and wanted_mode.split()[0] in _norm(ln.mode):
            s += 1
        return s

    return max(candidates, key=score)  # max() keeps the API's order on ties


def _line_label(ln: Line) -> str:
    return f"{ln.mode} {ln.code}".strip() if ln.mode else ln.name


def _looks_like_stop_id(text: str) -> bool:
    try:
        to_siri_stop_ref(text)
        return True
    except InvalidRequestError:
        return False


def _parse_time(value: str) -> datetime:
    """Accept '18:30', '2026-11-05T18:30' or a full ISO string. Naive = Paris time."""
    value = value.strip()
    try:
        if re.fullmatch(r"\d{1,2}[:h]\d{2}", value):
            hour, minute = map(int, re.split(r"[:h]", value))
            today = _now().astimezone(PARIS)
            return today.replace(hour=hour, minute=minute, second=0, microsecond=0)
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise InvalidRequestError(
            f"Could not read departure_time '{value}'. Use 'HH:MM' or 'YYYY-MM-DDTHH:MM'."
        ) from e
    return dt if dt.tzinfo else dt.replace(tzinfo=PARIS)


# --------------------------------------------------------------------------- tools


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def search_stop(query: str) -> str:
    """Find Paris / Île-de-France stations and stops by name and return their IDs.

    Use this first when the user names a station (e.g. "Gentilly", "Châtelet",
    "Gare de Lyon") and you need its ID for next_departures. Returns up to 5 matches
    with the lines serving each one, so you can pick the right station.

    Args:
        query: Station or stop name, in French or English spelling. e.g. "Gentilly".
    """
    if not query.strip():
        raise ToolError("query must not be empty.")
    try:
        async with _client() as client:
            stops = await client.search_stops(query)
    except IdfmError as e:
        raise ToolError(str(e)) from e
    return formatting.format_stops(query, stops)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def next_departures(stop_id: str, line: str | None = None) -> str:
    """Get the next real-time departures at a station, with minutes until departure.

    Use for questions like "When is the next RER B from Gentilly?" or "Next metro at
    Bastille?". Shows line, destination, minutes to wait, delay and platform.

    Args:
        stop_id: Station ID from search_stop (e.g. "stop_area:IDFM:63244"). A plain
            station name also works; the best match is used.
        line: Optional line filter, e.g. "RER B", "Metro 7", "Tram T3a", "Bus 57".
            Leave empty to get all lines at the station.
    """
    try:
        async with _client() as client:
            stop_label = stop_id
            if not _looks_like_stop_id(stop_id):
                matches = await client.search_stops(stop_id, count=1)
                if not matches:
                    raise InvalidRequestError(
                        f"No station found for '{stop_id}'. Use search_stop to find the ID."
                    )
                stop_label = f"{matches[0].name} ({matches[0].id})"
                stop_id = matches[0].id

            line_names: dict[str, str] = {}
            line_id = None
            if line:
                resolved = await _resolve_line(client, line)
                line_id = resolved.id
                line_names[line_code(resolved.id)] = _line_label(resolved)
            else:
                try:
                    for ln in await client.stop_area_lines(stop_id):
                        line_names[line_code(ln.id)] = _line_label(ln)
                except IdfmError:
                    pass  # names are a nice-to-have; fall back to raw line codes

            departures = await client.next_departures(stop_id, line_id)
    except IdfmError as e:
        raise ToolError(str(e)) from e
    return formatting.format_departures(stop_label, departures, _now(), line_names)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def plan_journey(from_place: str, to_place: str, departure_time: str | None = None) -> str:
    """Plan a public transport journey between two places in Île-de-France.

    Use for "How do I get from X to Y?" or "Fastest way to Orly from Bastille at 18:00?".
    Returns up to 3 options with total duration, number of transfers and each step
    (lines to take, where to change, walking time).

    Args:
        from_place: Origin: station name, address or landmark (e.g. "Gentilly",
            "Tour Eiffel", "10 rue de Rivoli Paris") or a stop ID from search_stop.
        to_place: Destination, same formats as from_place.
        departure_time: Optional, Paris local time: "18:30" (today) or
            "2026-11-05T18:30". Defaults to now.
    """
    try:
        when = _parse_time(departure_time) if departure_time else None
        async with _client() as client:
            ids = []
            for place in (from_place, to_place):
                pid = place if place.startswith("stop_area:") else await client.search_places(place)
                if not pid:
                    raise InvalidRequestError(f"Could not find a place called '{place}'.")
                ids.append(pid)
            journeys = await client.journeys(ids[0], ids[1], when=when)
    except IdfmError as e:
        raise ToolError(str(e)) from e
    return formatting.format_journeys(from_place, to_place, journeys)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def traffic_disruptions(line: str | None = None) -> str:
    """Get current traffic disruptions, incidents and works on the Île-de-France network.

    Use for "Are there problems on Metro 7?", "Is the RER A running?" or "Any
    disruptions right now?". Returns active and planned disruptions, most severe first.
    Elevator/escalator outages are counted but not listed.

    Args:
        line: Optional line, e.g. "Metro 7", "RER B", "Tram T2", "Transilien L".
            Leave empty for a network-wide summary.
    """
    try:
        async with _client() as client:
            scope = "the whole network"
            line_id = None
            if line:
                resolved = await _resolve_line(client, line)
                line_id = resolved.id
                scope = _line_label(resolved)
            items = await client.disruptions(line_id)
    except IdfmError as e:
        raise ToolError(str(e)) from e

    relevant = [d for d in items if d.status in (None, "active", "future")]
    elevator = [d for d in relevant if any("ascenseur" in _norm(t) for t in d.tags)]
    shown = [d for d in relevant if d not in elevator]
    return formatting.format_disruptions(scope, shown, len(elevator))


def main() -> None:
    """Entry point (`idfm-mcp` command). Loads .env, then serves over stdio."""
    load_dotenv()
    mcp.run()


if __name__ == "__main__":
    main()
