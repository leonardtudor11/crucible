"""Aggregate critiques + debate into a structured Report.

Two-pass synthesis:
- Pass 1: ask the synthesizer model for the full structured JSON in one shot.
- Pass 2 (fallback): if pass 1 doesn't yield findings with reviewer
  attribution, extract themes from the corpus, then for each theme run a
  focused multiple-choice "which reviewers raised this?" call. Attribution
  calls run in parallel.

Confidence per finding is derived from inter-model agreement:
- 5/6 reviewers raised it → high
- 3/6 → medium
- 1/6 → low
(Same ratio thresholds for any persona count.)
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from datetime import datetime, timezone
from uuid import uuid4

from .client import ChatClient
from .models import Confidence, Critique, DebateTurn, Evidence, Finding, Report

SCHEMA_VERSION = "1"

TEMPLATE_DIR = Path(__file__).parent / "templates"


SYNTHESIZER_SYSTEM = """You are the synthesizer in a multi-agent adversarial review system.
You receive an original input, critiques from independent reviewers, and a
debate round where each reviewer responded to the others. Consolidate this
into a single structured JSON report.

OUTPUT EXACTLY ONE JSON OBJECT with this exact schema and no other top-level keys:

{
  "findings": [
    {
      "title": "<= 60 character finding name",
      "summary": "one-sentence description of the concern",
      "raised_by": ["ExactReviewerName1", "ExactReviewerName2"],
      "severity": "critical" | "high" | "medium" | "low",
      "owasp_category": "<OWASP code or null>",
      "evidence": [
        {"persona": "ExactReviewerName1", "quote": "verbatim sentence from their critique that supports this finding"},
        {"persona": "ExactReviewerName2", "quote": "verbatim sentence from their critique"}
      ]
    }
  ],
  "vulnerabilities": ["short bullet", "short bullet"],
  "resilience_signals": ["short bullet", "short bullet"],
  "overall_assessment": "2-3 sentence verdict"
}

CRITICAL RULES:
1. "raised_by" MUST be a non-empty array of EXACT reviewer names from the
   roster supplied in the user message. Do not invent names. Do not
   abbreviate. If a finding came from one reviewer, use a one-element array.
2. "evidence" MUST contain one item per reviewer in raised_by. The "quote"
   MUST be a verbatim substring (or close paraphrase if no direct sentence
   exists) drawn from that reviewer's critique or debate turn. Do not invent
   quotes. Keep each quote under 200 characters.
3. Merge duplicate concerns from different reviewers into ONE finding with
   multiple names in raised_by.
4. "owasp_category" should be an OWASP code only for security or integrity
   findings. Use A01-A10 for generic web/app issues, or LLM01-LLM10 for
   AI/LLM-specific issues (e.g. LLM01 = Prompt Injection, LLM02 = Insecure
   Output Handling, LLM06 = Sensitive Information Disclosure). Set to null
   for findings about ethics, market, scope, operations, or other
   non-security concerns.
5. Vulnerabilities = weaknesses or risks. Resilience signals = strengths.
6. Output the JSON object ONLY. No prose before or after, no markdown code
   fences, no comments, no extra top-level keys.

