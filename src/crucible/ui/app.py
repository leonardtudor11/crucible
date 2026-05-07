"""Streamlit UI. Entry point: `streamlit run src/crucible/ui/app.py`."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
import streamlit as st

from crucible.client import ChatClient
from crucible.orchestrator import gather_critiques, run_debate
from crucible.personas import load_personas
from crucible.settings import load_settings
from crucible.synthesizer import render_markdown, synthesize


def detect_input_type(text: str) -> str:
    text = text.strip()
    if not text:
        return "business"

    first_line = text.split("\n", 1)[0].strip()
    if "\n" not in text:
        parsed = urlparse(first_line)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return "url"

    code_signals = 0
    if re.search(r"^```", text, re.MULTILINE):
        code_signals += 2
    if re.search(r"^\s*(import|from|#include|package|using)\s", text, re.MULTILINE):
        code_signals += 1
    if re.search(r"\b(def|class|function|const|let|var|return)\b\s+\w", text):
        code_signals += 1
    if re.search(r"[{};]\s*$", text, re.MULTILINE):
        code_signals += 1
    if code_signals >= 2:
        return "code"
    return "business"


def _strip_html(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<style.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body)
    return body.strip()


def fetch_url(url: str, timeout: float = 30.0, max_chars: int = 8000) -> str:
    try:
        r = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Crucible/0.1 (adversarial review bot)"},
        )
        r.raise_for_status()
        text = _strip_html(r.text)
        return text[:max_chars]
    except Exception as e:
        return f"[Failed to fetch URL: {e}]"


def prepare_input(text: str, input_type: str) -> str:
    if input_type == "url":
        url = text.strip().split("\n", 1)[0].strip()
        body = fetch_url(url)
        return (
            f"The following content was fetched from {url}.\n"
            f"Evaluate the proposal, system, or product described:\n\n"
            f"{body}"
        )
    if input_type == "code":
        return (
            "The following is source code. Evaluate it as a software/architecture "
            "proposal — design, security, maintainability, fit-for-purpose:\n\n"
            f"{text}"
        )
    return text


async def run_pipeline(
    user_input: str,
    input_type: str,
    progress_log: list[str],
):
    settings = load_settings()
    personas = load_personas(Path(settings.personas_dir))
    default_model = settings.defaults.get("model", "tinyllama")

    progress_log.append(
        f"Loaded {len(personas)} personas: "
        + ", ".join(p.name for p in personas)
    )
    progress_log.append(f"Endpoint: {settings.endpoint.base_url}")
    progress_log.append(f"Default model: {default_model}")

    async def progress(event: str, name: str) -> None:
        progress_log.append(f"  {event}: {name}")

    async with ChatClient(settings.endpoint) as client:
        progress_log.append("Round 1: gathering critiques in parallel...")
        critiques = await gather_critiques(
            client, personas, user_input, default_model, progress=progress
        )
        progress_log.append(f"Round 1 complete ({len(critiques)} critiques)")

        progress_log.append("Round 2: cross-debate in parallel...")
        debate = await run_debate(
            client,
            personas,
            user_input,
            critiques,
            default_model,
            progress=progress,
        )
        progress_log.append(f"Round 2 complete ({len(debate)} responses)")

        progress_log.append("Synthesizing report...")
        report = await synthesize(
            client,
            user_input,
            critiques,
            debate,
            model=settings.orchestration.synthesizer_model,
            temperature=settings.orchestration.synthesizer_temperature,
            input_type=input_type,
        )
        progress_log.append("Synthesis complete.")
    return report


def main() -> None:
    st.set_page_config(page_title="Crucible", layout="wide")
    st.title("Crucible — Adversarial Review")
    st.caption("Multi-agent stress test for AI systems and proposals.")

    user_input = st.text_area(
        "Proposal, URL, or code to evaluate",
        height=240,
        placeholder=(
            "Paste a business proposal, a URL (https://...), or a code snippet. "
            "Crucible will detect the type and run 6 adversarial reviewers in "
            "parallel."
        ),
    )

    detected = detect_input_type(user_input) if user_input.strip() else "—"
    cols = st.columns([1, 1, 4])
    with cols[0]:
        st.metric("Detected input type", detected)
    with cols[2]:
        st.write("")
        run = st.button(
            "Test",
            type="primary",
            disabled=not user_input.strip(),
            use_container_width=True,
        )

    if run:
        prepared = prepare_input(user_input, detected)
        progress_log: list[str] = []
        try:
            with st.spinner(
                "Running adversarial review — this can take 1–3 minutes on tinyllama..."
            ):
                report = asyncio.run(
                    run_pipeline(prepared, detected, progress_log)
                )
            st.success("Review complete.")
            with st.expander("Pipeline log", expanded=False):
                st.code("\n".join(progress_log), language=None)

            md = render_markdown(report)
            st.markdown(md)
            st.download_button(
                "Download report (Markdown)",
                data=md,
                file_name="crucible_report.md",
                mime="text/markdown",
            )
        except Exception as e:
            st.error(f"Pipeline failed: {e}")
            if progress_log:
                with st.expander("Pipeline log", expanded=True):
                    st.code("\n".join(progress_log), language=None)


if __name__ == "__main__":
    main()
