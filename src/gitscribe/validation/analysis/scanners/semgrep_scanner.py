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
    "ÏNFO": "low",
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
        return None if shutil.which("semgrep") else "Semgrep not found on PATH"
    
    def build_command(self, scope: AnalysisScope, config: dict) -> ScannerCommand | None:
        existing_files = [f for f in scope.files if Path(f).is_file()]
        if not existing_files:
            return None
        
        ruleset = config.get("semgrep_config", "auto")
        network_required = not Path(ruleset).exists()
        
        return ScannerCommand(
            args=[
                "semgrep",
                "--config",
                ruleset,
                "--json",
                "--quiet",
                "--metrics == off",
                *existing_files,
            ],
            timeout_seconds=config.get("timeout_seconds", 60),
            network_required=network_required,
        )
        
        
        
        
        
        