from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

from gitscribe.validation.analysis.models import AnalysisScope
from gitscribe.validation.analysis.runner import ScannerExecutionError, run_scanner_command
from gitscribe.validation.analysis.scanners.bandit_scanner import BanditScanner
from gitscribe.validation.analysis.scanners.ruff_scanner import RuffScanner
from gitscribe.validation.models import ChangeContext, Finding


class DeterministicAnalysisError(RuntimeError):
    pass


def _run(args: list[str], timeout_seconds: float | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise DeterministicAnalysisError(
            f"required scanner not found: {args[0]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DeterministicAnalysisError(
            f"{args[0]} did not finish within {timeout_seconds}s"
        ) from exc


def _path_traversal_findings(
    context: ChangeContext,
) -> list[Finding]:
    """
    Small, deliberately narrow change-local check.

    This is not intended to replace a full SAST engine. It catches the
    important prototype fixture where direct external input reaches a
    filesystem path sink.
    """
    findings: list[Finding] = []

    for file in context.files:
        if not file.endswith(".py"):
            continue

        path = Path(file)

        if not path.is_file():
            continue

        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=file)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue

        tainted: set[str] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue

            if not isinstance(node.value, ast.Call):
                continue

            call = node.value
            tainted_source = False

            if isinstance(call.func, ast.Name):
                tainted_source = call.func.id == "input"

            elif isinstance(call.func, ast.Attribute):
                tainted_source = call.func.attr in {
                    "args",
                    "form",
                    "json",
                    "query_params",
                }

            if tainted_source:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        tainted.add(target.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            if not node.args:
                continue

            sink = False

            if isinstance(node.func, ast.Name):
                sink = node.func.id in {"open", "Path"}

            elif isinstance(node.func, ast.Attribute):
                sink = node.func.attr in {
                    "join",
                    "resolve",
                }

            if not sink:
                continue

            if any(
                isinstance(arg, ast.Name) and arg.id in tainted
                for arg in node.args
            ):
                findings.append(
                    Finding(
                        id=f"path-traversal:{file}:{node.lineno}",
                        source="deterministic",
                        category="path traversal",
                        rule_id="GITSCRIBE-PATH-001",
                        severity="high",
                        confidence="medium",
                        file=file,
                        line=node.lineno,
                        message=(
                            "Potential path traversal: external input reaches "
                            "a filesystem path sink without an explicit "
                            "containment check."
                        ),
                        evidence=(
                            "A value originating from input/request parameters "
                            "flows into a filesystem path operation."
                        ),
                        remediation=(
                            "Normalize and validate the path, reject absolute "
                            "paths and '..' traversal, and enforce that the "
                            "resolved path remains inside an allowed root."
                        ),
                        sources=["gitscribe-path-check"],
                    )
                )
                # Deliberately no `break` here: the previous version
                # stopped scanning a file after its first tainted sink,
                # silently missing every other instance in the same
                # file. Each sink call site is a distinct location and
                # is reported independently; aggregator.py already
                # dedupes by (file, line, category) if the same line
                # somehow matches twice.

    return findings


def _run_scanner_adapter(
    scanner,
    context: ChangeContext,
    timeout_seconds: float,
) -> list[Finding]:
    """Shared ChangeContext -> AnalysisScope shim for every migrated
    Scanner adapter (validation/analysis/scanner.py), so run_ruff()/
    run_bandit()/future run_<x>() stay one-line wrappers instead of each
    re-implementing this same build/run/parse + error-translation
    sequence. ScannerExecutionError (an analysis-layer concern) is
    translated to DeterministicAnalysisError here so callers of this
    module - namely validation/orchestrator.py - don't need to know the
    analysis layer exists; see run_ruff()'s docstring for why that
    boundary matters.
    """
    scope = AnalysisScope(files=context.files)

    command = scanner.build_command(scope, {"timeout_seconds": timeout_seconds})
    if command is None:
        return []

    try:
        result = run_scanner_command(command)
    except ScannerExecutionError as exc:
        raise DeterministicAnalysisError(str(exc)) from exc

    try:
        return scanner.parse(result, scope)
    except ScannerExecutionError as exc:
        raise DeterministicAnalysisError(str(exc)) from exc


def run_ruff(
    context: ChangeContext,
    timeout_seconds: float = 60,
) -> list[Finding]:
    """Delegates to the registry-based RuffScanner adapter
    (validation/analysis/scanners/ruff_scanner.py).

    This function is now a thin wrapper kept for backward compatibility:
    validation/orchestrator.py (and anything else importing
    `run_ruff`/`run_deterministic` from this module) is unaffected by the
    migration to the Scanner protocol - see validation_layer.md's
    Required Implementation Sequence step 6 ("Migrate existing Ruff
    implementation") and section 2.1's target-treatment row for this
    module ("Refactor into scanner adapters; remove ad-hoc checks from
    orchestration"). The actual subprocess execution and finding-parsing
    logic now lives in exactly one place: RuffScanner.
    """
    return _run_scanner_adapter(RuffScanner(), context, timeout_seconds)


def run_bandit(
    context: ChangeContext,
    timeout_seconds: float = 60,
) -> list[Finding]:
    """Delegates to the registry-based BanditScanner adapter
    (validation/analysis/scanners/bandit_scanner.py) - Required
    Implementation Sequence step 7. Bandit is now the sole source of
    Python security findings; RuffScanner's `S`-rule handling was removed
    in the same change that added this, per validation_layer.md section 7
    (Tool Responsibility Matrix).
    """
    return _run_scanner_adapter(BanditScanner(), context, timeout_seconds)


def run_pip_audit(
    context: ChangeContext,
    timeout_seconds: float = 60,
) -> list[Finding]:
    dependency_files = {
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "poetry.lock",
        "uv.lock",
        "Pipfile",
        "Pipfile.lock",
    }

    dependency_change = any(
        Path(file).name in dependency_files
        for file in context.files
    )

    if not dependency_change:
        return []

    result = _run(
        [
            "pip-audit",
            "--format=json",
            ".",
        ],
        timeout_seconds=timeout_seconds,
    )

    if result.returncode not in (0, 1):
        raise DeterministicAnalysisError(
            "pip-audit failed: "
            + (result.stderr.strip() or "no diagnostic output")
        )

    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise DeterministicAnalysisError(
            "pip-audit returned malformed JSON"
        ) from exc

    findings: list[Finding] = []

    for item in raw:
        name = item.get("name", "unknown-package")
        version = item.get("version", "unknown")

        for vuln in item.get("vulns", []):
            vuln_id = vuln.get(
                "id",
                "unknown-vulnerability",
            )

            fixes = vuln.get("fix_versions") or []

            if fixes:
                remediation = (
                    f"Upgrade {name} from {version} to a fixed "
                    f"version: {', '.join(fixes)}"
                )
            else:
                remediation = (
                    f"Upgrade or replace {name} and verify the "
                    "advisory remediation."
                )

            findings.append(
                Finding(
                    id=f"pip-audit:{name}:{vuln_id}",
                    source="deterministic",
                    category="dependency",
                    rule_id=vuln_id,
                    severity="high",
                    confidence="high",
                    message=(
                        f"Known vulnerability {vuln_id} affects "
                        f"{name} {version}."
                    ),
                    evidence=(
                        f"pip-audit reported {vuln_id} for "
                        f"{name} {version}."
                    ),
                    remediation=remediation,
                    sources=["pip-audit"],
                )
            )

    return findings


def run_deterministic(
    context: ChangeContext,
    timeout_seconds: float = 60,
) -> list[Finding]:
    findings: list[Finding] = []

    findings.extend(_path_traversal_findings(context))
    findings.extend(run_ruff(context, timeout_seconds=timeout_seconds))
    findings.extend(run_bandit(context, timeout_seconds=timeout_seconds))
    findings.extend(run_pip_audit(context, timeout_seconds=timeout_seconds))

    return findings
