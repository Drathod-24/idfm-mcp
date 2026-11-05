"""Keep the eval dataset honest: it must reference real tools/params, and the
scoring function must behave as documented."""

import json
from pathlib import Path

import pytest

from evals.run_evals import load_tools, matches, validate

QUESTIONS = Path(__file__).parent.parent / "evals" / "questions.json"


async def test_dataset_matches_server_tools():
    cases = json.loads(QUESTIONS.read_text(encoding="utf-8"))["cases"]
    assert len(cases) == 20
    assert len({c["id"] for c in cases}) == 20
    assert validate(cases, await load_tools()) == []


@pytest.mark.parametrize(
    ("expected", "tool", "args", "ok"),
    [
        (
            {"tool": "search_stop", "args": {"query": "chatelet"}},
            "search_stop",
            {"query": "Châtelet"},
            True,
        ),
        ({"tool": "search_stop", "args": {"query": "chatelet"}}, "plan_journey", {}, False),
        ({"tool": "traffic_disruptions", "args": {"line": None}}, "traffic_disruptions", {}, True),
        (
            {"tool": "traffic_disruptions", "args": {"line": None}},
            "traffic_disruptions",
            {"line": "7"},
            False,
        ),
        (
            {"tool": "traffic_disruptions", "args": {"line": "7"}},
            "traffic_disruptions",
            {"line": "Metro 7"},
            True,
        ),
        ({"tool": None}, None, {}, True),
        ({"tool": None}, "search_stop", {"query": "x"}, False),
    ],
)
def test_matches(expected, tool, args, ok):
    assert matches(expected, tool, args) is ok
