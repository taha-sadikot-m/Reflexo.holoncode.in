"""Model role configuration (`config/models.yaml`, POC_04 §2.1) with `${REFLEXO_*}` interpolation from settings."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from reflexo.common.config import Settings, get_settings

_VAR = re.compile(r"\$\{(REFLEXO_[A-Z0-9_]+)\}")


class RoleConfig(BaseModel):
    primary: str
    fallback: str | None = None
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_output_tokens: int = Field(default=1024, gt=0)
    hedge_after_ms: int | None = None  # None = no hedging (fallback only on failure)
    timeout_ms: int = Field(default=20000, gt=0)
    thinking_budget: int | None = 0  # 0 = off; -1 = dynamic; None = provider default


class EmbeddingConfig(BaseModel):
    model: str
    dimensions: int = 768
    batch_size: int = Field(default=100, gt=0, le=250)
    timeout_ms: int = 15000


class ModelsConfig(BaseModel):
    roles: dict[str, RoleConfig]
    embedding: EmbeddingConfig

    def role(self, name: str) -> RoleConfig:
        try:
            return self.roles[name]
        except KeyError as exc:
            raise KeyError(f"unknown model role {name!r}; configured: {sorted(self.roles)}") from exc


def _interpolate(value: Any, settings: Settings) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            attr = m.group(1).removeprefix("REFLEXO_").lower()
            if not hasattr(settings, attr):
                raise KeyError(f"models.yaml references unknown setting {m.group(1)}")
            return str(getattr(settings, attr))

        return _VAR.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, settings) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, settings) for v in value]
    return value


def load_models_config(settings: Settings | None = None, path: str | Path | None = None) -> ModelsConfig:
    settings = settings or get_settings()
    raw = yaml.safe_load(Path(path or settings.models_config).read_text(encoding="utf-8"))
    cfg = ModelsConfig.model_validate(_interpolate(raw, settings))
    for name, role in cfg.roles.items():
        if role.fallback == role.primary:
            cfg.roles[name] = role.model_copy(update={"fallback": None})  # hedging against itself is pointless
    return cfg
