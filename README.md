# Crucible

Multi-agent adversarial stress-testing engine. Sends a proposal, URL, or
code snippet to 6 LLM personas in parallel, runs one cross-debate round,
and synthesizes a structured report scored by inter-model agreement.

## What it's for

You have a business proposal, a system design, or a piece of code. You
want an honest red-team review *before* you spend money or ship. Crucible
runs six small models in parallel, each playing a distinct adversarial
role, and reports which concerns multiple reviewers raised independently
(high-confidence findings) versus one-off objections (low-confidence).

## Architecture

```
                  ┌─────────────────────────────────────────┐
input ──────────► │ Round 1 — Independent critiques         │
                  │ (asyncio.gather, 6 in parallel)         │
                  │                                         │
                  │ Skeptic        Red Teamer    Pragmatist │
                  │ Ethics Auditor Market Critic Devil's A. │
                  └────────────────┬────────────────────────┘
                                   │
                                   ▼
                  ┌─────────────────────────────────────────┐
                  │ Round 2 — Cross-debate                  │
                  │ Each persona reads the others' takes,   │
                  │ refines its position. Parallel again.   │
                  └────────────────┬────────────────────────┘
                                   │
                                   ▼
                  ┌─────────────────────────────────────────┐
                  │ Synthesizer (two-pass)                  │
                  │ Pass 1: full structured JSON in one go  │
                  │ Pass 2 fallback: extract themes, then   │
                  │   per-reviewer yes/no attribution       │
                  │   (parallel). Avoids position bias on   │
                  │   smaller models.                       │
                  └────────────────┬────────────────────────┘
                                   │
                                   ▼
                          Markdown report
            (findings table, confidence, OWASP tags, transcripts)
```

## Install

Requires Python 3.11+ and an OpenAI-compatible chat endpoint. Default is
Ollama at `localhost:11434`.

```bash
pip install -e .
cp .env.example .env
ollama pull llama3.2:3b
```

### Model recommendations

| Model         | Size  | Pass 1 schema | Use            |
|---------------|-------|---------------|----------------|
| `tinyllama`   | 1.1B  | unreliable    | smoke test only |
| `llama3.2:3b` | 3B    | sometimes     | local dev      |
| `llama3.1:8b` | 8B    | reliable      | recommended    |
| `vLLM` 13B+   | 13B+  | reliable      | production     |

Pass 2 (theme + attribution) makes 3B models usable; 8B+ unlocks the
overall-assessment / vulnerabilities / resilience-signals fields.

## Run

**Streamlit UI:**

```bash
streamlit run src/crucible/ui/app.py
```

The UI auto-detects whether your input is a URL (fetches and strips HTML),
code (frames it as a software/architecture review), or business text.

**CLI:**

```bash
python scripts/run_demo.py "Your proposal text here"
# or pipe stdin:
cat proposal.md | python scripts/run_demo.py -
```

Report goes to stdout, progress events to stderr.

## Personas

| Persona            | Role                                                        |
|--------------------|-------------------------------------------------------------|
| Skeptic            | Demands evidence, flags unsupported claims                  |
| Red Teamer         | Hunts exploitation, abuse, adversarial misuse vectors       |
| Ethics Auditor     | Surfaces harms, bias, fairness gaps, dual-use risk          |
| Market Critic      | Questions commercial viability, positioning, sustainability |
| Devil's Advocate   | Steelmans the strongest counter-position                    |
| Pragmatist         | Stress-tests scope, complexity, day-90 operational reality  |

Each is a single YAML in `config/personas/`. Edit `system_prompt` to
retune behaviour. Drop in a new file to add a 7th reviewer. Per-persona
overrides for `model`, `temperature`, and `max_tokens` are supported.

## Configuration

Defaults are in `config/settings.yaml`. Environment variables override
them and load from `.env`:

| Variable                     | Purpose                              |
|------------------------------|--------------------------------------|
| `CRUCIBLE_API_BASE`          | OpenAI-compatible base URL           |
| `CRUCIBLE_API_KEY`           | Bearer token (`ollama` for local)    |
| `CRUCIBLE_DEFAULT_MODEL`     | Default model for all personas       |
| `CRUCIBLE_SYNTHESIZER_MODEL` | Model for the synthesizer            |
| `CRUCIBLE_TIMEOUT_SECONDS`   | httpx timeout (default 120)          |
| `CRUCIBLE_MAX_RETRIES`       | Retries on 5xx/timeout (default 2)   |

The client is endpoint-agnostic — the same code works against vLLM
(production), Ollama (local), llama.cpp server, or OpenAI itself.

## Confidence scoring

After synthesis, each finding gets a `confidence` label derived from
inter-reviewer agreement:

| Reviewers raising it | Confidence |
|----------------------|------------|
| 5/6 or 6/6           | high       |
| 3/6 or 4/6           | medium     |
| 1/6 or 2/6           | low        |

Thresholds are ratio-based, so the same logic applies if you change the
persona count.

## Output schema

Each finding:

```json
{
  "title": "Untested AI accuracy claims",
  "summary": "The proposal asserts brand-aligned output but supplies no benchmark.",
  "raised_by": ["Skeptic", "Devil's Advocate"],
  "severity": "high",
  "confidence": "medium",
  "owasp_category": null
}
```

`owasp_category` uses A01–A10 (OWASP Top 10) or LLM01–LLM10 (OWASP LLM
Top 10) for security-shaped findings; null for ethics, market, or scope
concerns.

## Project layout

```
crucible/
├── config/
│   ├── settings.yaml           endpoint, models, orchestration defaults
│   └── personas/*.yaml         6 persona definitions
├── src/crucible/
│   ├── client.py               async OpenAI-compatible chat client
│   ├── settings.py             YAML + env-var loader
│   ├── personas.py             YAML → PersonaConfig
│   ├── orchestrator.py         parallel critiques + cross-debate
│   ├── synthesizer.py          two-pass synthesis + confidence scoring
│   ├── models.py               Pydantic data models
│   ├── templates/report.md.j2  Jinja2 markdown template
│   └── ui/app.py               Streamlit UI
├── scripts/run_demo.py         CLI entry point
└── tests/                      pytest fixtures
```

## License

MIT — see [LICENSE](LICENSE).