EXAMPLE OUTPUT (illustrates schema only — for an unrelated proposal):
{
  "findings": [
    {
      "title": "Untested AI accuracy claims",
      "summary": "The proposal asserts brand-aligned AI output but supplies no benchmark or A/B test data.",
      "raised_by": ["Skeptic", "Devil's Advocate"],
      "severity": "high",
      "owasp_category": null,
      "evidence": [
        {"persona": "Skeptic", "quote": "There is no evidence showing the quality of AI-generated content compared to human-created content."},
        {"persona": "Devil's Advocate", "quote": "Without comparative A/B data the value claim is asserted, not demonstrated."}
      ]
    },
    {
      "title": "Prompt injection via uploaded brand guidelines",
      "summary": "Customer-supplied text flows unchecked into the model, allowing exfiltration of other customers' templates.",
      "raised_by": ["Red Teamer"],
      "severity": "critical",
      "owasp_category": "LLM01",
      "evidence": [
        {"persona": "Red Teamer", "quote": "An attacker uploading crafted text could exfiltrate other customers' templates."}
      ]
    }
  ],
  "vulnerabilities": [
    "No human-review step before content publishes."
  ],
  "resilience_signals": [
    "Pricing falls below the SMB purchase-friction threshold."
  ],
  "overall_assessment": "Conditional. The product can ship technically, but neither content quality nor willingness-to-pay has been validated."
}"""


THEME_EXTRACTOR_SYSTEM = """You are an analyst extracting distinct concerns from a multi-reviewer critique.
Output ONLY valid JSON, no prose, no code fences."""


ATTRIBUTION_SYSTEM = """You are an attribution assistant. Given a concern and several
reviewer critiques, decide for EACH reviewer whether they raised that concern
(explicitly OR in substance — paraphrasing and indirect mentions count).
Output ONLY a JSON object mapping each reviewer name to a boolean. No prose,
no code fences, no extra keys."""


def _build_synthesizer_user_prompt(
    user_input: str,
    critiques: list[Critique],
    debate: list[DebateTurn],
) -> str:
    parts: list[str] = ["# Original input under review", user_input, ""]

    parts.append("# Round 1 — Independent critiques")
    for c in critiques:
        parts.append(f"## {c.persona}")
        parts.append(c.content)
        parts.append("")

    parts.append("# Round 2 — Cross-debate")
    for t in debate:
        parts.append(f"## {t.persona}")
        parts.append(t.content)
        parts.append("")

    parts.append("# Reviewer roster — use these EXACT names in raised_by")
    names = sorted({c.persona for c in critiques})
    parts.append(", ".join(names))
    parts.append("")
    parts.append("Now produce the JSON report.")
    return "\n".join(parts)


def _extract_json(text: str) -> dict[str, Any]:
    """Parse `text` as JSON, with progressively looser fallbacks."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _confidence_label(count: int, total: int) -> Confidence:
    if total <= 0:
        return "low"
    ratio = count / total
    if ratio >= 5 / 6:
        return "high"
    if ratio >= 3 / 6:
        return "medium"
    return "low"


