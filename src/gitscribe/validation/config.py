from __future__ import annotations

from pathlib import Path

import yaml

from gitscribe.config_locator import find_config_path
from gitscribe.validation.mode import VALID_MODES

DEFAULTS = {
    "enabled": True,
    "fail_closed": True,
    "fail_on": ["critical", "high"],
    "block_secrets": True,
    "block_new_vulnerabilities": True,
    # Ordered path->mode assignments for batching/pre-defining review scope.
    # First match wins; files matching nothing use the existing "both" behavior.
    # e.g. [{"path": "src/gitscribe/validation/**", "mode": "agentic"}]
    "file_rules": [],
    "deterministic": {
        "enabled": True,
    },
    "ai": {
        "enabled": True,
        "provider": "llamacpp",
        "model": "qwen2.5-coder-3b-instruct-q4_k_m",
        "base_url": "http://127.0.0.1:8080",
        "api_key_env": "VALIDATION_API_KEY",
        "timeout_seconds": 120,
        "max_context_tokens": 6000,
        "max_output_tokens": 1200,
        "max_file_chars": 12000,
    },
}


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value

    return result


def load_validation_config(path: str | None = None) -> dict:
    path = path or find_config_path()
    config_path = Path(path)

    if not config_path.is_file():
        raise RuntimeError(f"config file not found: {path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    validation = raw.get("validation", {})

    if not isinstance(validation, dict):
        raise RuntimeError("validation configuration must be a mapping")

    cfg = _merge(DEFAULTS, validation)

    cfg["ignore_patterns"] = raw.get("ignore_patterns", [])

    if cfg["ai"]["provider"] != "llamacpp":
        raise RuntimeError("validation.ai.provider must be 'llamacpp'")

    if cfg["ai"]["max_context_tokens"] <= 0:
        raise RuntimeError("validation.ai.max_context_tokens must be > 0")

    if cfg["ai"]["timeout_seconds"] <= 0:
        raise RuntimeError("validation.ai.timeout_seconds must be > 0")

    if not isinstance(cfg["file_rules"], list):
        raise RuntimeError("validation.file_rules must be a list")

    for rule in cfg["file_rules"]:
        if not isinstance(rule, dict) or "path" not in rule or "mode" not in rule:
            raise RuntimeError(
                "validation.file_rules entries must be {path: <glob>, mode: <static|agentic|both>}"
            )

        if rule["mode"] not in VALID_MODES:
            raise RuntimeError(
                f"validation.file_rules mode must be one of {VALID_MODES}, got {rule['mode']!r}"
            )

    return cfg
