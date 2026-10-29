"""
core/analysis/linter.py
Stage 3: wraps ruff (already a project dependency) — not reinventing a
linter, per the explicit non-goal. Normalizes ruff's JSON output into
symbol-scoped findings so risk_classifier and CLI output share one shape.
"""

from __future__ import annotations

import json
import os
import subprocess

from pydantic import BaseModel

from gitscribe.core.indexer.index_store import _get_connection


class LintFinding(BaseModel):
    file: str
    lineno: int
    code: str
    message: str
    severity: str
    col: int = 0
    fixable: bool = False  # ruff reports an auto-fix for this finding


_ERROR_PREFIXES = ("F", "E9", "S")


def _severity_for(code: str) -> str:
    return "error" if code.startswith(_ERROR_PREFIXES) else "warning"


class RuffNotFoundError(RuntimeError):
    """Raised when the `ruff` binary isn't on PATH."""


class RuffError(RuntimeError):
    """ruff itself failed (bad config, crash) - distinct from 'found lint
    errors'. Previously this was swallowed and reported as 'no findings'."""


def _ruff(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["ruff", *args], capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuffNotFoundError(
            "`ruff` not found on PATH. It's a project dependency - "
            "install it with `pip install ruff` or reinstall gitscribe."
        ) from e


def run_ruff(repo_root: str = ".", paths: list[str] | None = None) -> list[LintFinding]:
    targets = list(paths) if paths else [repo_root]
    result = _ruff(["check", *targets, "--output-format=json"])
    # ruff: 0 = clean, 1 = findings, 2 = ruff failed (config error, crash)
    if result.returncode >= 2:
        raise RuffError((result.stderr or result.stdout).strip() or f"ruff exited with {result.returncode}")
    if not result.stdout.strip():
        return []
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuffError(f"could not parse ruff output: {e}") from e

    findings = []
    for item in raw:
        code = item.get("code") or ""
        findings.append(
            LintFinding(
                file=item["filename"],
                lineno=item["location"]["row"],
                col=item["location"].get("column", 0),
                code=code,
                message=item.get("message", ""),
                severity=_severity_for(code),
                fixable=item.get("fix") is not None,
            )
        )
    return findings


def apply_fixes(repo_root: str = ".", paths: list[str] | None = None, unsafe: bool = False) -> None:
    """`ruff check --fix` (safe fixes only unless `unsafe`)."""
    args = ["check", *(paths or [repo_root]), "--fix", "--exit-zero", "--quiet"]
    if unsafe:
        args.append("--unsafe-fixes")
    result = _ruff(args)
    if result.returncode >= 2:
        raise RuffError((result.stderr or result.stdout).strip() or "ruff --fix failed")


def format_code(repo_root: str = ".", paths: list[str] | None = None, diff: bool = False) -> str:
    """`ruff format`; with `diff`, only previews and changes nothing. Returns ruff's output."""
    result = _ruff(["format", *(paths or [repo_root]), *(["--diff"] if diff else [])])
    if result.returncode >= 2:
        raise RuffError((result.stderr or result.stdout).strip() or "ruff format failed")
    return result.stdout + (result.stderr if diff else "")


def preview_fixes(repo_root: str = ".", paths: list[str] | None = None, unsafe: bool = False) -> str:
    """`ruff check --diff`: what `--fix` would change, without changing it."""
    args = ["check", *(paths or [repo_root]), "--diff", "--exit-zero"]
    if unsafe:
        args.append("--unsafe-fixes")
    result = _ruff(args)
    if result.returncode >= 2:
        raise RuffError((result.stderr or result.stdout).strip() or "ruff --diff failed")
    return result.stdout


def findings_by_file(findings: list[LintFinding]) -> dict[str, list[LintFinding]]:
    grouped: dict[str, list[LintFinding]] = {}
    for f in findings:
        grouped.setdefault(f.file, []).append(f)
    return grouped


def severity_score(findings: list[LintFinding]) -> float:
    """0-1 normalized signal for risk_classifier: proportion of findings
    that are errors vs warnings. Simple, explainable, no magic weighting.
    """
    if not findings:
        return 0.0
    errors = sum(1 for f in findings if f.severity == "error")
    return errors / len(findings)

def _symbol_id_for(conn, file: str, lineno: int) -> int | None:
    """Line-range containment lookup — smallest enclosing symbol wins
    (e.g. a method inside a class)"""
    file = _normalize_path(file)
    row = conn.execute(
        """SELECT id FROM symbols
           WHERE file = ? AND lineno <= ?
             AND (end_lineno IS NULL OR end_lineno >= ?)
           ORDER BY (COALESCE(end_lineno, lineno) - lineno) ASC
           LIMIT 1""",
        (file, lineno, lineno),
    ).fetchone()
    return row["id"] if row else None


def write_lint_findings(findings: list[LintFinding]) -> int:
    """Maps each finding to a symbol_id (NULL for module-level findings
    with no enclosing symbol, per spec §3.2) and writes to
    review_findings with source='lint'. Returns count written.
    """
    conn = _get_connection()
    for f in findings:
        symbol_id = _symbol_id_for(conn, f.file, f.lineno)
        conn.execute(
            """INSERT INTO review_findings
               (symbol_id, source, severity, rule_or_reason, message, line_start, line_end)
               VALUES (?, 'lint', ?, ?, ?, ?, ?)""",
            (symbol_id, f.severity, f.code, f.message, f.lineno, f.lineno),
        )
    conn.commit()
    return len(findings)

def run_lint_review(repo_root: str = ".") -> int:
    """Entry point for `gitscribe review --lint-only`:
    deterministic, no LLM call, runs regardless of Merkle skip state.
    """
    findings = run_ruff(repo_root)
    return write_lint_findings(findings)

def _normalize_path(file: str) -> str:
    """ruff --output-format=json reports absolute paths; symbols.file is
    stored relative to repo root Without normalizing, the
    containment lookup below silently never matches"""
    try:
        return os.path.relpath(file)
    except ValueError:
        return file
