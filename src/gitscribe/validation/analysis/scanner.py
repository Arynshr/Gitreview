from __future__ import annotations

from typing import Protocol, runtime_checkable

from gitscribe.validation.analysis.models import AnalysisScope, ProcessResult, ScannerCommand
from gitscribe.validation.models import Finding


@runtime_checkable
class Scanner(Protocol):
    """One external tool, adapted to gitscribe's canonical pipeline.

    Per validation_layer.md section 6, this is the *only* shape a scanner
    integration takes - "Scanner-specific code must be adapters, not
    independent validation systems" (section 1). The three methods below
    are the required contract every adapter implements:

    - supports(scope): can this scanner do anything useful for this scope
      (e.g. RuffScanner.supports() checks for changed .py files)?
    - build_command(scope, config): what should the process runner
      execute? Returns None when supports() would also be False, or the
      scope resolves to nothing to scan for this tool.
    - parse(result, scope): turn a ProcessResult into canonical Findings.
      Raises ScannerExecutionError (validation.analysis.runner) for
      genuine tool failures (malformed output, unexpected exit code);
      finding a real vulnerability is never an error.

    `name` and `category` are plain class/instance attributes, not
    methods - `category` must be one of the Tool Responsibility Matrix
    categories in validation_layer.md section 7 (e.g. "security",
    "quality", "dependency"), since ScannerRegistry.for_category() and
    future policy/reporting code group adapters by it.
    """

    name: str
    category: str

    def supports(self, scope: AnalysisScope) -> bool: ...

    def build_command(self, scope: AnalysisScope, config: dict) -> ScannerCommand | None: ...

    def parse(self, result: ProcessResult, scope: AnalysisScope) -> list[Finding]: ...


class OptionalScannerHooks:
    """Default (no-op-ish) implementations of the optional lifecycle
    hooks from validation_layer.md section 6 ("Optional lifecycle hooks:
    preflight(), build_command(), run(), parse(), health()"). Concrete
    adapters may inherit from this to avoid re-declaring hooks they don't
    need to customize; it deliberately is NOT part of the `Scanner`
    Protocol above, so isinstance(x, Scanner) checks stay based only on
    the three required methods.
    """

    def preflight(self) -> str | None:
        """Return None if the underlying tool is available to run, else
        a short human-readable reason it isn't (e.g. missing binary).
        Used by ScannerRegistry/health checks (steps 19-20), not by
        parse()/build_command() themselves.
        """
        return None

    def run(self, scope: AnalysisScope, config: dict) -> ProcessResult | None:
        """Convenience: build_command() + run_scanner_command() in one
        call, for adapters/tests that don't need the two steps split
        apart. Not part of the required Protocol since some future
        callers (e.g. a concurrent planner) may want to build every
        command first and run them as a batch.
        """
        from gitscribe.validation.analysis.runner import run_scanner_command

        command = self.build_command(scope, config)  # type: ignore[attr-defined]
        if command is None:
            return None
        return run_scanner_command(command)

    def health(self) -> dict:
        return {"available": self.preflight() is None}
