from __future__ import annotations

from gitscribe.validation.analysis.models import AnalysisScope, ProcessResult, ScannerCommand
from gitscribe.validation.models import Finding


class _NotImplementedScanner:
    """Placeholder adapter for a tool genuinely planned but not yet
    built. Kept as infrastructure even though STUB_SCANNERS is
    currently empty (see below) - this is the mechanism to use if a new
    scanner is deliberately added to scope later, not a parking lot for
    "maybe someday" tools.

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


STUB_SCANNERS: list[_NotImplementedScanner] = [
    # Empty by design (see /SCOPE.md). Previously listed Bandit, Bearer,
    # Clang-Tidy, CodeQL, ESLint, Trivy, and Syft as pending work for a
    # "Required Implementation Sequence" that kept growing the tool
    # count. That master-plan approach to scope is the thing being
    # abandoned here, not any single tool on the old list - the service
    # now has four scanners total (ruff, semgrep, betterleaks, osv) and
    # no backlog of more to add by default. Syft/SBOM generation
    # specifically isn't coming back as a stub either way: it produces
    # an SBOM, not a list[Finding], so it was never going to fit this
    # protocol - see the removed validation/sbom.py in version control
    # if SBOM generation becomes a real, separately-scoped feature later.
]
