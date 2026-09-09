from __future__ import annotations

from gitscribe.validation.analysis.models import AnalysisScope, ProcessResult, ScannerCommand
from gitscribe.validation.models import Finding


class _NotImplementedScanner:
    """Placeholder adapter for a tool the Required Implementation
    Sequence (validation_layer.md) hasn't reached yet.

    Registered via registry.build_default_registry() with
    implemented=False so ScannerRegistry.implemented()/.for_category()
    never select these into a real run. build_command()/parse() raise
    rather than silently returning "no findings" if something ever calls
    them directly - a planner bug that selects a stub must fail loudly,
    not masquerade as a clean scan.
    """

    def __init__(self, name: str, category: str) -> None:
        self.name = name
        self.category = category

    def supports(self, scope: AnalysisScope) -> bool:
        return False

    def preflight(self) -> str | None:
        return f"{self.name} adapter not implemented yet"

    def build_command(self, scope: AnalysisScope, config: dict) -> ScannerCommand | None:
        raise NotImplementedError(f"{self.name} adapter not implemented yet")

    def parse(self, result: ProcessResult, scope: AnalysisScope) -> list[Finding]:
        raise NotImplementedError(f"{self.name} adapter not implemented yet")

    def health(self) -> dict:
        return {"available": False, "reason": self.preflight()}

STUB_SCANNERS = [
    _NotImplementedScanner("gitleaks", "security"),
    _NotImplementedScanner("semgrep", "security"),
    _NotImplementedScanner("syft", "dependency"),
    _NotImplementedScanner("osv-scanner", "dependency"),
    _NotImplementedScanner("trivy", "security"),
    _NotImplementedScanner("eslint", "quality"),
    _NotImplementedScanner("bearer", "security"),
    _NotImplementedScanner("clang-tidy", "quality"),
    _NotImplementedScanner("codeql", "security"),
]
