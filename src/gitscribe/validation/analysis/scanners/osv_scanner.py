from __future__ import annotations

import json
import shutil
from pathlib import Path

from gitscribe.validation.analysis.models import AnalysisScope, ProcessResult, ScannerCommand
from gitscribe.validation.analysis.runner import ScannerExecutionError
from gitscribe.validation.analysis.scanner import OptionalScannerHooks
from gitscribe.validation.models import Finding, Severity

# Lockfiles/manifests that trigger a scan. Mirrors run_pip_audit()'s
# `dependency_files` gate in deterministic.py
_DEPENDENCY_FILES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "poetry.lock",
    "uv.lock",
    "Pipfile",
    "Pipfile.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
}

_SEVERITY_MAP: dict[str, Severity] = {
    "critical": "critical",
    "high": "high",
    "moderate": "medium",
    "medium": "medium",
    "low": "low",
}

class OSVScanner(OptionalScannerHooks):
    """OSV-Scanner adapter - multi-ecosystem dependency vulnerability
    scanning (npm, Go, Rust, Ruby, etc.), the gap pip-audit's Python-only
    scope leaves - same relationship Betterleaks/Bandit have (one tool
    covers Python, this one covers everything else).
    """
    name = "osv-scanner"
    category = "dependency"

    def supports(self, scope: AnalysisScope) -> bool:
        return any(Path(f).name in _DEPENDENCY_FILES for f in scope.files)

    def preflight(self) -> str | None:
        return None if shutil.which("osv-scanner") else "osv-scanner not found on PATH"

    def build_command(self, scope: AnalysisScope, config:dict) -> ScannerCommand | None:
        if not self.supports(scope):
            return None

        return ScannerCommand(
            args = ["osv-scanner", "scan", "source", "--format", "json", "."],
            timeout_seconds=config.get("timeout_seconds", 60),
            network_required= True,
        )

    def parse(self, result: ProcessResult, scope: AnalysisScope) -> list[Finding]:
        # 0 = no vulnerabilities, 1 = vulnerabilities found, 128 = no
        # packages found in the scanned tree (an empty scan, not a
        # failure - e.g. a lockfile changed but nothing was resolvable).
        # Anything else is a genuine tool failure.
        if result.returncode == 128:
            return []

        if result.returncode not in (0, 1):
            raise ScannerExecutionError(
                "osv-scanner failed: " + (result.stderr.strip() or "no diagnostic output")
            )

        try:
            raw = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ScannerExecutionError("osv-scanner returned malformed JSON") from exc

        findings: list[Finding] = []

        for entry in raw.get("results", []):
            source_path = (entry.get("source") or {}).get("path")

            for pkg_entry in entry.get("packages", []):
                package = pkg_entry.get("package", {})
                pkg_name = package.get("name", "unknown")
                pkg_version = package.get("version", "unknown")
                ecosystem = package.get("ecosystem", "")

                for vuln in pkg_entry.get("vulnerabilities", []):
                    vuln_id = vuln.get("id", "OSV")
                    severity = severity_of(vuln)
                    aliases = vuln.get("aliases") or []

                    findings.append(
                        Finding(
                            id=f"osv:{vuln_id}:{pkg_name}:{pkg_version}",
                            source="deterministic",
                            category="dependency",
                            rule_id=vuln_id,
                            severity=severity,
                            confidence="high",
                            file=source_path,
                            line=None,
                            message=(
                                f"{pkg_name}@{pkg_version} ({ecosystem}) has known "
                                f"vulnerability {vuln_id}"
                                + (f" ({', '.join(aliases)})" if aliases else "")
                            ),
                            evidence=(vuln.get("summary") or "").strip()[:500]
                            or f"Matched against the OSV.dev database for {ecosystem}.",
                            remediation=(
                                f"Upgrade {pkg_name} past the vulnerable version range. "
                                f"See https://osv.dev/vulnerability/{vuln_id} for fixed versions."
                            ),
                            sources=["osv-scanner"],
                        )
                    )

        return findings

def severity_of(vuln: dict) -> Severity:
    """Best-effort: prefers OSV's plain-word `database_specific.severity`
    when present.
    """
    word = str((vuln.get("database_specific") or {}).get("severity", "")).lower()
    return _SEVERITY_MAP.get(word, "medium")
