from __future__ import annotations

import json
import shutil
from pathlib import Path

from gitscribe.validation.analysis.models import AnalysisScope, ProcessResult, ScannerCommand
from gitscribe.validation.analysis.runner import ScannerExecutionError
from gitscribe.validation.analysis.scanner import OptionalScannerHooks
from gitscribe.validation.models import Confidence, Finding, Severity

_SEMGREP_SEVERITY: dict[str, Severity] = {
    "ERROR": "high",
    "WARNING": "medium",
    "INFO": "low",
}
_SEMGREP_CONFIDENCE: dict[str, Confidence] = {
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
}


class SemgrepScanner(OptionalScannerHooks):
    """Semgrep adapter - generic multi-language SAST, the gap Bandit's
    Python-only scope leaves (Bandit is not to be used for that, per its
    own docstring). Runs on the whole file set Betterleaks/Bandit see,
    not gated to a single extension - Semgrep silently no-ops on file
    types it has no rules for, so there's no need to pre-filter here.
    """

    name = "semgrep"
    category = "security"

    def supports(self, scope: AnalysisScope) -> bool:
        return bool(scope.files)

    def preflight(self) -> str | None:
        return None if shutil.which("semgrep") else "semgrep not found on PATH"

    def build_command(self, scope: AnalysisScope, config: dict) -> ScannerCommand | None:
        existing_files = [f for f in scope.files if Path(f).is_file()]

        if not existing_files:
            return None

        ruleset = config.get("semgrep_config", "p/default")
        # A local path doesn't need the registry; anything else ("auto",
        # "p/...", a bare ruleset name) does - see config_schema.py's
        # semgrep_config description for the offline alternative.
        network_required = not Path(ruleset).exists()

        return ScannerCommand(
            args=[
                "semgrep",
                "--config",
                ruleset,
                "--json",
                "--quiet",
                "--metrics=off",  # no usage telemetry upload - local-first
                *existing_files,
            ],
            timeout_seconds=config.get("timeout_seconds", 60),
            network_required=network_required,
        )

    def parse(self, result: ProcessResult, scope: AnalysisScope) -> list[Finding]:
        # Semgrep's exit code reflects scan execution, not finding count
        # (no --error flag is passed) - 0 is the only success code.
        if result.returncode != 0:
            raise ScannerExecutionError(
                "semgrep failed: " + (result.stderr.strip() or "no diagnostic output")
            )

        try:
            raw = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ScannerExecutionError("semgrep returned malformed JSON") from exc

        findings: list[Finding] = []

        for item in raw.get("results", []):
            check_id = item.get("check_id", "SEMGREP")
            extra = item.get("extra", {})
            metadata = extra.get("metadata", {})

            findings.append(
                Finding(
                    id=f"semgrep:{check_id}:{item.get('path')}:{item.get('start', {}).get('line', 0)}",
                    source="deterministic",
                    category="security",
                    rule_id=check_id,
                    severity=_SEMGREP_SEVERITY.get(extra.get("severity", "WARNING"), "medium"),
                    confidence=_SEMGREP_CONFIDENCE.get(metadata.get("confidence", "MEDIUM"), "medium"),
                    file=item.get("path"),
                    line=item.get("start", {}).get("line"),
                    message=extra.get("message", check_id),
                    evidence=f"Semgrep rule {check_id} matched this location.",
                    remediation=(
                        (metadata.get("references") or [None])[0]
                        or "Review the matched pattern and apply the rule's recommended fix."
                    ),
                    sources=["semgrep"],
                )
            )

        return findings
