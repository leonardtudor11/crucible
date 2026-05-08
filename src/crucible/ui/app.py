"""Crucible Streamlit dashboard.

Run: streamlit run src/crucible/ui/app.py
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import streamlit as st

from crucible.client import ChatClient
from crucible.models import Report
from crucible.orchestrator import (
    InputTooLongError,
    MAX_INPUT_CHARS,
    gather_critiques,
    run_debate,
)
from crucible.personas import load_personas
from crucible.settings import load_settings
from crucible.synthesizer import render_markdown, synthesize


SEVERITY_COLOR = {
    "critical": "#dc2626",
    "high": "#ea580c",
    "medium": "#ca8a04",
    "low": "#65a30d",
}
CONFIDENCE_LABEL = {"high": "HIGH", "medium": "MED", "low": "LOW"}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


# ----- input-type detection ----------------------------------------------------

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
    return re.sub(r"\s+", " ", body).strip()


def fetch_url(url: str, timeout: float = 30.0, max_chars: int = 8000) -> str:
    try:
        r = httpx.get(
            url, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "Crucible/0.1"},
        )
        r.raise_for_status()
        return _strip_html(r.text)[:max_chars]
    except Exception as e:
        return f"[Failed to fetch URL: {e}]"


def prepare_input(text: str, input_type: str) -> str:
    if input_type == "url":
        url = text.strip().split("\n", 1)[0].strip()
        body = fetch_url(url)
        return (
            f"The following content was fetched from {url}.\n"
            f"Evaluate the proposal, system, or product described:\n\n{body}"
        )
    if input_type == "code":
        return (
            "The following is source code. Evaluate as a software/architecture "
            "proposal — design, security, maintainability, fit-for-purpose:\n\n"
            f"{text}"
        )
    return text


# ----- GPU snapshot via SSH ----------------------------------------------------

DROPLET_IP = "129.212.181.126"


def _ssh_json(cmd: str) -> dict:
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
         f"root@{DROPLET_IP}", cmd],
        capture_output=True, text=True, timeout=8,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return {}
    try:
        return json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        return {}


@st.cache_data(ttl=10, show_spinner=False)
def gpu_snapshot() -> dict | None:
    try:
        mem = _ssh_json("rocm-smi --showmeminfo vram --json 2>/dev/null")
        use = _ssh_json("rocm-smi --showuse --json 2>/dev/null")
        gpu_mem = mem.get("card0") or mem.get("GPU[0]") or next(iter(mem.values()), {})
        gpu_use = use.get("card0") or use.get("GPU[0]") or next(iter(use.values()), {})
        used_b = int(gpu_mem.get("VRAM Total Used Memory (B)", 0))
        total_b = int(gpu_mem.get("VRAM Total Memory (B)", 0))
        gpu_pct = gpu_use.get("GPU use (%)", "?")
        return {
            "used_gb": round(used_b / 1e9, 1),
            "total_gb": round(total_b / 1e9, 1),
            "pct": round(used_b / total_b * 100, 1) if total_b else 0,
            "gpu_use_pct": gpu_pct,
        }
    except Exception:
        return None


# ----- pipeline orchestration --------------------------------------------------

async def run_pipeline(user_input: str, input_type: str, status_holder):
    settings = load_settings()
    personas = load_personas(Path(settings.personas_dir))
    default_model = settings.defaults.get("model", "Qwen/Qwen2.5-7B-Instruct")

    status_holder.update(
        label=f"Loaded {len(personas)} personas across "
              f"{len({p.model or default_model for p in personas})} different models"
    )

    async with ChatClient(settings.endpoint) as client:
        status_holder.update(label="Round 1: 6 critiques in parallel...")
        critiques = await gather_critiques(client, personas, user_input, default_model)
        status_holder.update(label=f"Round 1 complete: {len(critiques)} critiques")

        status_holder.update(label="Round 2: cross-debate (each persona reads the others)...")
        debate = await run_debate(client, personas, user_input, critiques, default_model)
        status_holder.update(label=f"Round 2 complete: {len(debate)} responses")

        status_holder.update(label="Synthesizing — extracting findings, citing evidence...")
        report = await synthesize(
            client, user_input, critiques, debate,
            model=settings.orchestration.synthesizer_model,
            temperature=settings.orchestration.synthesizer_temperature,
            input_type=input_type,
        )
        status_holder.update(label="Synthesis complete", state="complete")
    return report


# ----- rendering ---------------------------------------------------------------

def render_finding_card(idx: int, f) -> None:
    color = SEVERITY_COLOR.get(f.severity, "#6b7280")
    conf_badge = CONFIDENCE_LABEL.get(f.confidence, "?")
    owasp = f.owasp_category or ""
    owasp_html = (
        f'<span style="background:#1e3a8a;color:white;padding:2px 8px;'
        f'border-radius:4px;font-size:0.75rem;margin-left:8px;">'
        f'OWASP {owasp}</span>'
        if owasp else ""
    )
    raised_count = len(f.raised_by)
    total = max(raised_count, 1)
    st.markdown(
        f"""
