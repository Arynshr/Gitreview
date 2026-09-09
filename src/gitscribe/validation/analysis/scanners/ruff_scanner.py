from __future__ import annotations

import json
import shutil
from pathlib import Path

from gitscribe.validation.analysis.models import AnalysisScope, ProcessResult, ScannerCommand
from gitscribe.validation.analysis.runner import ScannerExecutionError
from gitscribe.validation.analysis.scanner import OptionalScannerHooks
from gitscribe.validation.models import Finding


class RuffScanner(OptionalScannerHooks):
    """Ruff adapter - Python code quality / syntax / pyflakes-style
    checks only, per validation_layer.md section 7's Tool Responsibility
    Matrix ("Do not use it for: Canonical Python security scanning").

    Ruff's `S` (flake8-bandit) rule family and the S105-S107/critical-code
    severity escalation it used to carry during the Ruff-only migration
    window have been removed and now live exclusively in
    scanners/bandit_scanner.py (Required Implementation Sequence step 7).
    Selecting "S" here as well would duplicate every finding Bandit
    already reports for the same source lines under a different rule-ID
    namespace - exactly what the Tool Responsibility Matrix exists to
    prevent (section 7: "mandatory to prevent duplicate findings and
    redundant work").
    """

    name = "ruff"
    category = "quality"

    def supports(self, scope: AnalysisScope) -> bool:
        return any(f.lower().endswith(".py") for f in scope.files)

    def preflight(self) -> str | None:
        return None if shutil.which("ruff") else "ruff not found on PATH"

    def build_command(self, scope: AnalysisScope, config: dict) -> ScannerCommand | None:
        python_files = [
            f
            for f in scope.files
            if f.lower().endswith(".py") and Path(f).is_file()
        ]

        if not python_files:
            return None

        return ScannerCommand(
            args=[
                "ruff",
                "check",
                # E9 (syntax errors) + F (pyflakes) only - quality/
                # correctness checks. No "S": Bandit is now the sole
                # source of Python security findings.
                "--select",
                "E9,F",
                "--output-format=json",
                "--no-fix",
                *python_files,
            ],
            timeout_seconds=config.get("timeout_seconds", 60),
        )

    def parse(self, result: ProcessResult, scope: AnalysisScope) -> list[Finding]:
        if result.returncode not in (0, 1):
            raise ScannerExecutionError(
                "ruff failed: " + (result.stderr.strip() or "no diagnostic output")
            )

        try:
            raw = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ScannerExecutionError("ruff returned malformed JSON") from exc

        findings: list[Finding] = []

        for item in raw:
            code = str(item.get("code") or "RUFF")

            findings.append(
                Finding(
                    id=(
                        f"ruff:{code}:"
                        f"{item.get('filename')}:"
                        f"{item.get('location', {}).get('row', 0)}"
                    ),
                    source="deterministic",
                    category="validation",
                    rule_id=code,
                    severity="medium",
                    confidence="high",
                    file=item.get("filename"),
                    line=item.get("location", {}).get("row"),
                    message=item.get("message", "Ruff finding"),
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
