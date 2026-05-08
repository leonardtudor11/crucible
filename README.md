# Crucible

**Multi-agent adversarial review running on a single AMD MI300X.** Six
different open-source LLM families critique your proposal, system, or
code in parallel; cross-architecture agreement determines confidence.
Every finding is backed by verbatim evidence quotes from the reviewer
that raised it.

Built for the AMD Developer Cloud Hackathon (May 2026).

![Crucible dashboard showing findings with severity, confidence, and overall assessment](screenshots/03_findings_with_evidence.png)

---

## The 30-second pitch

> AI judging AI is everywhere now. The problem is *single-model judging*:
> when one model evaluates from "six perspectives" via prompt engineering,
> agreement is just that model's prior repeated six times. Crucible
> replaces that with **architectural diversity** — six different
> open-source families (Qwen, Phi, Falcon, Hermes/Llama, InternLM, Yi)
> serving from one MI300X. When 4 of 6 *different architectures*
> independently flag the same risk, that's a real cross-validation.

## Architecture

```
                                              AMD MI300X (192 GB VRAM)
                                              ┌──────────────────────────┐
                                              │  vLLM × 6 containers     │
   Mac (SSH tunnel)                           │  127.0.0.1:8001-8006     │
   ┌──────────┐    ┌──────────────────────┐   │  ─────────────────────   │
   │ Crucible │ ── │ LiteLLM proxy        │ ─►│  :8001 Qwen2.5-7B        │
   │ Streamlit│    │ 127.0.0.1:8000       │   │  :8002 Phi-3.5-mini      │
   │ + CLI    │    │ routes by model name │ ─►│  :8003 Falcon3-7B        │
   └──────────┘    └──────────────────────┘   │  :8004 Hermes-3-Llama-8B │
                                              │  :8005 InternLM2.5-7B    │
                                              │  :8006 Yi-1.5-9B         │
                                              └──────────────────────────┘
```

### Pipeline

```
input ─► Round 1: 6 critiques in parallel (asyncio.gather, each persona on its own model)
       ─► Round 2: cross-debate (each persona reads the others, refines its take)
       ─► Synthesis: structured JSON {findings, evidence, vulnerabilities, ...}
       ─► Confidence scoring: 5-6/6 reviewers ⇒ high · 3-4/6 ⇒ medium · 1-2/6 ⇒ low
```

## Personas → models (6 distinct families)

| Persona            | Role                                                | Model |
|--------------------|-----------------------------------------------------|-------|
| Skeptic            | Demands evidence, attacks unsupported claims        | Qwen2.5-7B-Instruct |
| Red Teamer         | Hunts exploitation, abuse, adversarial misuse       | NousResearch/Hermes-3-Llama-3.1-8B |
| Ethics Auditor     | Surfaces harms, bias, fairness gaps, dual-use       | tiiuae/Falcon3-7B-Instruct |
| Market Critic      | Commercial viability, positioning, sustainability   | microsoft/Phi-3.5-mini-instruct |
| Devil's Advocate   | Steelmans the strongest counter-position            | 01-ai/Yi-1.5-9B-Chat |
| Pragmatist         | Scope, complexity, day-90 operational reality       | internlm/internlm2_5-7b-chat |

## Eval numbers (from `eval_results/`)

6 inputs (SaaS, healthcare AI, fintech DeFi, insecure auth code,
prompt-injection attempt, Romanian-language proposal).

| Metric | Multi-model (6 families) | Single-model (Qwen-7B ×6) |
|---|---|---|
| Median runtime | 58.5 s | 30.8 s |
| Pass-1 schema success | 100% | 100% |
| Median findings/run | 4.5 | 4.5 |
| Median evidence quotes/run | 10.0 | 10.5 |
| **OWASP-tagged findings (total)** | **8** | **3** |
| Medium-confidence findings (total) | 12 | 6 |

**Architectural diversity catches more security signal.** Multi-model
flags 2.7× more OWASP-tagged findings than single-model on the same
corpus — different architectures have different security blind spots,
and aggregating six families catches what any single model misses.

**Confidence numbers are not directly comparable.** Six instances of
Qwen-7B "agreeing" with itself is a single prior repeated; six different
architectures landing on the same finding is independent cross-validation.
The medium-confidence totals shift run-to-run with sampling variance —
what matters is the *kind* of agreement, not the specific count.