<div style="border-left:4px solid {color};padding:12px 16px;margin:8px 0;
            background:#0b1220;border-radius:4px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <div>
      <span style="background:{color};color:white;padding:2px 8px;
                   border-radius:4px;font-size:0.75rem;font-weight:600;
                   text-transform:uppercase;">{f.severity}</span>
      <span style="background:#374151;color:#e5e7eb;padding:2px 8px;
                   border-radius:4px;font-size:0.75rem;margin-left:8px;">
        {conf_badge} confidence · {raised_count}/6
      </span>
      {owasp_html}
    </div>
  </div>
  <h4 style="margin:8px 0 4px 0;color:#e5e7eb;">#{idx}. {f.title}</h4>
  <p style="margin:4px 0;color:#9ca3af;font-size:0.95rem;">{f.summary}</p>
  <p style="margin:4px 0;font-size:0.85rem;color:#9ca3af;">
    Raised by: <strong>{', '.join(f.raised_by) or '—'}</strong>
  </p>
</div>
""",
        unsafe_allow_html=True,
    )
    if f.evidence:
        with st.expander(f"Evidence ({len(f.evidence)} verbatim quote{'s' if len(f.evidence) != 1 else ''})"):
            for e in f.evidence:
                st.markdown(f"**{e.persona}** — *“{e.quote}”*")


def render_findings_tab(report: Report) -> None:
    if not report.findings:
        st.warning("No structured findings extracted. See raw critiques in other tabs.")
        return
    cols = st.columns(4)
    cols[0].metric("Findings", len(report.findings))
    cols[1].metric(
        "High-confidence",
        sum(1 for f in report.findings if f.confidence == "high"),
    )
    cols[2].metric(
        "Medium",
        sum(1 for f in report.findings if f.confidence == "medium"),
    )
    cols[3].metric(
        "OWASP-tagged",
        sum(1 for f in report.findings if f.owasp_category),
    )
    st.markdown("---")
    if report.overall_assessment:
        st.markdown("### Overall Assessment")
        st.info(report.overall_assessment)
    for i, f in enumerate(report.findings, 1):
        render_finding_card(i, f)


def render_reviewers_tab(report: Report) -> None:
    for c in report.critiques:
        with st.expander(f"**{c.persona}** — model: `{c.model or 'default'}`", expanded=False):
            st.markdown(c.content)


def render_debate_tab(report: Report) -> None:
    for t in report.debate:
        with st.expander(f"**{t.persona}** debate response — model: `{t.model or 'default'}`"):
            st.markdown(t.content)


def render_metrics_tab(report: Report, runtime_s: float) -> None:
    md = report.metadata
    cols = st.columns(4)
    cols[0].metric("Runtime", f"{runtime_s:.1f}s")
    cols[1].metric("Distinct models", md.get("distinct_model_count", 0))
    cols[2].metric("Synthesis pass", md.get("synthesis_pass", "—"))
    cols[3].metric("Schema version", md.get("schema_version", "—"))

    st.markdown("### Model panel")
    persona_models = md.get("persona_models", {})
    if persona_models:
        rows = [
            {"Persona": p, "Model": m}
            for p, m in persona_models.items()
        ]
        st.table(rows)

    st.markdown("### Run metadata")
    st.json({
        "run_id": md.get("run_id"),
        "timestamp": md.get("timestamp"),
        "synthesizer_model": md.get("synthesizer_model"),
        "input_chars": md.get("input_chars"),
        "vulnerabilities_count": len(report.vulnerabilities),
        "resilience_signals_count": len(report.resilience_signals),
    })


def render_gpu_panel() -> None:
    snap = gpu_snapshot()
    if snap is None:
        st.warning("GPU snapshot unavailable")
        return
    pct = snap["pct"]
    bar_color = "#10b981" if pct < 70 else "#f59e0b" if pct < 90 else "#ef4444"
    st.markdown(
        f"""
<div style="font-size:0.85rem;color:#9ca3af;">VRAM</div>
<div style="font-size:1.4rem;font-weight:600;color:#f9fafb;">
  {snap['used_gb']}/{snap['total_gb']} GB
</div>
<div style="height:6px;background:#1f2937;border-radius:3px;overflow:hidden;margin:4px 0;">
  <div style="width:{pct}%;height:100%;background:{bar_color};"></div>
</div>
<div style="font-size:0.75rem;color:#9ca3af;">
  {pct}% utilized · GPU use: {snap['gpu_use_pct']}%
