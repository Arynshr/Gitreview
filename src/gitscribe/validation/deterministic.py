from __future__ import annotations

import ast
from pathlib import Path

from gitscribe.validation.analysis.models import AnalysisScope
from gitscribe.validation.analysis.runner import ScannerExecutionError, run_scanner_command
from gitscribe.validation.analysis.scanners.betterleaks_scanner import BetterleaksScanner
from gitscribe.validation.analysis.scanners.osv_scanner import OSVScanner
from gitscribe.validation.analysis.scanners.ruff_scanner import RuffScanner
from gitscribe.validation.analysis.scanners.semgrep_scanner import SemgrepScanner
from gitscribe.validation.models import ChangeContext, Finding


class DeterministicAnalysisError(RuntimeError):
    pass


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

            if any(isinstance(arg, ast.Name) and arg.id in tainted for arg in node.args):
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
                # Deliberately no `break` here: each sink call site is a
                # distinct location and is reported independently;
                # aggregator.py dedupes by (file, line, category) if the
                # same line somehow matches twice.

    return findings


def _run_scanner_adapter(
    scanner,
    context: ChangeContext,
    timeout_seconds: float,
    extra_config: dict | None = None,
) -> list[Finding]:
    """Shared ChangeContext -> AnalysisScope shim for every Scanner
    adapter (validation/analysis/scanner.py), so run_ruff()/run_semgrep()/
    etc. stay one-line wrappers instead of each re-implementing this same
    build/run/parse + error-translation sequence. ScannerExecutionError
    (an analysis-layer concern) is translated to DeterministicAnalysisError
    here so callers of this module - namely validation/orchestrator.py -
    don't need to know the analysis layer exists. `extra_config` merges
    in scanner-specific flags (e.g. BetterleaksScanner's
    `betterleaks_live_validation`) that don't fit a single positional
    param.
    """
    scope = AnalysisScope(files=context.files)

    config = {"timeout_seconds": timeout_seconds, **(extra_config or {})}
    command = scanner.build_command(scope, config)
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
    """Delegates to RuffScanner - Python quality/style. Kept as the one
    per-language concession in the pruned toolset: zero-config, cheap,
    and genuinely not redundant with Semgrep's pattern-based rules (see
    SCOPE.md for why this is the only quality linter still in scope).
    """
    return _run_scanner_adapter(RuffScanner(), context, timeout_seconds)


def run_betterleaks(
    context: ChangeContext,
    timeout_seconds: float = 60,
    extra_config: dict | None = None,
) -> list[Finding]:
    """Delegates to BetterleaksScanner. Not called from
    run_deterministic() below - §10.3 requires this to run
    unconditionally, independent of the mode split and
    `deterministic.enabled`; see orchestrator.py. `extra_config` should
    be the `validation.deterministic` config dict, to pass through
    `betterleaks_live_validation`.
    """
    return _run_scanner_adapter(BetterleaksScanner(), context, timeout_seconds, extra_config)


def run_semgrep(
    context: ChangeContext,
    timeout_seconds: float = 60,
    extra_config: dict | None = None,
) -> list[Finding]:
    """Delegates to SemgrepScanner - the service's sole general-purpose,
    multi-language SAST engine. Per SCOPE.md, this is deliberately the
    only pattern-based security scanner in the bundle: Bandit, Bearer,
    and CodeQL were cut because their coverage overlapped this tool's
    rulesets without adding a capability worth a whole extra dependency.
    Growth to a new language's security coverage should mean enabling
    more of Semgrep's own rule registry, not adding another scanner.
    """
    return _run_scanner_adapter(SemgrepScanner(), context, timeout_seconds, extra_config)


def run_osv(
    context: ChangeContext,
    timeout_seconds: float = 60,
) -> list[Finding]:
    """Delegates to OSVScanner - the service's sole dependency-
    vulnerability scanner. OSV.dev's database already aggregates PyPA's
    advisory feed alongside every other ecosystem it covers, so this
    alone also covers what a Python-only tool (pip-audit) would - see
    SCOPE.md for why pip-audit was cut rather than kept in parallel.
    """
    return _run_scanner_adapter(OSVScanner(), context, timeout_seconds)


def run_deterministic(
    context: ChangeContext,
    timeout_seconds: float = 60,
    extra_config: dict | None = None,
) -> list[Finding]:
    findings: list[Finding] = []

    findings.extend(_path_traversal_findings(context))
    findings.extend(run_ruff(context, timeout_seconds=timeout_seconds))
    findings.extend(
        run_semgrep(context, timeout_seconds=timeout_seconds, extra_config=extra_config)
    )
    findings.extend(run_osv(context, timeout_seconds=timeout_seconds))

    return findings
