"""Run Crucible across the eval corpus and emit metrics.

Usage:
    python scripts/eval.py                 # multi-model (default, current personas YAML)
    python scripts/eval.py --single-model  # override every persona to one model

Outputs:
    eval_results/<mode>_<timestamp>.json   per-input metrics + aggregates
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from crucible.client import ChatClient
from crucible.orchestrator import gather_critiques, run_debate
from crucible.personas import load_personas
from crucible.settings import load_settings
from crucible.synthesizer import synthesize


SINGLE_MODEL = "Qwen/Qwen2.5-7B-Instruct"


def gpu_snapshot(droplet_ip: str | None) -> dict | None:
    """Read rocm-smi over SSH for VRAM used. None if unreachable."""
    if not droplet_ip:
        return None
    try:
        out = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                f"root@{droplet_ip}",
                "rocm-smi --showmeminfo vram --json 2>/dev/null",
            ],
            check=False, capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        data = json.loads(out.stdout)
        gpu0 = data.get("card0") or data.get("GPU[0]") or next(iter(data.values()), {})
        used = int(gpu0.get("VRAM Total Used Memory (B)", 0))
        total = int(gpu0.get("VRAM Total Memory (B)", 0))
        return {"used_gb": round(used / 1e9, 2), "total_gb": round(total / 1e9, 2)}
    except Exception:
        return None


async def run_one(text: str, single_model: bool):
    settings = load_settings()
    personas = load_personas(Path(settings.personas_dir))
    if single_model:
        for p in personas:
            p.model = SINGLE_MODEL
    default_model = settings.defaults.get("model", SINGLE_MODEL)
    synth_model = settings.orchestration.synthesizer_model

    t0 = time.perf_counter()
    error: str | None = None
    report = None
    try:
        async with ChatClient(settings.endpoint) as client:
            critiques = await gather_critiques(client, personas, text, default_model)
            t_round1 = time.perf_counter()
            debate = await run_debate(client, personas, text, critiques, default_model)
            t_round2 = time.perf_counter()
            report = await synthesize(
                client, text, critiques, debate,
                model=synth_model,
                temperature=settings.orchestration.synthesizer_temperature,
            )
            t_synth = time.perf_counter()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        return {"error": error, "total_s": round(time.perf_counter() - t0, 2)}

    if report is None:
        return {"error": "no report", "total_s": round(time.perf_counter() - t0, 2)}

    def conf_count(label):
        return sum(1 for f in report.findings if f.confidence == label)

    def severity_count(label):
        return sum(1 for f in report.findings if f.severity == label)

    return {
        "error": None,
        "round1_s": round(t_round1 - t0, 2),
        "round2_s": round(t_round2 - t_round1, 2),
        "synth_s": round(t_synth - t_round2, 2),
        "total_s": round(t_synth - t0, 2),
        "synthesis_pass": report.metadata.get("synthesis_pass"),
        "distinct_models": report.metadata.get("distinct_model_count", 0),
        "findings_count": len(report.findings),
        "confidence": {
            "high": conf_count("high"),
            "medium": conf_count("medium"),
            "low": conf_count("low"),
        },
        "severity": {
            "critical": severity_count("critical"),
            "high": severity_count("high"),
            "medium": severity_count("medium"),
            "low": severity_count("low"),
        },
        "owasp_tagged": sum(
            1 for f in report.findings if f.owasp_category is not None
        ),
        "evidence_total": sum(len(f.evidence) for f in report.findings),
        "vulnerabilities_count": len(report.vulnerabilities),
        "resilience_count": len(report.resilience_signals),
        "overall_assessment_chars": len(report.overall_assessment),
        "run_id": report.metadata.get("run_id"),
    }


def aggregate(per_input):
    valid = [r for r in per_input if not r.get("error")]
    if not valid:
        return {"valid_runs": 0}
    return {
        "valid_runs": len(valid),
        "errored_runs": len(per_input) - len(valid),
        "median_total_s": statistics.median(r["total_s"] for r in valid),
        "p95_total_s": (
            statistics.quantiles((r["total_s"] for r in valid), n=20)[-1]
            if len(valid) > 1 else valid[0]["total_s"]
        ),
        "median_findings": statistics.median(r["findings_count"] for r in valid),
        "median_evidence_per_run": statistics.median(
            r["evidence_total"] for r in valid
        ),
        "pass1_rate": round(
            sum(1 for r in valid if r["synthesis_pass"] == "pass1") / len(valid), 2
        ),
        "owasp_tagged_total": sum(r["owasp_tagged"] for r in valid),
        "high_confidence_findings_total": sum(
            r["confidence"]["high"] for r in valid
        ),
        "medium_confidence_findings_total": sum(
            r["confidence"]["medium"] for r in valid
        ),
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--single-model", action="store_true",
        help="Override all personas to use one model (homogeneous baseline).",
    )
    parser.add_argument(
        "--corpus", default="tests/eval_corpus",
        help="Directory of *.txt input files",
    )
    parser.add_argument(
        "--droplet", default="129.212.181.126",
        help="Droplet IP for GPU snapshot (set empty to skip)",
    )
    args = parser.parse_args()

    corpus_dir = Path(args.corpus)
    inputs = sorted(corpus_dir.glob("*.txt"))
    if not inputs:
        print(f"no inputs found in {corpus_dir}", file=sys.stderr)
        sys.exit(1)

    mode = "single" if args.single_model else "multi"
    print(f"=== eval mode: {mode} ({len(inputs)} inputs) ===", file=sys.stderr)

    gpu_before = gpu_snapshot(args.droplet)
    if gpu_before:
        print(f"  gpu before: {gpu_before['used_gb']}/{gpu_before['total_gb']} GB", file=sys.stderr)

    results = []
    for path in inputs:
        text = path.read_text()
        print(f"  -> {path.name} ({len(text)} chars)", file=sys.stderr)
        r = await run_one(text, single_model=args.single_model)
        r["input_file"] = path.name
        r["input_chars"] = len(text)
        results.append(r)
        if r.get("error"):
            print(f"     ERROR: {r['error']}", file=sys.stderr)
        else:
            print(
                f"     {r['total_s']}s | {r['findings_count']} findings | "
                f"pass={r['synthesis_pass']} | "
                f"conf H/M/L={r['confidence']['high']}/{r['confidence']['medium']}/{r['confidence']['low']} | "
                f"evidence={r['evidence_total']}",
                file=sys.stderr,
            )

    gpu_after = gpu_snapshot(args.droplet)
    agg = aggregate(results)

    payload = {
        "mode": mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input_count": len(inputs),
        "gpu_before": gpu_before,
        "gpu_after": gpu_after,
        "aggregate": agg,
        "per_input": results,
    }

    out_dir = Path("eval_results")
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{mode}_{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\n=== aggregate ({mode}) ===", file=sys.stderr)
    for k, v in agg.items():
        print(f"  {k}: {v}", file=sys.stderr)
    print(f"\nresults: {out_path}", file=sys.stderr)
    print(out_path)


if __name__ == "__main__":
    asyncio.run(main())