</div>
""",
        unsafe_allow_html=True,
    )


# ----- sample inputs -----------------------------------------------------------

def load_samples() -> dict[str, str]:
    samples_dir = _project_root() / "tests" / "eval_corpus"
    if not samples_dir.exists():
        return {}
    return {
        p.stem.split("_", 1)[1].replace("_", " ").title(): p.read_text()
        for p in sorted(samples_dir.glob("*.txt"))
    }


# ----- main --------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="Crucible — Adversarial AI Review",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown(
        """
<style>
.block-container { padding-top: 2rem; max-width: 1280px; }
h1 { letter-spacing: -0.02em; }
[data-testid="stMetric"] {
    background: #0b1220;
    padding: 8px 12px;
    border-radius: 6px;
    border: 1px solid #1f2937;
}
[data-testid="stSidebar"] {
    background: #050a16;
}
[data-testid="stSidebar"] * {
    color: #d1d5db !important;
}
[data-testid="stSidebar"] strong {
    color: #f9fafb !important;
}
[data-testid="stSidebar"] [data-testid="stMetric"] {
    background: #0b1220;
}
[data-testid="stSidebar"] [data-testid="stMetricValue"] {
    font-size: 1.1rem;
    color: #f9fafb !important;
}
[data-testid="stSidebar"] [data-testid="stMetricLabel"] {
    font-size: 0.7rem;
    color: #9ca3af !important;
}
</style>
""",
        unsafe_allow_html=True,
    )

    # ----- sidebar -----
    with st.sidebar:
        st.markdown("### Crucible")
        st.caption("Multi-agent adversarial review on AMD MI300X")
        st.markdown("---")
        st.markdown("**GPU (AMD MI300X)**")
        render_gpu_panel()
        st.markdown("---")
        st.markdown("**Reviewer panel**")
        st.markdown(
            """
- **Skeptic** · Qwen2.5-7B
- **Red Teamer** · Hermes-3-Llama-8B
- **Ethics Auditor** · Falcon3-7B
- **Market Critic** · Phi-3.5-mini
- **Devil's Advocate** · Yi-1.5-9B
- **Pragmatist** · InternLM2.5-7B

Six different open-source model families.
Cross-architecture agreement scoring.
"""
        )

    # ----- header -----
    st.title("Crucible")
    st.markdown(
        "**Multi-agent adversarial review** · 6 different open-source models running in "
        "parallel on a single AMD MI300X · evidence-cited findings · cross-architecture "
        "agreement scoring."
    )

    # ----- sample inputs -----
    samples = load_samples()
    if samples:
        st.markdown("**Try a sample input:**")
        cols = st.columns(len(samples))
        for col, (name, body) in zip(cols, samples.items()):
            with col:
                if st.button(name, use_container_width=True, key=f"sample_{name}"):
                    st.session_state["input_area"] = body

    user_input = st.text_area(
        "Proposal, URL, or code to evaluate",
        height=200,
        max_chars=MAX_INPUT_CHARS,
        placeholder="Paste a business proposal, a URL (https://...), or a code snippet.",
        key="input_area",
    )

    detected = detect_input_type(user_input) if user_input.strip() else "—"
    cols = st.columns([1, 1, 1, 4])
    with cols[0]:
        st.metric("Input type", detected)
    with cols[1]:
        st.metric("Length", f"{len(user_input)} ch")
    with cols[2]:
        st.metric("Reviewers", "6")
    with cols[3]:
        st.write("")
        run = st.button(
            "Run Adversarial Review",
            type="primary",
            disabled=not user_input.strip(),
            use_container_width=True,
        )

    if run:
        prepared = prepare_input(user_input, detected)
        st.markdown("---")
        try:
            t0 = time.perf_counter()
            with st.status("Starting pipeline...", expanded=True) as status_holder:
                report = asyncio.run(
                    run_pipeline(prepared, detected, status_holder)
                )
            runtime_s = time.perf_counter() - t0

            tab1, tab2, tab3, tab4, tab5 = st.tabs(
                ["Findings", "Reviewers (Round 1)", "Cross-debate (Round 2)", "Metrics", "Markdown"]
            )
            with tab1:
                render_findings_tab(report)
            with tab2:
                render_reviewers_tab(report)
            with tab3:
                render_debate_tab(report)
            with tab4:
                render_metrics_tab(report, runtime_s)
            with tab5:
                md = render_markdown(report)
                st.code(md, language="markdown")
                st.download_button(
                    "Download Markdown report",
                    data=md,
                    file_name=f"crucible_{report.metadata.get('run_id','report')}.md",
                    mime="text/markdown",
                )
        except InputTooLongError as e:
            st.error(f"Input too long: {e}")
        except Exception as e:
            st.error(f"Pipeline failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