def _normalize_severity(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s if s in {"critical", "high", "medium", "low"} else "medium"


_OWASP_CODE_RE = re.compile(r"^(A0[1-9]|A10|LLM0[1-9]|LLM10)$", re.IGNORECASE)


def _normalize_owasp(value: Any) -> str | None:
    if value is None or not isinstance(value, str):
        return None
    s = value.strip()
    if not s or s.lower() in {"null", "none", "n/a", "na"}:
        return None
    if not _OWASP_CODE_RE.match(s):
        return None
    return s.upper()


def _dedupe_names(names: list[Any], valid: set[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if isinstance(n, str) and n in valid and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _sort_findings(findings: list[Finding]) -> list[Finding]:
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(
        key=lambda f: (severity_order[f.severity], confidence_order[f.confidence])
    )
    return findings


def _coerce_evidence(raw_evidence: Any, valid_names: set[str]) -> list[Evidence]:
    out: list[Evidence] = []
    if not isinstance(raw_evidence, list):
        return out
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        persona = str(item.get("persona", "")).strip()
        quote = str(item.get("quote", "")).strip()
        if not persona or not quote or persona not in valid_names:
            continue
        if len(quote) > 400:
            quote = quote[:400].rstrip() + " […]"
        out.append(Evidence(persona=persona, quote=quote))
    return out


def _coerce_findings(
    raw_findings: list[Any],
    persona_names: list[str],
) -> list[Finding]:
    valid_names = set(persona_names)
    total = len(persona_names)
    out: list[Finding] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        summary = str(item.get("summary", "")).strip()
        if not title or not summary:
            continue
        raised = _dedupe_names(item.get("raised_by") or [], valid_names)
        evidence = _coerce_evidence(item.get("evidence"), valid_names)
        # Filter evidence to only include personas in raised_by.
        evidence = [e for e in evidence if e.persona in raised]
        out.append(
            Finding(
                title=title[:200],
                summary=summary,
                raised_by=raised,
                severity=_normalize_severity(item.get("severity")),  # type: ignore[arg-type]
                confidence=_confidence_label(len(raised), total),
                owasp_category=_normalize_owasp(item.get("owasp_category")),
                evidence=evidence,
            )
        )
    return _sort_findings(out)


# ----- Pass 2 (theme extraction + per-theme attribution) ---------------------


def _build_corpus(
    user_input: str,
    critiques: list[Critique],
    debate: list[DebateTurn],
) -> str:
    """Compact corpus reused by both pass-2 calls."""
    parts: list[str] = ["# Input", user_input, "", "# Critiques"]
    for c in critiques:
        parts.append(f"## {c.persona}")
        parts.append(c.content)
        parts.append("")
    parts.append("# Debate")
    for t in debate:
        parts.append(f"## {t.persona}")
        parts.append(t.content)
        parts.append("")
    return "\n".join(parts)


THEME_PROMPT_TEMPLATE = """{corpus}

From the critiques and debate above, extract the 5 to 8 most important
distinct concerns. Merge near-duplicates.

For "owasp_category": output a code like "A01" (web/app) or "LLM01" (AI/LLM)
ONLY if the concern is genuinely a security/integrity issue. Otherwise output
null. Do not output the literal phrase "OWASP code" — emit a code or null.

Output ONLY this JSON:

{{
  "themes": [
    {{
      "title": "<= 60 char name",
      "summary": "one sentence",
      "severity": "critical" | "high" | "medium" | "low",
      "owasp_category": null
    }}
  ]
}}
"""


ATTRIBUTION_PROMPT_TEMPLATE = """Concern title: {title}
Concern summary: {summary}

Below are the critiques from {n_reviewers} independent reviewers. For each
reviewer, decide whether their critique raised the above concern (explicitly
OR in substance — paraphrasing and indirect mentions count).

{critiques}

You MUST decide for EVERY reviewer. Do not omit any name. Output ONLY this
JSON, replacing each "?" with true or false (boolean lowercase, no quotes):

{template}
"""


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1"}
    return False


def _build_attribution_template(persona_names: list[str]) -> str:
    items = [f'  "{n}": ?' for n in persona_names]
    return "{\n" + ",\n".join(items) + "\n}"


async def _extract_themes(
    client: ChatClient,
    user_input: str,
    critiques: list[Critique],
    debate: list[DebateTurn],
    *,
    model: str,
    temperature: float,
) -> list[dict[str, Any]]:
    corpus = _build_corpus(user_input, critiques, debate)
    prompt = THEME_PROMPT_TEMPLATE.format(corpus=corpus)
    response = await client.chat(
        model=model,
        system=THEME_EXTRACTOR_SYSTEM,
        user=prompt,
        temperature=temperature,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )
    data = _extract_json(response)
    themes = data.get("themes") or []
    return [t for t in themes if isinstance(t, dict)]


async def _attribute_theme(
    client: ChatClient,
    theme: dict[str, Any],
    persona_names: list[str],
    critiques: list[Critique],
    *,
    model: str,
) -> list[str]:
    title = str(theme.get("title", "")).strip()
    summary = str(theme.get("summary", "")).strip()
    if not title or not summary:
        return []

    crit_blocks = "\n\n".join(
        f"## {c.persona}\n{c.content}" for c in critiques
    )
    template = _build_attribution_template(persona_names)
    prompt = ATTRIBUTION_PROMPT_TEMPLATE.format(
        title=title,
        summary=summary,
        n_reviewers=len(persona_names),
        critiques=crit_blocks,
        template=template,
    )

    try:
        response = await client.chat(
            model=model,
            system=ATTRIBUTION_SYSTEM,
            user=prompt,
            temperature=0.1,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
    except Exception:
        return []

    data = _extract_json(response)
    # Per-reviewer yes/no map — pull names whose value is truthy.
    selected = [n for n in persona_names if _is_truthy(data.get(n))]
    return selected


async def _pass_two(
    client: ChatClient,
    user_input: str,
    critiques: list[Critique],
    debate: list[DebateTurn],
    persona_names: list[str],
    *,
    model: str,
    temperature: float,
) -> list[Finding]:
    themes = await _extract_themes(
        client, user_input, critiques, debate, model=model, temperature=temperature
    )
    if not themes:
        return []

    attributions = await asyncio.gather(
        *(
            _attribute_theme(client, t, persona_names, critiques, model=model)
            for t in themes
        ),
        return_exceptions=True,
    )

    total = len(persona_names)
    out: list[Finding] = []
    for theme, attribution in zip(themes, attributions):
        if isinstance(attribution, BaseException):
            attribution = []
        title = str(theme.get("title", "")).strip()
        summary = str(theme.get("summary", "")).strip()
        if not title or not summary:
            continue
        raised = _dedupe_names(attribution or [], set(persona_names))
        out.append(
            Finding(
                title=title[:200],
                summary=summary,
                raised_by=raised,
                severity=_normalize_severity(theme.get("severity")),  # type: ignore[arg-type]
                confidence=_confidence_label(len(raised), total),
                owasp_category=_normalize_owasp(theme.get("owasp_category")),
            )
        )
    return _sort_findings(out)


# ----- Public entry points ---------------------------------------------------


def _has_attribution(findings: list[Finding]) -> bool:
    return any(len(f.raised_by) > 0 for f in findings)


async def synthesize(
    client: ChatClient,
    user_input: str,
    critiques: list[Critique],
    debate: list[DebateTurn],
    *,
    model: str,
    temperature: float = 0.3,
    input_type: str = "business",
) -> Report:
    user_prompt = _build_synthesizer_user_prompt(user_input, critiques, debate)

    # PASS 1
    raw = await client.chat(
        model=model,
        system=SYNTHESIZER_SYSTEM,
        user=user_prompt,
        temperature=temperature,
        max_tokens=2048,
        response_format={"type": "json_object"},
    )
    parsed = _extract_json(raw)
    persona_names = [c.persona for c in critiques]

    findings = _coerce_findings(parsed.get("findings") or [], persona_names)
    pass_used = "pass1"

    # PASS 2 fallback: pass 1 returned no findings, OR returned findings with
    # zero attribution (the "wrong-schema" case where confidence can't be
    # computed).
    if not findings or not _has_attribution(findings):
        try:
            pass_two_findings = await _pass_two(
                client,
                user_input,
                critiques,
                debate,
                persona_names,
                model=model,
                temperature=temperature,
            )
        except Exception:
            pass_two_findings = []
        if pass_two_findings:
            findings = pass_two_findings
            pass_used = "pass2"

    vulnerabilities = [
        str(v).strip()
        for v in (parsed.get("vulnerabilities") or [])
        if str(v).strip()
    ]
    resilience_signals = [
        str(v).strip()
        for v in (parsed.get("resilience_signals") or [])
        if str(v).strip()
    ]
    overall = str(parsed.get("overall_assessment", "")).strip()

    if not findings and not overall and not vulnerabilities:
        overall = (
            "Synthesizer did not return parseable structured output after two "
            "passes. The raw synthesizer response and the per-reviewer "
            "critiques below remain the source of truth for this run."
        )

    persona_models = {
        c.persona: c.model for c in critiques if c.model is not None
    }
    distinct_models = sorted(set(persona_models.values()))

    return Report(
        input=user_input,
        input_type=input_type,
        critiques=critiques,
        debate=debate,
        findings=findings,
        vulnerabilities=vulnerabilities,
        resilience_signals=resilience_signals,
        overall_assessment=overall,
        synthesis_raw=raw,
        metadata={
            "schema_version": SCHEMA_VERSION,
            "run_id": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "synthesizer_model": model,
            "synthesis_pass": pass_used,
            "persona_models": persona_models,
            "distinct_models": distinct_models,
            "distinct_model_count": len(distinct_models),
            "input_chars": len(user_input),
        },
    )


def render_markdown(report: Report) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("report.md.j2")
    return template.render(report=report)