Both modes hit a **100% pass-1 schema success rate** — the two-pass
synthesizer design (strict JSON + few-shot in pass 1, theme-extraction
with per-reviewer yes/no attribution in pass 2) means the pipeline stays
robust if a future small model breaks JSON.

![Eval comparison page in the dashboard](screenshots/02_comparison_page.png)

The dashboard also has a **live A/B mode** — one click runs both modes
on the same input sequentially and renders findings side-by-side, so
judges can see calibration on whatever input they paste.

![Live A/B comparison — multi-model vs single-model on the same input](screenshots/05_ab_compare.png)

## Trust pack

Built into every report:

| Feature | What it does |
|---|---|
| **Evidence citation** | Each finding emits one verbatim quote per reviewer in `raised_by` — auditable claims, falsifiable attribution. |
| **Self-disclosure metadata** | `run_id`, ISO timestamp, schema version, per-persona model map, distinct-model count, synthesizer model + pass — every report says exactly what produced it. |
| **Prompt-injection defense** | Every persona prompt prefixed with a SECURITY NOTICE; user input wrapped in `<<USER_INPUT>>` markers. Verified resistant to "Ignore previous instructions" attacks (input #5 in corpus). |
| **Input length cap** | Hard 20k char limit raises `InputTooLongError` before tokens are spent. |
| **OWASP categorization** | Security findings get a strict OWASP code (A01-A10 web/app, LLM01-LLM10 LLM-specific). Regex-validated to reject placeholder strings. |

## Quick start

### On the MI300X droplet

```bash
# 6 vLLM containers on ports 8001-8006, LiteLLM proxy on 8000
# (see ops/ for the launch commands; LiteLLM config at /shared-docker/litellm/config.yaml)
docker ps    # should show 7 containers: vllm_qwen, vllm_phi, vllm_falcon,
             # vllm_hermes, vllm_internlm, vllm_yi, litellm
```

### From your laptop

```bash
git clone https://github.com/leonardtudor11/crucible
cd crucible
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Tunnel to the MI300X
ssh -L 8000:localhost:8000 -N root@<droplet-ip> &

# Configure
cp .env.example .env
# Set CRUCIBLE_API_BASE=http://localhost:8000/v1
# Set CRUCIBLE_API_KEY=<litellm-master-key>

# Run
streamlit run src/crucible/ui/app.py    # dashboard
python scripts/run_demo.py "<your proposal>"   # CLI
python scripts/eval.py                   # corpus eval (multi-model)
python scripts/eval.py --single-model    # baseline comparison
```

## Project layout

```
crucible/
├── config/
│   ├── settings.yaml              endpoint, models, orchestration defaults
│   └── personas/*.yaml            6 persona definitions with model: field
├── src/crucible/
│   ├── client.py                  async OpenAI-compatible chat client
│   ├── settings.py                YAML + env-var loader
│   ├── personas.py                YAML → PersonaConfig
│   ├── orchestrator.py            parallel critiques + cross-debate;
│   │                              injection guard + length cap
│   ├── synthesizer.py             two-pass synthesis: strict JSON pass 1,
│   │                              theme + per-reviewer yes/no fallback;
│   │                              evidence + confidence scoring
│   ├── models.py                  Pydantic: Finding, Evidence, Report
│   ├── templates/report.md.j2     Jinja2 markdown template
│   └── ui/app.py                  Streamlit dashboard with eval comparison
├── scripts/
│   ├── run_demo.py                CLI entry point
│   └── eval.py                    corpus evaluator (multi vs single mode)
├── tests/eval_corpus/             5 inputs (4 real + 1 adversarial)
└── eval_results/                  per-run aggregates JSON
```

## Output schema (`Finding`)

```json
{
  "title": "Untested AI accuracy claims",
  "summary": "The proposal asserts brand-aligned AI output but supplies no benchmark.",
  "raised_by": ["Skeptic", "Devil's Advocate"],
  "severity": "high",
  "confidence": "medium",
  "owasp_category": null,
  "evidence": [
    {"persona": "Skeptic", "quote": "There is no evidence showing the quality of AI-generated content compared to human-created content."},
    {"persona": "Devil's Advocate", "quote": "Without comparative A/B data the value claim is asserted, not demonstrated."}
  ]
}
```

## License

MIT — see [LICENSE](LICENSE).
