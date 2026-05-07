"""End-to-end CLI demo.

Usage:
    python scripts/run_demo.py "Your proposal text here"
    cat proposal.md | python scripts/run_demo.py -
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from crucible.client import ChatClient
from crucible.orchestrator import gather_critiques, run_debate
from crucible.personas import load_personas
from crucible.settings import load_settings
from crucible.synthesizer import render_markdown, synthesize


async def main(text: str) -> None:
    settings = load_settings()
    personas = load_personas(Path(settings.personas_dir))
    default_model = settings.defaults.get("model", "tinyllama")

    print(
        f"[crucible] {len(personas)} personas: "
        + ", ".join(p.name for p in personas),
        file=sys.stderr,
    )
    print(f"[crucible] endpoint: {settings.endpoint.base_url}", file=sys.stderr)
    print(f"[crucible] default model: {default_model}", file=sys.stderr)

    async def progress(event: str, name: str) -> None:
        print(f"[crucible] {event}: {name}", file=sys.stderr)

    async with ChatClient(settings.endpoint) as client:
        print("[crucible] round 1: gathering critiques...", file=sys.stderr)
        critiques = await gather_critiques(
            client, personas, text, default_model, progress=progress
        )
        print("[crucible] round 2: cross-debate...", file=sys.stderr)
        debate = await run_debate(
            client, personas, text, critiques, default_model, progress=progress
        )
        print("[crucible] synthesizing...", file=sys.stderr)
        report = await synthesize(
            client,
            text,
            critiques,
            debate,
            model=settings.orchestration.synthesizer_model,
            temperature=settings.orchestration.synthesizer_temperature,
        )
    print(render_markdown(report))


def _read_input(argv: list[str]) -> str:
    if len(argv) < 2:
        print(
            "Usage: python scripts/run_demo.py 'proposal text' "
            "(or '-' to read from stdin)",
            file=sys.stderr,
        )
        sys.exit(1)
    if argv[1] == "-":
        return sys.stdin.read()
    return argv[1]


if __name__ == "__main__":
    asyncio.run(main(_read_input(sys.argv)))
