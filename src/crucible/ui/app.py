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

SINGLE_MODEL_BASELINE = "Qwen/Qwen2.5-7B-Instruct"


async def run_pipeline(
    user_input: str, input_type: str, set_phase,
    single_model: bool = False,
):
    """Run the full pipeline. `set_phase(text)` is called as the
    pipeline advances so the UI can display a single rolling status line.
    """
    settings = load_settings()
    personas = load_personas(Path(settings.personas_dir))
    if single_model:
        for p in personas:
            p.model = SINGLE_MODEL_BASELINE
    default_model = settings.defaults.get("model", SINGLE_MODEL_BASELINE)

    distinct = len({p.model or default_model for p in personas})
    mode_label = "single-model baseline" if single_model else f"{distinct}-model panel"

    async with ChatClient(settings.endpoint) as client:
        set_phase(f"{mode_label} — Round 1: 6 critiques in parallel...")
        critiques = await gather_critiques(client, personas, user_input, default_model)

        set_phase(f"{mode_label} — Round 2: cross-debate ({len(critiques)} reviewers)...")
        debate = await run_debate(client, personas, user_input, critiques, default_model)

        set_phase(f"{mode_label} — Synthesizing findings + evidence...")
        report = await synthesize(
            client, user_input, critiques, debate,
            model=settings.orchestration.synthesizer_model,
            temperature=settings.orchestration.synthesizer_temperature,
            input_type=input_type,
        )
    return report


# ----- rendering ---------------------------------------------------------------

def render_finding_card(idx: int, f) -> None:
    color = SEVERITY_COLOR.get(f.severity, "#6b7280")
    conf_badge = CONFIDENCE_LABEL.get(f.confidence, "?")
    owasp = f.owasp_category or ""
    owasp_html = (
        f'<span style="background:#1e3a8a;color:white;padding:2px 8px;border-radius:4px;font-size:0.75rem;margin-left:8px;">OWASP {owasp}</span>'
        if owasp else ""
    )
    raised_count = len(f.raised_by)
    raised_str = ", ".join(f.raised_by) or "—"
    # Single-line HTML to avoid markdown's 4-space-indent code-block rule
    # escaping nested </div> tags.
    html = (
        f'<div style="border-left:4px solid {color};padding:14px 16px;margin:14px 0;background:#0b1220;border-radius:6px;">'
        f'<div>'
        f'<span style="background:{color};color:white;padding:3px 10px;border-radius:4px;font-size:0.75rem;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;">{f.severity}</span>'
        f'<span style="background:#374151;color:#e5e7eb;padding:3px 10px;border-radius:4px;font-size:0.75rem;margin-left:8px;">{conf_badge} confidence · {raised_count}/6</span>'
        f'{owasp_html}'
        f'</div>'
        f'<h4 style="margin:10px 0 4px 0;color:#e5e7eb;font-size:1.05rem;">#{idx}. {f.title}</h4>'
        f'<p style="margin:6px 0;color:#cbd5e1;font-size:0.95rem;line-height:1.5;">{f.summary}</p>'
        f'<p style="margin:6px 0 0 0;font-size:0.8rem;color:#9ca3af;">Raised by: <strong style="color:#e5e7eb;">{raised_str}</strong></p>'
        f'</div>'
    )
    st.markdown(html, unsafe_allow_html=True)
    if f.evidence:
        plural = "s" if len(f.evidence) != 1 else ""
        with st.expander(f"Evidence ({len(f.evidence)} verbatim quote{plural})"):
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
    html = (
        f'<div style="font-size:0.85rem;color:#9ca3af;">VRAM</div>'
        f'<div style="font-size:1.4rem;font-weight:600;color:#f9fafb;">{snap["used_gb"]}/{snap["total_gb"]} GB</div>'
        f'<div style="height:6px;background:#1f2937;border-radius:3px;overflow:hidden;margin:4px 0;">'
        f'<div style="width:{pct}%;height:100%;background:{bar_color};"></div>'
        f'</div>'
        f'<div style="font-size:0.75rem;color:#9ca3af;">{pct}% utilized · GPU use: {snap["gpu_use_pct"]}%</div>'
    )
    st.markdown(html, unsafe_allow_html=True)


# ----- sample inputs -----------------------------------------------------------

def load_samples() -> dict[str, str]:
    samples_dir = _project_root() / "tests" / "eval_corpus"
    if not samples_dir.exists():
        return {}
    return {
        p.stem.split("_", 1)[1].replace("_", " ").title(): p.read_text()
        for p in sorted(samples_dir.glob("*.txt"))
    }


