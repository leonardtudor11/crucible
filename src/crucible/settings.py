"""Load settings.yaml and apply env-var overrides."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import EndpointConfig, OrchestrationConfig, Settings


def _project_root() -> Path:
    # src/crucible/settings.py → project root
    return Path(__file__).resolve().parents[2]


def locate_settings(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    env = os.getenv("CRUCIBLE_CONFIG_DIR")
    if env:
        return Path(env) / "settings.yaml"
    cwd_candidate = Path.cwd() / "config" / "settings.yaml"
    if cwd_candidate.exists():
        return cwd_candidate
    root_candidate = _project_root() / "config" / "settings.yaml"
    if root_candidate.exists():
        return root_candidate
    raise FileNotFoundError(
        "Could not locate config/settings.yaml. Set CRUCIBLE_CONFIG_DIR or "
        "run from the project root."
    )


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv()
    settings_path = locate_settings(path)
    data = yaml.safe_load(settings_path.read_text()) or {}

    endpoint = dict(data.get("endpoint", {}))
    if v := os.getenv("CRUCIBLE_API_BASE"):
        endpoint["base_url"] = v
    if v := os.getenv("CRUCIBLE_API_KEY"):
        endpoint["api_key"] = v
    if v := os.getenv("CRUCIBLE_TIMEOUT_SECONDS"):
        endpoint["timeout"] = float(v)
    if v := os.getenv("CRUCIBLE_MAX_RETRIES"):
        endpoint["max_retries"] = int(v)

    defaults = dict(data.get("defaults", {}))
    if v := os.getenv("CRUCIBLE_DEFAULT_MODEL"):
        defaults["model"] = v

    orchestration = dict(data.get("orchestration", {}))
    if v := os.getenv("CRUCIBLE_SYNTHESIZER_MODEL"):
        orchestration["synthesizer_model"] = v

    personas_dir = data.get("personas_dir", "personas")
    pd = Path(personas_dir)
    if not pd.is_absolute():
        # Resolve relative to the settings file's directory
        pd = settings_path.parent / pd
    personas_dir = str(pd)

    return Settings(
        endpoint=EndpointConfig(**endpoint),
        defaults=defaults,
        orchestration=OrchestrationConfig(**orchestration),
        personas_dir=personas_dir,
    )
