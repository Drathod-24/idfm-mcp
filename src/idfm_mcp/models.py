"""Pydantic models: the clean, typed shapes the rest of the code works with.

The PRIM API returns two very different formats (SIRI Lite for real-time departures,
Navitia for everything else). The client parses both into these models so that the
server and formatting layers never touch raw JSON.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class Stop(BaseModel):
    """A stop area (station) as returned by a place search."""

    id: str = Field(description="Navitia stop area ID, e.g. 'stop_area:IDFM:71264'")
    name: str
    city: str | None = None
    lines: list[str] = Field(default_factory=list, description="e.g. ['RER B', 'Bus 57']")


class Line(BaseModel):
    """A commercial transport line."""

    id: str = Field(description="Navitia line ID, e.g. 'line:IDFM:C01743'")
    code: str = Field(description="Short public code, e.g. 'B' or '7'")
    name: str
    mode: str = Field(description="Commercial mode, e.g. 'RER', 'Métro', 'Bus'")


class Departure(BaseModel):
    """One upcoming vehicle at a stop (from SIRI stop-monitoring)."""

    line_id: str | None = Field(None, description="IDFM line code, e.g. 'C01743'")
    direction: str | None = None
    destination: str | None = None
    expected: datetime | None = Field(None, description="Expected departure (timezone-aware)")
    aimed: datetime | None = Field(None, description="Scheduled departure (timezone-aware)")
    status: str | None = Field(None, description="SIRI status, e.g. 'onTime', 'delayed'")
    platform: str | None = None
    at_stop: bool = False


class Section(BaseModel):
    """One leg of a journey: a ride on a line, a walk, a transfer or a wait."""

    type: str = Field(description="Navitia section type, e.g. 'public_transport', 'street_network'")
    mode: str | None = Field(None, description="'RER', 'Métro', 'walking', ...")
    line_code: str | None = None
    direction: str | None = None
    from_name: str | None = None
    to_name: str | None = None
    departure: datetime | None = None
    arrival: datetime | None = None
    duration_s: int = 0


class Journey(BaseModel):
    """A full itinerary between two places."""

    departure: datetime
    arrival: datetime
    duration_s: int
    nb_transfers: int
    sections: list[Section] = Field(default_factory=list)


class Disruption(BaseModel):
    """A traffic information message (incident, works, etc.)."""

    id: str
    status: str | None = Field(None, description="'active', 'future' or 'past'")
    effect: str | None = Field(None, description="e.g. 'NO_SERVICE', 'SIGNIFICANT_DELAYS'")
    severity: str | None = None
    title: str | None = None
    message: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    affected_lines: list[str] = Field(default_factory=list)
