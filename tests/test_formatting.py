from datetime import UTC, datetime, timedelta

from idfm_mcp import formatting as f
from idfm_mcp.client import PARIS
from idfm_mcp.models import Departure, Disruption

NOW = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)


def test_minutes():
    assert f.minutes(240) == "4 min"
    assert f.minutes(3900) == "1 h 05"


def test_hhmm_converts_to_paris_time():
    assert f.hhmm(datetime(2026, 7, 1, 12, 0, tzinfo=UTC)) == "14:00"  # summer, UTC+2
    assert f.hhmm(datetime(2026, 12, 1, 12, 0, tzinfo=UTC)) == "13:00"  # winter, UTC+1


def test_departures_cancelled_and_capped():
    deps = [
        Departure(line_id="C1", destination="X", aimed=NOW, expected=NOW, status="cancelled")
    ] + [
        Departure(line_id="C1", destination=f"D{i}", expected=NOW + timedelta(minutes=i))
        for i in range(1, 15)
    ]
    text = f.format_departures("Stop", deps, NOW, {"C1": "Métro 1"})
    assert "Métro 1 → X: CANCELLED (was 09:00)" in text
    assert "(+5 more)" in text
    assert len(text.splitlines()) == 1 + f.MAX_DEPARTURES + 1


def test_departures_empty():
    assert "No real-time departures" in f.format_departures("Stop", [], NOW)


def test_disruptions_sorted_by_severity():
    items = [
        Disruption(id="a", title="Minor", effect="OTHER_EFFECT"),
        Disruption(id="b", title="Line closed", effect="NO_SERVICE"),
        Disruption(id="c", title="Works next week", status="future", effect="DETOUR"),
    ]
    lines = f.format_disruptions("RER A", items, 0).splitlines()
    assert lines[1].startswith("- Line closed — no service")
    assert "(planned)" in lines[2]


def test_disruption_message_truncated():
    d = Disruption(
        id="a", title="T", message="word " * 200, end=datetime(2026, 11, 5, tzinfo=PARIS)
    )
    text = f.format_disruptions("x", [d], 0)
    assert "…" in text
    assert "Until 05/11 00:00" in text
