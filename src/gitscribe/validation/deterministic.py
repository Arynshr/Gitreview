from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

from gitscribe.validation.models import ChangeContext, Finding


class DeterministicAnalysisError(RuntimeError):
    pass


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DeterministicAnalysisError(
            f"required scanner not found: {args[0]}"
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


def run_ruff(
    context: ChangeContext,
) -> list[Finding]:
    python_files = [
        file
        for file in context.files
        if file.lower().endswith(".py")
        and Path(file).is_file()
    ]

    if not python_files:
        return []

    result = _run(
        [
            "ruff",
            "check",
            "--output-format=json",
            "--no-fix",
            *python_files,
        ]
    )

    if result.returncode not in (0, 1):
        raise DeterministicAnalysisError(
            "ruff failed: "
            + (result.stderr.strip() or "no diagnostic output")
        )

    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise DeterministicAnalysisError(
            "ruff returned malformed JSON"
        ) from exc

    findings: list[Finding] = []

    # Well-known bandit-derived codes for injection/unsafe-deserialization
    # classes that warrant escalation above the flat "high" every other
    # S-rule gets. Deliberately a short, high-confidence list rather than
    # an attempt to re-grade the entire bandit rule set.
    critical_codes = {
        "S608",  # SQL injection via string-built query
        "S602",  # subprocess call with shell=True
        "S301",  # unsafe pickle deserialization
        "S302",  # unsafe marshal deserialization
    }

    for item in raw:
        code = str(item.get("code") or "RUFF")

        security = code.startswith("S")

        if code in {"S105", "S106", "S107"}:
            category = "secrets"
            severity = "high"
        elif code in critical_codes:
            category = "security"
            severity = "critical"
        elif security:
            category = "security"
            severity = "high"
        else:
            category = "validation"
            severity = "medium"

        findings.append(
            Finding(
                id=(
                    f"ruff:{code}:"
                    f"{item.get('filename')}:"
                    f"{item.get('location', {}).get('row', 0)}"
                ),
                source="deterministic",
                category=category,
                rule_id=code,
                severity=severity,
                confidence="high",
                file=item.get("filename"),
                line=item.get("location", {}).get("row"),
                message=item.get(
                    "message",
                    "Ruff finding",
                ),
                evidence=(
                    f"Ruff rule {code} reported this location "
                    "in the changed file."
                ),
                remediation=(
                    "Review the reported rule and apply the "
                    "recommended secure pattern."
                ),
                sources=["ruff"],
            )
        )

    return findings


def run_pip_audit(
    context: ChangeContext,
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
        ]
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
) -> list[Finding]:
    findings: list[Finding] = []

    findings.extend(_path_traversal_findings(context))
    findings.extend(run_ruff(context))
    findings.extend(run_pip_audit(context))

    return findings
