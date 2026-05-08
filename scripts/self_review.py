"""Re-run Crucible on its own project pitch and overwrite SELF_REVIEW.md.

This is the recursive proof: the tool's value claim is testable on the
tool itself. Anyone cloning the repo can run `python scripts/self_review.py`
to regenerate it from scratch and verify the findings haven't been
hand-edited.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from crucible.client import ChatClient
from crucible.orchestrator import gather_critiques, run_debate
from crucible.personas import load_personas
from crucible.settings import load_settings
from crucible.synthesizer import render_markdown, synthesize


PITCH = """\
Crucible is a multi-agent adversarial review engine that runs six \
architecturally distinct open-source LLMs in parallel on a single AMD \
MI300X GPU. Each model plays a different reviewer persona: Skeptic \
(Qwen2.5-7B), Red Teamer (Hermes-3-Llama-8B), Ethics Auditor \
(Falcon3-7B), Market Critic (Phi-3.5-mini), Devil's Advocate \
(Yi-1.5-9B), Pragmatist (InternLM2.5-7B). The system synthesizes their \
critiques into a structured report scored by cross-architecture \
agreement, with verbatim evidence quotes per finding.

The problem: most AI-judging-AI systems run one model with six \
different prompts. When that model agrees with itself across \
perspectives, agreement is a correlated signal — one prior repeated \
six times. Crucible replaces this with genuine architectural \
diversity. When Qwen, Phi, Falcon, Hermes-Llama, InternLM, and Yi \
independently flag the same risk, that's real cross-validation.

Architecture: a LiteLLM proxy on 127.0.0.1:8000 routes by model name \
to six vLLM containers on ports 8001-8006, each loading a different \
model with gpu-memory-utilization 0.13 so all six fit in 192 GB VRAM. \
The Python library hits the proxy through an SSH tunnel from the \
operator's laptop. The pipeline runs Round 1 of independent critiques \
in parallel, Round 2 of cross-debate where each persona reads the \
others, and a two-pass synthesizer that emits structured JSON with \
verbatim evidence quotes per finding.

Trust mechanisms: evidence citations, self-disclosure metadata \
(run_id, schema version, per-persona model map), prompt-injection \
defense via SECURITY NOTICE prefix and USER_INPUT markers, input \
length cap, OWASP categorization with regex validation.

Evaluation on six inputs: multi-model surfaces 8 OWASP-tagged findings \
vs 3 for single-model on the same corpus. Both modes hit 100% pass-1 \
schema adherence. Median runtime: 58 seconds multi vs 31 seconds \
single. We claim that the multi-model architecture provides calibrated \
confidence whereas single-model agreement is correlated noise.

Built in 24 hours for the AMD Developer Cloud Hackathon, May 2026. \
MIT licensed, open source at github.com/leonardtudor11/crucible.\
"""


async def main() -> None:
    settings = load_settings()
    personas = load_personas(Path(settings.personas_dir))
    default_model = settings.defaults.get("model", "Qwen/Qwen2.5-7B-Instruct")

    print(f"Running self-review across {len(personas)} personas...")
    async with ChatClient(settings.endpoint) as client:
        critiques = await gather_critiques(client, personas, PITCH, default_model)
        debate = await run_debate(client, personas, PITCH, critiques, default_model)
        report = await synthesize(
            client, PITCH, critiques, debate,
            model=settings.orchestration.synthesizer_model,
            temperature=settings.orchestration.synthesizer_temperature,
        )

    output_path = Path("SELF_REVIEW.md")
    output_path.write_text(render_markdown(report))
    print(
        f"SELF_REVIEW.md regenerated ({len(report.findings)} findings, "
        f"{sum(len(f.evidence) for f in report.findings)} evidence quotes, "
        f"{report.metadata.get('synthesis_pass', '?')} pass)"
    )


if __name__ == "__main__":
    asyncio.run(main())
