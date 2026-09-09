from __future__ import annotations

import json
import shutil
from pathlib import Path

from gitscribe.validation.analysis.models import AnalysisScope, ProcessResult, ScannerCommand
from gitscribe.validation.analysis.runner import ScannerExecutionError
from gitscribe.validation.analysis.scanner import OptionalScannerHooks
from gitscribe.validation.models import Confidence, Finding, Severity

_BANDIT_SEVERITY: dict[str, Severity] = {
    "LOW": "low",
    "MEDIUM": "medium",
    "HIGH": "high",
}
_BANDIT_CONFIDENCE: dict[str, Confidence] = {
    "LOW": "low",
    "MEDIUM": "medium",
    "HIGH": "high",
}

# Bandit's own severity rating. This is the escalation ruff_scanner.py's
# docstring already claims lives here ("the S105-S107/critical-code
# severity escalation ... now live exclusively in
# scanners/bandit_scanner.py") - it must exist for that claim to be true,
_HARDCODED_CREDENTIAL_TEST_IDS = {"B105", "B106", "B107"}


class BanditScanner(OptionalScannerHooks):
    """Bandit adapter - Python-specific security rules only, per
    validation_layer.md section 7's Tool Responsibility Matrix ("Do not
    use it for: Generic multi-language SAST"). This is now the sole
    source of Python security findings; RuffScanner's `S`-rule family was
    removed in the same change that introduced this adapter (Required
    Implementation Sequence step 7) to avoid both tools reporting the
    same source line under different rule-ID namespaces.
    """

    name = "bandit"
    category = "security"

    def supports(self, scope: AnalysisScope) -> bool:
        return any(f.lower().endswith(".py") for f in scope.files)

    def preflight(self) -> str | None:
        return None if shutil.which("bandit") else "bandit not found on PATH"

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
                "bandit",
                "--format",
                "json",
                "--quiet",
                *python_files,
            ],
            timeout_seconds=config.get("timeout_seconds", 60),
        )

    def parse(self, result: ProcessResult, scope: AnalysisScope) -> list[Finding]:
        # Bandit: 0 = no issues found, 1 = issues found at/above the
        # configured threshold. Any other code (e.g. 2 = usage/parse
        # error) is a genuine tool failure, not a scan result.
        if result.returncode not in (0, 1):
            raise ScannerExecutionError(
                "bandit failed: " + (result.stderr.strip() or "no diagnostic output")
            )

        try:
            raw = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ScannerExecutionError("bandit returned malformed JSON") from exc

        findings: list[Finding] = []

        for item in raw.get("results", []):
            test_id = str(item.get("test_id") or "BANDIT")
            is_credential = test_id in _HARDCODED_CREDENTIAL_TEST_IDS

            severity: Severity = (
                "critical"
                if is_credential
                else _BANDIT_SEVERITY.get(item.get("issue_severity", "MEDIUM"), "medium")
            )
            confidence: Confidence = _BANDIT_CONFIDENCE.get(
                item.get("issue_confidence", "MEDIUM"), "medium"
            )

            findings.append(
                Finding(
                    id=(
                        f"bandit:{test_id}:"
                        f"{item.get('filename')}:"
                        f"{item.get('line_number', 0)}"
                    ),
                    source="deterministic",
                    category="hardcoded credential" if is_credential else "security",
                    rule_id=test_id,
                    severity=severity,
                    confidence=confidence,
                    file=item.get("filename"),
                    line=item.get("line_number"),
                    message=item.get("issue_text", "Bandit finding"),
                    evidence=(
                        f"Bandit rule {test_id} ({item.get('test_name', '')}) "
                        "reported this location in the changed file."
                    ),
                    remediation=(
                        "Review the reported rule and apply the recommended "
                        f"secure pattern. See {item.get('more_info') or 'the Bandit documentation'}."
                    ),
                    sources=["bandit"],
                )
            )

        return findings
