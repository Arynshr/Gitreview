from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from gitscribe.config_locator import find_config_path
from gitscribe.core.config_schema import DEFAULT_MODE, VALID_MODES, ValidationConfig

# Re-exported for backward compatibility: other modules (e.g.
# validation.mode) historically imported these constants from here. The
# values themselves now live in core/config_schema.py, alongside the rest
# of the app's config schema - this is the single place that defines them.
__all__ = ["DEFAULT_MODE", "VALID_MODES", "load_validation_config"]


def load_validation_config(path: str | None = None) -> dict:
    """Loads and validates the `validation:` section of config.yaml.

    Schema, defaults, and field constraints all live in exactly one place:
    `core.config_schema.ValidationConfig`, part of the same GitScribeConfig
    tree that `core.cli.load_config` validates as a whole. This function is
    a thin adapter over that model - parse the raw section, validate it,
    hand back a plain dict - so existing consumers (orchestrator.py, ai.py,
    deterministic.py, policy.py, mode.py) are unaffected: they already just
    do cfg.get(...) / cfg["ai"] / cfg["file_rules"] on the result.

    Previously this module carried its own hand-rolled DEFAULTS dict, its
    own recursive _merge(), and its own ad-hoc validation checks (provider
    must be "llamacpp", timeouts > 0, file_rules shape, etc.) - a second,
    independent config schema for the exact same config.yaml file that
    core.config_schema.GitScribeConfig already validates. That duplication
    is what let the two drift apart. Pydantic's own merging (explicit
    fields override model defaults) and validators now do all of that in
    one place.
    """
    path = path or find_config_path()
    config_path = Path(path)

    if not config_path.is_file():
        raise RuntimeError(f"config file not found: {path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    section = raw.get("validation", {})

    if not isinstance(section, dict):
        raise RuntimeError("validation configuration must be a mapping")

    try:
        validated = ValidationConfig(**section)
    except ValidationError as exc:
        raise RuntimeError(f"validation configuration failed: {exc}") from exc

    cfg = validated.model_dump()
    cfg["ignore_patterns"] = raw.get("ignore_patterns", [])

    return cfg
