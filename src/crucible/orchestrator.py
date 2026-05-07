"""Run personas in parallel, then conduct one cross-debate round."""
from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable

from .client import ChatClient
from .models import Critique, DebateTurn, PersonaConfig

# (event, persona_name) — events: critique_start, critique_done,
#                                  debate_start, debate_done
ProgressCallback = Callable[[str, str], Awaitable[None] | None]


async def _emit(cb: ProgressCallback | None, event: str, name: str) -> None:
    if cb is None:
        return
    result = cb(event, name)
    if inspect.isawaitable(result):
        await result


async def _one_critique(
    client: ChatClient,
    persona: PersonaConfig,
    user_input: str,
    default_model: str,
    progress: ProgressCallback | None,
) -> Critique:
    await _emit(progress, "critique_start", persona.name)
    model = persona.model or default_model
    content = await client.chat(
        model=model,
        system=persona.system_prompt,
        user=user_input,
        temperature=persona.temperature,
        max_tokens=persona.max_tokens,
    )
    await _emit(progress, "critique_done", persona.name)
    return Critique(persona=persona.name, content=content)


async def gather_critiques(
    client: ChatClient,
    personas: list[PersonaConfig],
    user_input: str,
    default_model: str,
    progress: ProgressCallback | None = None,
) -> list[Critique]:
    """Round 1: each persona critiques the input independently and in parallel."""
    tasks = [
        _one_critique(client, p, user_input, default_model, progress)
        for p in personas
    ]
    return await asyncio.gather(*tasks)


def _format_others(critiques: list[Critique], exclude: str) -> str:
    others = [c for c in critiques if c.persona != exclude]
    return "\n\n".join(f"## {c.persona} said\n{c.content}" for c in others)


async def _one_debate_turn(
    client: ChatClient,
    persona: PersonaConfig,
    user_input: str,
    critiques: list[Critique],
    default_model: str,
    progress: ProgressCallback | None,
) -> DebateTurn:
    await _emit(progress, "debate_start", persona.name)
    others_text = _format_others(critiques, persona.name)
    debate_prompt = (
        f"# Original input under review\n{user_input}\n\n"
        f"# What other reviewers said\n{others_text}\n\n"
        f"# Your task\n"
        f"You are still {persona.name}. Refine your position in light of the "
        f"other reviewers. Where do you concede ground? Where do you hold firm "
        f"and why? What did the others miss? Be concrete and concise — under "
        f"250 words."
    )
    model = persona.model or default_model
    content = await client.chat(
        model=model,
        system=persona.system_prompt,
        user=debate_prompt,
        temperature=persona.temperature,
        max_tokens=persona.max_tokens,
    )
    references = [c.persona for c in critiques if c.persona != persona.name]
    await _emit(progress, "debate_done", persona.name)
    return DebateTurn(persona=persona.name, content=content, references=references)


async def run_debate(
    client: ChatClient,
    personas: list[PersonaConfig],
    user_input: str,
    critiques: list[Critique],
    default_model: str,
    progress: ProgressCallback | None = None,
) -> list[DebateTurn]:
    """Round 2: each persona reads the others' critiques and refines its position."""
    tasks = [
        _one_debate_turn(client, p, user_input, critiques, default_model, progress)
        for p in personas
    ]
    return await asyncio.gather(*tasks)
