"""Scanner-adapter analysis layer for gitscribe.validation.

This package implements the domain contracts and shared execution
machinery described in validation_layer.md sections 5-7: AnalysisScope /
ExecutionPlan (models.py), the common subprocess runner (runner.py), the
Scanner adapter protocol (scanner.py), and the ScannerRegistry
(registry.py). Individual scanner adapters live under
gitscribe.validation.analysis.scanners.

Per validation_layer.md's "Required Implementation Sequence", this is a
staged migration: as of this change the Ruff and Bandit adapters are
implemented end-to-end. Everything else (Semgrep, Gitleaks, Syft, OSV,
Trivy, ESLint, Bearer, Clang-Tidy, CodeQL) is registered as an explicit
not-yet-implemented stub so the target registry/plan shape is visible,
without claiming capability that doesn't exist yet.
"""
