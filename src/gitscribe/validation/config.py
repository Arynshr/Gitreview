from __future__ import annotations

import subprocess
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


def _repo_root_for(path: Path) -> Path | None:
    for directory in (path.parent, *path.parent.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _read_config_text(path: Path, ref: str | None) -> str:
    """Reads config.yaml either from the working tree (ref=None, the
    normal case) or as it existed at a specific git ref.

    The `ref` path matters specifically for hook-triggered runs
    (`gitscribe verify --pre-push`/`--pre-merge`): the change under review
    could itself have edited config.yaml as part of the same diff. Without
    this, a malicious PR could redirect where validation.ai sends the
    diff (base_url), swap which env var is sent as its bearer token
    (api_key_env), change which model is trusted (model), or simply set
    `validation.enabled: false` and disable the gate reviewing it -
    entirely by editing the very file that configures its own reviewer.
    Reading config.yaml from the pre-change ref instead closes that: the
    diff's own edits to config.yaml take effect on the *next* run, after
    a human has actually reviewed and merged them, not on the run that's
    reviewing them for the first time.
    """
    if ref is None:
        if not path.is_file():
            raise RuntimeError(f"config file not found: {path}")
        return path.read_text(encoding="utf-8")

    root = _repo_root_for(path)

    if root is None:
        # Not resolvable to a git repo root - fall back to the working
        # tree copy rather than failing the whole run over it.
        if not path.is_file():
            raise RuntimeError(f"config file not found: {path}")
        return path.read_text(encoding="utf-8")

    rel = path.relative_to(root)

    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{rel.as_posix()}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git is required to resolve config.yaml at a ref") from exc

    if result.returncode == 0:
        return result.stdout

    # config.yaml didn't exist at that ref (e.g. it's new in this change,
    # or the ref itself doesn't resolve) - fall back to the working tree
    # copy rather than erroring the whole run over it.
    if not path.is_file():
        raise RuntimeError(f"config file not found at {ref} or in working tree: {path}")

    return path.read_text(encoding="utf-8")


def load_validation_config(path: str | None = None, ref: str | None = None) -> dict:
    """Loads and validates the `validation:` section of config.yaml.

    Schema, defaults, and field constraints all live in exactly one place:
    `core.config_schema.ValidationConfig`, part of the same GitScribeConfig
    tree that `core.cli.load_config` validates as a whole. This function is
    a thin adapter over that model - parse the raw section, validate it,
    hand back a plain dict - so existing consumers (orchestrator.py, ai.py,
    deterministic.py, policy.py, mode.py) are unaffected: they already just
    do cfg.get(...) / cfg["ai"] / cfg["file_rules"] on the result.

    `ref`, when given, reads config.yaml from that git ref instead of the
    working tree - see _read_config_text()'s docstring for why this
    matters for hook-triggered runs specifically.
    """
    path = path or find_config_path()
    config_path = Path(path)

    raw_text = _read_config_text(config_path, ref)
    raw = yaml.safe_load(raw_text) or {}
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
