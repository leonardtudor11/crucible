"""Pydantic data models shared across the pipeline."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low"]
Confidence = Literal["high", "medium", "low"]


class PersonaConfig(BaseModel):
    name: str
    description: str = ""
    model: str | None = None
    temperature: float = 0.6
    max_tokens: int = 1024
    system_prompt: str


class EndpointConfig(BaseModel):
    base_url: str
    api_key: str = ""
    timeout: float = 120.0
    max_retries: int = 2


class OrchestrationConfig(BaseModel):
    debate_rounds: int = 1
    synthesizer_model: str
    synthesizer_temperature: float = 0.3
    parallel: bool = True


class Settings(BaseModel):
    endpoint: EndpointConfig
    defaults: dict[str, Any] = Field(default_factory=dict)
    orchestration: OrchestrationConfig
    personas_dir: str = "config/personas"


class Critique(BaseModel):
    persona: str
    content: str
    model: str | None = None
    raw: dict[str, Any] | None = None


class DebateTurn(BaseModel):
    persona: str
    content: str
    model: str | None = None
    references: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    persona: str
    quote: str


class Finding(BaseModel):
    title: str
    summary: str
    raised_by: list[str] = Field(default_factory=list)
    severity: Severity = "medium"
    confidence: Confidence = "low"
    owasp_category: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class Report(BaseModel):
    input: str
    input_type: str = "business"
    critiques: list[Critique]
    debate: list[DebateTurn]
    findings: list[Finding] = Field(default_factory=list)
    vulnerabilities: list[str] = Field(default_factory=list)
    resilience_signals: list[str] = Field(default_factory=list)
    overall_assessment: str = ""
    synthesis_raw: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
