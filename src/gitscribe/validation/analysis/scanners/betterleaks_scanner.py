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


class BetterleaksScanner(OptionalScannerHooks):
    """Betterleaks adapter - secret detection, replacing Gitleaks (same
    author, positioned as its direct successor: BPE-tokenization
    detection instead of entropy, CLI flags/config largely compatible).
    Per §10.3, mandatory and not subject to capability/risk-based
    selection, so `supports()` always returns True. `orchestrator.py`
    calls this unconditionally, ahead of the static/agentic mode split.

    Report schema note: Betterleaks' JSON `Finding` struct nests the file
    path under `Fragment.FilePath`/`Attributes.path` rather than a
    top-level `File` key like Gitleaks had - `_file_of()` below checks
    both, plus a plain `File` key, since this is based on published
    examples of a very new (2026), fast-moving tool rather than something
    tested directly against a running binary. Verify against your
    installed version's actual output if findings aren't showing up.
    """

    name = "betterleaks"
    category = "security"

    def supports(self, scope: AnalysisScope) -> bool:
        return True

    def preflight(self) -> str | None:
        return None if shutil.which("betterleaks") else "betterleaks not found on PATH"

    def build_command(self, scope: AnalysisScope, config: dict) -> ScannerCommand | None:
        # Writes its report to a file, not stdout (same as Gitleaks).
        # Threaded through the command's own args; parse() reads the path
        # back out of ProcessResult.args.
        report_fd, report_path = tempfile.mkstemp(prefix="gitscribe-betterleaks-", suffix=".json")
        os.close(report_fd)

        args = [
            "betterleaks",
            "dir",
            ".",
            "--no-banner",
            "--report-format",
            "json",
            "--report-path",
            report_path,
        ]

        # Live validation calls out to the credential's own provider API
        # (e.g. AWS, GitHub) to confirm it's an active secret - a real
        # network egress + third-party call this tool didn't make before,
        # from what may be an unattended hook/CI run. Off by default;
        # opt in via validation.deterministic.betterleaks_live_validation.
        if config.get("betterleaks_live_validation", False):
            args.append("--validation")

        return ScannerCommand(
            args=args,
            timeout_seconds=config.get("timeout_seconds", 60),
            network_required=bool(config.get("betterleaks_live_validation", False)),
        )

    def parse(self, result: ProcessResult, scope: AnalysisScope) -> list[Finding]:
        # 0 = no leaks, 1 = leaks found; anything else is a tool failure.
        # Assumed consistent with Gitleaks' convention - not independently
        # confirmed against a running Betterleaks binary.
        if result.returncode not in (0, 1):
            raise ScannerExecutionError(
                "betterleaks failed: " + (result.stderr.strip() or "no diagnostic output")
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
            raise ScannerExecutionError("betterleaks returned malformed JSON") from exc

        # Scans the whole tree; filter to this scope's files so a
        # pre-existing secret elsewhere isn't reported as new here.
        in_scope = set(scope.files)

        findings: list[Finding] = []

        for item in raw:
            file = _file_of(item)

            if file is not None and file not in in_scope:
                continue

            rule_id = item.get("RuleID", "BETTERLEAKS")

            findings.append(
                Finding(
                    id=f"betterleaks:{rule_id}:{file}:{item.get('StartLine', 0)}",
                    source="deterministic",
                    category="secret",
                    rule_id=rule_id,
                    severity="critical",
                    confidence="high",
                    file=file,
                    line=item.get("StartLine"),
                    message=f"Potential secret detected: {item.get('Description', rule_id)}",
                    evidence=(
                        "Betterleaks matched a secret-shaped pattern at this "
                        "location (matched value withheld from this report)."
                        + (" Confirmed live via provider validation." if _validated(item) else "")
                    ),
                    remediation=(
                        "Revoke/rotate the exposed credential, remove it from "
                        "the change (and history if already committed), and "
                        "use a secrets manager or environment variable instead."
                    ),
                    sources=["betterleaks"],
                )
            )

        return findings


def _file_of(item: dict) -> str | None:
    """Betterleaks' JSON schema (per published examples) nests the path
    under `Fragment.FilePath` or `Attributes.path`; `File` is checked too
    in case a given build still emits it Gitleaks-style.
    """
    fragment = item.get("Fragment") or {}
    if fragment.get("FilePath"):
        return fragment["FilePath"]

    attributes = item.get("Attributes") or {}
    if attributes.get("path"):
        return attributes["path"]

    return item.get("File")


def _validated(item: dict) -> bool:
    """Best-effort read of live-validation status, when
    `--validation` was passed - field name not confirmed against a real
    validated-finding example, only inferred from the `--validation`
    flag's existence. Treated as informational (folded into `evidence`),
    never used to change severity - an unvalidated finding is still a
    real secret in the diff either way.
    """
    attributes = item.get("Attributes") or {}
    status = str(attributes.get("validation_status") or attributes.get("status") or "").lower()
    return status in {"valid", "verified", "true"}
