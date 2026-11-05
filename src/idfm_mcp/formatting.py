"""Turn models into short, plain-text answers for an LLM.

Principles: one line per item, human units (minutes, local Paris time), IDs only where
the LLM needs them for a follow-up call, and a hard cap on list length so a busy
station or a network-wide incident list never floods the context window.
"""

from datetime import datetime

from idfm_mcp.client import PARIS
from idfm_mcp.models import Departure, Disruption, Journey, Stop

MAX_DEPARTURES = 10
MAX_DISRUPTIONS = 10

# Most severe first. Values come from the GTFS-RT / Navitia "effect" field.
EFFECT_RANK = {
    "NO_SERVICE": 0,
    "REDUCED_SERVICE": 1,
    "SIGNIFICANT_DELAYS": 2,
    "DETOUR": 3,
    "STOP_MOVED": 4,
    "MODIFIED_SERVICE": 5,
    "ADDITIONAL_SERVICE": 6,
    "OTHER_EFFECT": 7,
    "UNKNOWN_EFFECT": 8,
}
EFFECT_LABEL = {
    "NO_SERVICE": "no service",
    "REDUCED_SERVICE": "reduced service",
    "SIGNIFICANT_DELAYS": "significant delays",
    "DETOUR": "detour",
    "STOP_MOVED": "stop moved",
    "MODIFIED_SERVICE": "modified service",
    "ADDITIONAL_SERVICE": "additional service",
}


def hhmm(dt: datetime | None) -> str:
    return dt.astimezone(PARIS).strftime("%H:%M") if dt else "?"


def minutes(seconds: int) -> str:
    m = round(seconds / 60)
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d}"


def _truncate(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- stops


def format_stops(query: str, stops: list[Stop]) -> str:
    if not stops:
        return f"No stop found for '{query}'. Try another spelling or a nearby station name."
    lines = [f"Stops matching '{query}':"]
    for i, s in enumerate(stops, 1):
        city = f" ({s.city})" if s.city and s.city != s.name else ""
        served = f" | lines: {', '.join(s.lines)}" if s.lines else ""
        lines.append(f"{i}. {s.name}{city} | id: {s.id}{served}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- departures


def format_departures(
    stop_label: str,
    departures: list[Departure],
    now: datetime,
    line_names: dict[str, str] | None = None,
) -> str:
    line_names = line_names or {}
    upcoming = [d for d in departures if (d.expected or d.aimed)]
    if not upcoming:
        return (
            f"No real-time departures at {stop_label} right now. The service may have ended "
            "for the night, or this stop may not be covered by real-time data."
        )
    out = [f"Next departures at {stop_label} (now {hhmm(now)}):"]
    for d in upcoming[:MAX_DEPARTURES]:
        when = d.expected or d.aimed
        assert when is not None
        line = line_names.get(d.line_id or "", d.line_id or "?")
        dest = d.destination or d.direction or "?"

        if d.status == "cancelled":
            out.append(f"- {line} → {dest}: CANCELLED (was {hhmm(d.aimed or when)})")
            continue

        mins = int((when - now).total_seconds() // 60)
        eta = "now" if d.at_stop or mins <= 0 else f"in {mins} min"
        extra = []
        if d.aimed and d.expected and (delay := round((d.expected - d.aimed).total_seconds() / 60)):
            extra.append(f"{'+' if delay > 0 else ''}{delay} min vs schedule")
        if d.platform:
            extra.append(f"platform {d.platform}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        out.append(f"- {line} → {dest}: {eta}, {hhmm(when)}{suffix}")
    if len(upcoming) > MAX_DEPARTURES:
        out.append(f"(+{len(upcoming) - MAX_DEPARTURES} more)")
    return "\n".join(out)


# --------------------------------------------------------------------------- journeys


def format_journeys(from_label: str, to_label: str, journeys: list[Journey]) -> str:
    if not journeys:
        return f"No route found from {from_label} to {to_label} at that time."
    out = [f"Routes from {from_label} to {to_label}:"]
    for n, j in enumerate(journeys, 1):
        transfers = f"{j.nb_transfers} transfer{'s' if j.nb_transfers != 1 else ''}"
        out.append(
            f"\nOption {n}: {hhmm(j.departure)} → {hhmm(j.arrival)} "
            f"({minutes(j.duration_s)}, {transfers})"
        )
        step = 0
        for s in j.sections:
            if s.type == "public_transport":
                text = (
                    f"{s.mode or ''} {s.line_code or ''}".strip()
                    + (f" toward {s.direction}" if s.direction else "")
                    + f": {s.from_name} → {s.to_name} ({hhmm(s.departure)}–{hhmm(s.arrival)})"
                )
            elif s.type in ("street_network", "crow_fly") and s.duration_s > 0:
                verb = {"walking": "Walk", "bike": "Cycle", "car": "Drive"}.get(
                    s.mode or "", "Walk"
                )
                text = f"{verb} {minutes(s.duration_s)} to {s.to_name}"
            elif s.type == "transfer" and s.duration_s > 0:
                text = f"Change: walk {minutes(s.duration_s)} to {s.to_name}"
            else:
                continue  # waiting, zero-length legs, etc.
            step += 1
            out.append(f"  {step}. {text}")
    return "\n".join(out)


# --------------------------------------------------------------------------- disruptions


def format_disruptions(scope: str, items: list[Disruption], hidden_elevator_notices: int) -> str:
    if not items:
        msg = f"No current disruptions reported for {scope}. Traffic looks normal."
        if hidden_elevator_notices:
            msg += f" ({hidden_elevator_notices} elevator/escalator notices not shown.)"
        return msg

    far_future = datetime.max.replace(tzinfo=PARIS)
    ranked = sorted(
        items, key=lambda d: (EFFECT_RANK.get(d.effect or "", 9), d.start or far_future)
    )
    out = [f"{len(items)} disruption(s) for {scope}:"]
    for d in ranked[:MAX_DISRUPTIONS]:
        lines = f"[{', '.join(d.affected_lines)}] " if d.affected_lines else ""
        effect = EFFECT_LABEL.get(d.effect or "")
        status = " (planned)" if d.status == "future" else ""
        head = d.title or (d.message or "Disruption")[:80]
        out.append(f"- {lines}{head}{f' — {effect}' if effect else ''}{status}")
        if d.message and d.message != d.title:
            out.append(f"  {_truncate(d.message)}")
        if d.end:
            out.append(f"  Until {d.end.astimezone(PARIS).strftime('%d/%m %H:%M')}")
    if len(items) > MAX_DISRUPTIONS:
        out.append(f"(+{len(items) - MAX_DISRUPTIONS} more; ask about a specific line for detail)")
    if hidden_elevator_notices:
        out.append(f"({hidden_elevator_notices} elevator/escalator notices not shown.)")
    return "\n".join(out)