def load_latest_eval(mode: str) -> dict | None:
    eval_dir = _project_root() / "eval_results"
    if not eval_dir.exists():
        return None
    files = sorted(eval_dir.glob(f"{mode}_*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except Exception:
        return None


def render_comparison_page() -> None:
    multi = load_latest_eval("multi")
    single = load_latest_eval("single")
    if not multi or not single:
        st.warning(
            "Eval results not found. Run `python scripts/eval.py` and "
            "`python scripts/eval.py --single-model`."
        )
        return

    st.markdown("### Multi-model vs Single-model — same corpus, same prompts")
    st.caption(
        "All 6 personas, 5 inputs (SaaS, healthcare, fintech, code, "
        "adversarial). Single-model = every persona pinned to "
        "Qwen2.5-7B-Instruct. Multi-model = each persona on a different "
        "open-source family."
    )

    multi_agg = multi.get("aggregate", {})
    single_agg = single.get("aggregate", {})

    rows = [
        ("Median runtime (s)", multi_agg.get("median_total_s"), single_agg.get("median_total_s")),
        ("p95 runtime (s)", multi_agg.get("p95_total_s"), single_agg.get("p95_total_s")),
        ("Pass-1 success rate", multi_agg.get("pass1_rate"), single_agg.get("pass1_rate")),
        ("Median findings/run", multi_agg.get("median_findings"), single_agg.get("median_findings")),
        ("Median evidence/run", multi_agg.get("median_evidence_per_run"), single_agg.get("median_evidence_per_run")),
        ("OWASP-tagged total", multi_agg.get("owasp_tagged_total"), single_agg.get("owasp_tagged_total")),
        ("Medium-conf findings (total)", multi_agg.get("medium_confidence_findings_total"), single_agg.get("medium_confidence_findings_total")),
        ("High-conf findings (total)", multi_agg.get("high_confidence_findings_total"), single_agg.get("high_confidence_findings_total")),
    ]
    st.table([
        {"Metric": m, "Multi-model": v1, "Single-model": v2}
        for m, v1, v2 in rows
    ])

    st.markdown("### What this comparison tells you")
    multi_owasp = multi_agg.get("owasp_tagged_total", 0)
    single_owasp = single_agg.get("owasp_tagged_total", 0)
    multi_med = multi_agg.get("medium_confidence_findings_total", 0)
    single_med = single_agg.get("medium_confidence_findings_total", 0)
    st.info(
        f"**Architectural diversity surfaces more security signal.** "
        f"Multi-model OWASP-tagged: **{multi_owasp}**, single-model: **{single_owasp}**. "
        f"Different architectures have different security blind spots — aggregating six "
        f"families catches what any single model misses.\n\n"
        f"**Confidence numbers are not directly comparable.** Single-model "
        f"'agreement' is one prior repeated six times — it should not be read "
        f"as cross-validation. Multi-model agreement is six independent "
        f"architectures landing on the same finding — that IS cross-validation. "
        f"This run: multi medium-conf={multi_med}, single medium-conf={single_med}. "
        f"The numbers shift run-to-run; what matters is the *kind* of agreement."
    )

    with st.expander("Per-input details"):
        cols = st.columns(2)
        with cols[0]:
            st.markdown("**Multi-model**")
            for r in multi.get("per_input", []):
                if r.get("error"):
                    continue
                st.text(
                    f"{r['input_file']}: {r['total_s']}s · "
                    f"{r['findings_count']} findings · "
                    f"H/M/L {r['confidence']['high']}/{r['confidence']['medium']}/{r['confidence']['low']}"
                )
        with cols[1]:
            st.markdown("**Single-model**")
            for r in single.get("per_input", []):
                if r.get("error"):
                    continue
                st.text(
                    f"{r['input_file']}: {r['total_s']}s · "
                    f"{r['findings_count']} findings · "
                    f"H/M/L {r['confidence']['high']}/{r['confidence']['medium']}/{r['confidence']['low']}"
                )


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
        page = st.radio(
            "View",
            ["Run review", "Multi vs Single (eval)"],
            label_visibility="collapsed",
        )
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

    if page == "Multi vs Single (eval)":
        st.title("Crucible — Eval Comparison")
        render_comparison_page()
        return

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
    type_color = {
        "business": "#2563eb",
        "code": "#7c3aed",
        "url": "#0891b2",
        "—": "#6b7280",
    }.get(detected, "#6b7280")

    chips_html = (
        f'<div style="display:flex;gap:12px;align-items:center;margin:8px 0;">'
        f'<span style="background:{type_color};color:white;padding:6px 14px;'
        f'border-radius:6px;font-size:0.85rem;font-weight:600;'
        f'text-transform:uppercase;letter-spacing:0.05em;">{detected}</span>'
        f'<span style="background:#1f2937;color:#d1d5db;padding:6px 14px;'
        f'border-radius:6px;font-size:0.85rem;">'
        f'<strong>{len(user_input):,}</strong> chars · max {MAX_INPUT_CHARS:,}</span>'
        f'<span style="background:#1f2937;color:#d1d5db;padding:6px 14px;'
        f'border-radius:6px;font-size:0.85rem;">'
        f'<strong>6</strong> reviewers · 6 model families</span>'
        f'</div>'
    )
    st.markdown(chips_html, unsafe_allow_html=True)
    cols = st.columns([2, 2, 1])
    with cols[0]:
        run = st.button(
            "Run multi-model review (6 architectures)",
            type="primary",
            disabled=not user_input.strip(),
            use_container_width=True,
        )
    with cols[1]:
        run_both = st.button(
            "Run BOTH (multi + single baseline) — A/B",
            disabled=not user_input.strip(),
            use_container_width=True,
        )
    with cols[2]:
        run_single = st.button(
            "Single-model only",
            disabled=not user_input.strip(),
            use_container_width=True,
        )

    def _run(single: bool, label: str):
        phase_box = st.empty()

        def set_phase(text: str) -> None:
            phase_box.info(text)

        try:
            t0 = time.perf_counter()
            set_phase(f"{label}: starting...")
            rpt = asyncio.run(
                run_pipeline(prepared, detected, set_phase, single_model=single)
            )
            elapsed = time.perf_counter() - t0
            phase_box.success(f"{label} complete in {elapsed:.1f}s")
            return rpt, elapsed
        except InputTooLongError as e:
            phase_box.error(f"Input too long: {e}")
        except Exception as e:
            phase_box.error(f"{label} failed: {type(e).__name__}: {e}")
        return None, 0.0

    if run or run_single or run_both:
        prepared = prepare_input(user_input, detected)
        # Clear previous a/b state
        st.session_state.pop("ab_multi", None)
        st.session_state.pop("ab_single", None)

        if run:
            report, runtime_s = _run(single=False, label="Multi-model review")
            if report:
                st.session_state["last_report"] = report
                st.session_state["last_runtime_s"] = runtime_s
        elif run_single:
            report, runtime_s = _run(single=True, label="Single-model baseline")
            if report:
                st.session_state["last_report"] = report
                st.session_state["last_runtime_s"] = runtime_s
        elif run_both:
            multi_report, multi_t = _run(single=False, label="Multi-model review")
            single_report, single_t = _run(single=True, label="Single-model baseline")
            if multi_report and single_report:
                st.session_state["ab_multi"] = (multi_report, multi_t)
                st.session_state["ab_single"] = (single_report, single_t)
                # Clear single-report view to favour A/B view
                st.session_state.pop("last_report", None)

    # ----- A/B side-by-side render (when both modes were run) -----
    ab_multi = st.session_state.get("ab_multi")
    ab_single = st.session_state.get("ab_single")
    if ab_multi and ab_single:
        m_rpt, m_t = ab_multi
        s_rpt, s_t = ab_single
        st.markdown("---")
        st.markdown("## A/B comparison — multi-model vs single-model on this input")
        cols = st.columns(2, gap="large")
        with cols[0]:
            st.markdown(f"### Multi-model panel ({m_rpt.metadata.get('distinct_model_count', 0)} families)")
            cc = st.columns(4)
            cc[0].metric("Runtime", f"{m_t:.1f}s")
            cc[1].metric("Findings", len(m_rpt.findings))
            cc[2].metric("Medium-conf", sum(1 for f in m_rpt.findings if f.confidence == "medium"))
            cc[3].metric("OWASP", sum(1 for f in m_rpt.findings if f.owasp_category))
            st.info(m_rpt.overall_assessment or "—")
            for i, f in enumerate(m_rpt.findings, 1):
                render_finding_card(i, f)
        with cols[1]:
            st.markdown("### Single-model baseline (Qwen-7B ×6)")
            cc = st.columns(4)
            cc[0].metric("Runtime", f"{s_t:.1f}s")
            cc[1].metric("Findings", len(s_rpt.findings))
            cc[2].metric("Medium-conf", sum(1 for f in s_rpt.findings if f.confidence == "medium"))
            cc[3].metric("OWASP", sum(1 for f in s_rpt.findings if f.owasp_category))
            st.info(s_rpt.overall_assessment or "—")
            for i, f in enumerate(s_rpt.findings, 1):
                render_finding_card(i, f)

        # Calibration callout below
        m_med = sum(1 for f in m_rpt.findings if f.confidence == "medium")
        s_med = sum(1 for f in s_rpt.findings if f.confidence == "medium")
        st.markdown("---")
        st.warning(
            f"**Calibration check:** single-model claims {s_med} medium-confidence "
            f"findings vs multi-model's {m_med}. The single-model count is the same "
            f"Qwen prior repeated 6 times. Multi-model agreement requires "
            f"{m_rpt.metadata.get('distinct_model_count', 0)} different architectures "
            f"to independently flag the same risk — a stronger signal."
        )
        return

    # Render last report (persists across reruns until a new one is produced)
    last_report = st.session_state.get("last_report")
    last_runtime = st.session_state.get("last_runtime_s", 0)
    if last_report is not None:
        st.markdown("---")
        tab1, tab2, tab3, tab4, tab5 = st.tabs(
            ["Findings", "Reviewers (Round 1)", "Cross-debate (Round 2)", "Metrics", "Markdown"]
        )
        with tab1:
            render_findings_tab(last_report)
        with tab2:
            render_reviewers_tab(last_report)
        with tab3:
            render_debate_tab(last_report)
        with tab4:
            render_metrics_tab(last_report, last_runtime)
        with tab5:
            md = render_markdown(last_report)
            st.code(md, language="markdown")
            st.download_button(
                "Download Markdown report",
                data=md,
                file_name=f"crucible_{last_report.metadata.get('run_id', 'report')}.md",
                mime="text/markdown",
            )


if __name__ == "__main__":
    main()
