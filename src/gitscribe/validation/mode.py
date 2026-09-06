from __future__ import annotations

import pathspec

# VALID_MODES/DEFAULT_MODE live in core/config_schema.py (the single schema
# source for the whole app) and are re-exported here so existing importers
# of gitscribe.validation.mode don't need to change.
from gitscribe.core.config_schema import DEFAULT_MODE, VALID_MODES

__all__ = ["VALID_MODES", "DEFAULT_MODE", "resolve_file_modes", "split_by_mode"]


def resolve_file_modes(
    files: list[str],
    file_rules: list[dict],
    default_mode: str = DEFAULT_MODE,
) -> dict[str, str]:
    """Pure, deterministic: no LLM, no I/O beyond glob matching.

    `file_rules` is an ordered list of {"path": <glob>, "mode": <static|agentic|both>}.
    First matching rule wins per file; unmatched files fall back to `default_mode`.
    """
    modes: dict[str, str] = {}

    for file in files:
        mode = default_mode

        for rule in file_rules:
            selector = pathspec.PathSpec.from_lines("gitwildmatch", [rule["path"]])

            if selector.match_file(file):
                mode = rule["mode"]
                break

        modes[file] = mode

    return modes


def split_by_mode(modes: dict[str, str]) -> tuple[list[str], list[str]]:
    """Returns (static_files, agentic_files). A file in "both" mode appears in each."""
    static_files = [f for f, m in modes.items() if m in ("static", "both")]
    agentic_files = [f for f, m in modes.items() if m in ("agentic", "both")]
    return static_files, agentic_files
