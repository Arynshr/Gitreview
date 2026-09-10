from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from gitscribe.validation.analysis.models import AnalysisScope, ProcessResult, ScannerCommand
from gitscribe.validation.analysis.runner import ScannerExecutionError
from gitscribe.validation.analysis.scanner import OptionalScannerHooks
from gitscribe.validation.models import Finding


class GitleaksScanner(OptionalScannerHooks):
    """Gitleaks adapter - secret/credential detection, per
    validation_layer.md §10.3: "required: true on every ExecutionPlan,
    set independently of the planner's per-file/per-language capability
    matching. It is not subject to risk-based or file-type-based
    selection, and no AI/LLM component in the system has authority to
    omit it."

    `supports()` therefore always returns True - unlike every other
    scanner, this one is never conditionally skipped based on scope
    contents. (The full §10.3 guarantee - independent of the planner's
    capability matching - is completed once the Planner node exists,
    §4a.2; today `validation/orchestrator.py` calls this unconditionally
    on the whole change, ahead of and independent of the static/agentic
    mode split, for the same reason - see orchestrator.py.)
    """

    name = "gitleaks"
    category = "security"

    def supports(self, scope: AnalysisScope) -> bool:
        return True

    def preflight(self) -> str | None:
        return None if shutil.which("gitleaks") else "gitleaks not found on PATH"

    def build_command(self, scope: AnalysisScope, config: dict) -> ScannerCommand | None:
        # Gitleaks writes its report to a file, not stdout - there's no
        # "--report-path -" support to stream JSON through the pipe the
        # generic runner captures. The report path is threaded through
        # via the command's own args (see parse(), which reads it back
        # out of ProcessResult.args) rather than adding a scanner-specific
        # side channel to ScannerCommand/ProcessResult.
        report_fd, report_path = tempfile.mkstemp(prefix="gitscribe-gitleaks-", suffix=".json")
        os.close(report_fd)

        return ScannerCommand(
            args=[
                "gitleaks",
                "detect",
                "--source",
                ".",
                "--no-banner",
                "--no-git",
                "--report-format",
                "json",
                "--report-path",
                report_path,
            ],
            timeout_seconds=config.get("timeout_seconds", 60),
        )

    def parse(self, result: ProcessResult, scope: AnalysisScope) -> list[Finding]:
        # Gitleaks: 0 = no leaks found, 1 = leaks found. Anything else is
        # a genuine tool failure (bad args, can't read source, etc.).
        if result.returncode not in (0, 1):
            raise ScannerExecutionError(
                "gitleaks failed: " + (result.stderr.strip() or "no diagnostic output")
            )

        report_path = None
        if "--report-path" in result.args:
            report_path = result.args[result.args.index("--report-path") + 1]

        if not report_path or not Path(report_path).is_file():
            return []

        try:
            raw_text = Path(report_path).read_text(encoding="utf-8").strip()
        finally:
            Path(report_path).unlink(missing_ok=True)

        if not raw_text:
            return []

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ScannerExecutionError("gitleaks returned malformed JSON") from exc

        # `--source .` scans the whole working tree (gitleaks has no
        # notion of "just these changed files"); filter down to this
        # scope's files here so a pre-existing secret elsewhere in the
        # repo isn't reported as if it were introduced by this change.
        # This is a scope filter, not a mandatory-scan opt-out - the scan
        # itself still always covers the whole tree (§10.3).
        in_scope = set(scope.files)

        findings: list[Finding] = []

        for item in raw:
            file = item.get("File")

            if file is not None and file not in in_scope:
                continue

            rule_id = item.get("RuleID", "GITLEAKS")

            findings.append(
                Finding(
                    id=f"gitleaks:{rule_id}:{file}:{item.get('StartLine', 0)}",
                    source="deterministic",
                    category="secret",
                    rule_id=rule_id,
                    severity="critical",
                    confidence="high",
                    file=file,
                    line=item.get("StartLine"),
                    message=f"Potential secret detected: {item.get('Description', rule_id)}",
                    evidence=(
                        "Gitleaks matched a secret-shaped pattern at this "
                        "location (matched value withheld from this report)."
                    ),
                    remediation=(
                        "Revoke/rotate the exposed credential, remove it from "
                        "the change (and history if already committed), and "
                        "use a secrets manager or environment variable instead."
                    ),
                    sources=["gitleaks"],
                )
            )

        return findings
