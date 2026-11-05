"""Tool-selection eval: does an LLM pick the right tool and arguments from our docstrings?

It reads the tool definitions straight from the MCP server (so the eval always tests
the real docstrings), sends each question to Claude with those tools, and scores the
FIRST tool call against the acceptable answers in questions.json. No PRIM API calls
are made: we only test the routing decision, which is what the docstrings control.

    uv run --group evals python evals/run_evals.py            # live (needs ANTHROPIC_API_KEY)
    uv run --group evals python evals/run_evals.py --offline  # validate dataset only (CI)

Results are written to evals/results.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

from mcp import Client

from idfm_mcp.server import mcp

HERE = Path(__file__).parent
MODEL = "claude-opus-5-5"
SYSTEM = (
    "You are a Paris public transport assistant. Use the available tools when they help "
    "answer the question. If no tool is relevant, answer directly without calling one."
)


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return text.lower().strip()


def matches(expected: dict[str, Any], tool: str | None, args: dict[str, Any]) -> bool:
    if expected.get("tool") != tool:
        return False
    for key, want in expected.get("args", {}).items():
        got = args.get(key)
        if want is None:
            if got not in (None, ""):
                return False
        elif got is None or norm(want) not in norm(got):
            return False
    return True


async def load_tools() -> list[dict[str, Any]]:
    """Convert the server's MCP tool list into Anthropic API tool definitions."""
    async with Client(mcp) as c:
        listed = (await c.list_tools()).tools
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in listed
    ]


def validate(cases: list[dict], tools: list[dict]) -> list[str]:
    """Check every expected call names a real tool and real parameters."""
    params = {t["name"]: set(t["input_schema"].get("properties", {})) for t in tools}
    errors = []
    for case in cases:
        for exp in case["accept"]:
            name = exp.get("tool")
            if name is None:
                continue
            if name not in params:
                errors.append(f"{case['id']}: unknown tool {name}")
            elif unknown := set(exp.get("args", {})) - params[name]:
                errors.append(f"{case['id']}: unknown args {unknown} for {name}")
    return errors


async def first_tool_call(client: Any, tools: list[dict], question: str) -> tuple[str | None, dict]:
    # Opus 5.5 does not support forced tool_choice; "auto" also lets us test that the
    # model correctly calls *no* tool for off-topic questions.
    response = await client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        tools=tools,
        tool_choice={"type": "auto"},
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": question}],
    )
    if response.stop_reason == "refusal":
        return "<refusal>", {}
    for block in response.content:
        if block.type == "tool_use":
            return block.name, dict(block.input)
    return None, {}


async def run_live(cases: list[dict], tools: list[dict]) -> list[dict]:
    import anthropic

    client = anthropic.AsyncAnthropic()
    sem = asyncio.Semaphore(5)

    async def one(case: dict) -> dict:
        async with sem:
            try:
                tool, args = await first_tool_call(client, tools, case["question"])
                error = None
            except anthropic.APIError as e:
                tool, args, error = None, {}, f"{type(e).__name__}: {e}"
        passed = error is None and any(matches(exp, tool, args) for exp in case["accept"])
        return {**case, "tool": tool, "args": args, "passed": passed, "error": error}

    return await asyncio.gather(*(one(c) for c in cases))


def report(results: list[dict]) -> str:
    passed = sum(r["passed"] for r in results)
    lines = [
        f"# Eval results ({MODEL})",
        "",
        f"**{passed}/{len(results)} passed ({passed / len(results):.0%})**",
        "",
        "| # | Question | Expected | Got | Result |",
        "|---|---|---|---|---|",
    ]
    for i, r in enumerate(results, 1):
        expected = " or ".join(e["tool"] or "(no tool)" for e in r["accept"])
        got = r["error"] or (
            f"`{r['tool']}({json.dumps(r['args'], ensure_ascii=False)})`"
            if r["tool"]
            else "(no tool)"
        )
        lines.append(
            f"| {i} | {r['question']} | {expected} | {got} | {'✅' if r['passed'] else '❌'} |"
        )
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="validate dataset only")
    opts = parser.parse_args()

    cases = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))["cases"]
    tools = await load_tools()

    if errors := validate(cases, tools):
        print("Dataset errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"Dataset OK: {len(cases)} cases, {len(tools)} tools.")
    if opts.offline:
        return 0

    results = await run_live(cases, tools)
    text = report(results)
    (HERE / "results.md").write_text(text, encoding="utf-8")
    print(text)
    return 0 if all(r["passed"] for r in results) else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
