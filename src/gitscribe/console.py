"""
Single abstraction for all human-facing CLI console output.

cli.py previously called typer.echo/secho ad hoc per command, which is why
some commands (init, generate) were colored and others (lint, graph) printed
everything in plain, undifferentiated text. Every command should route
through these four functions so severity is always visually distinct and
the "[gitscribe]" prefix is applied in exactly one place.

Not a replacement for telemetry.py: that module is structured, file-backed
logging of LLM call metadata (JSONL, for later analysis). This module is
ephemeral, human-facing terminal output. Different concern, not a duplicate.
"""

import typer

PREFIX = "[gitscribe]"

# `--stream json` owns stdout (NDJSON); human messages move to stderr.
_STDERR = False


def route_to_stderr(flag: bool) -> None:
    global _STDERR
    _STDERR = flag


def error(message: str) -> None:
    typer.secho(f"{PREFIX} {message}", err=True, fg=typer.colors.RED)


def warn(message: str) -> None:
    typer.secho(f"{PREFIX} {message}", err=True, fg=typer.colors.YELLOW)


def success(message: str) -> None:
    typer.secho(f"{PREFIX} {message}", err=_STDERR, fg=typer.colors.GREEN)


def info(message: str) -> None:
    """Neutral status message - no color, no prefix (matches existing
    plain-output commands like `generate`'s PR body)."""
    typer.echo(message, err=_STDERR)


def line(message: str, severity: str = "info") -> None:
    """One colored line of domain data (a finding, a graph edge) - stdout,
    no "[gitscribe]" prefix. Distinct from error()/warn(), which are for
    CLI-level status messages on stderr. Use this for per-row output where
    the prefix would just be repeated noise."""
    colors = {"error": typer.colors.RED, "warning": typer.colors.YELLOW}
    typer.secho(message, err=_STDERR, fg=colors.get(severity))
