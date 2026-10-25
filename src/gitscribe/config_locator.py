from __future__ import annotations

import os
from pathlib import Path

CONFIG_ENV_VAR = "GITSCRIBE_CONFIG"
CONFIG_FILENAME = "config.yaml"


def find_config_path(start: str | os.PathLike = ".") -> str:
    """The single place every config loader in this project resolves
    config.yaml from — `core/cli.py`'s `load_config`, `validation/config.py`'s
    `load_validation_config`, and `core/analysis/rag.py`'s sandboxed-ai
    lookup all go through this, so there is exactly one file to edit no
    matter which command you run or which subdirectory of the repo you're
    standing in when you run it.

    Resolution order:
    1. `GITSCRIBE_CONFIG` env var, if set — explicit override, absolute or
       relative to CWD.
    2. Walk upward from `start` looking for `config.yaml`, stopping at the
       first match or at the repo root (a `.git` directory), whichever
       comes first. This is what makes running any `gitscribe` command
       from a subdirectory find the same root config.yaml instead of
       needing a copy in every directory.
    3. Fall back to the literal "config.yaml" (today's behavior) if
       nothing is found — callers keep their existing "file not found"
       error path unchanged.
    """
    override = os.environ.get(CONFIG_ENV_VAR)

    if override:
        return override

    current = Path(start).resolve()

    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME

        if candidate.is_file():
            return str(candidate)

        if (directory / ".git").exists():
            break

    return CONFIG_FILENAME
