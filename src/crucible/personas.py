"""Load persona definitions from YAML configs."""
from __future__ import annotations

from pathlib import Path

import yaml

from .models import PersonaConfig


def load_persona(path: Path) -> PersonaConfig:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Persona file {path} did not parse to a mapping")
    return PersonaConfig.model_validate(data)


def load_personas(directory: Path) -> list[PersonaConfig]:
    if not directory.exists():
        raise FileNotFoundError(f"Persona directory not found: {directory}")
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise FileNotFoundError(f"No persona YAMLs found in {directory}")
    return [load_persona(f) for f in files]
