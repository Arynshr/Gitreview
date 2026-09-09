from __future__ import annotations

import logging
from dataclasses import dataclass

from gitscribe.validation.analysis.scanner import Scanner

logger = logging.getLogger("gitscribe.validation.analysis.registry")


@dataclass
class ScannerRegistration:
    scanner: Scanner
    implemented: bool = True
    unsupported_reason: str | None = None


class ScannerRegistry:
    """Central lookup of every scanner adapter gitscribe knows about,
    keyed by Scanner.name.

    This is the one place validation code should ask "what scanners
    exist" / "which adapters handle category X"

    `implemented=False` entries (see scanners/stubs.py) exist so the
    target shape of the full registry is visible ahead of the Required
    Implementation Sequence reaching them - callers that only want
    scanners actually safe to run must use .implemented()/.for_category(),
    never .all() directly, which also returns stubs.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, ScannerRegistration] = {}

    def register(
        self,
        scanner: Scanner,
        *,
        implemented: bool = True,
        unsupported_reason: str | None = None,
    ) -> None:
        if scanner.name in self._by_name:
            raise ValueError(f"scanner already registered: {scanner.name}")

        self._by_name[scanner.name] = ScannerRegistration(
            scanner=scanner,
            implemented=implemented,
            unsupported_reason=unsupported_reason,
        )

        logger.debug(
            "registered scanner=%s category=%s implemented=%s",
            scanner.name,
            scanner.category,
            implemented,
        )

    def get(self, name: str) -> ScannerRegistration | None:
        reg = self._by_name.get(name)
        if reg is None:
            logger.debug("lookup for unknown scanner=%s", name)
        elif not reg.implemented:
            logger.debug(
                "lookup for not-yet-implemented scanner=%s: %s",
                name,
                reg.unsupported_reason,
            )
        return reg

    def all(self) -> list[ScannerRegistration]:
        """Every registration, implemented or not. Prefer .implemented()
        unless you specifically need stub entries too (e.g. for a
        diagnostics/`gitscribe verify --list-scanners`-style command)."""
        return list(self._by_name.values())

    def implemented(self) -> list[Scanner]:
        return [r.scanner for r in self._by_name.values() if r.implemented]

    def for_category(self, category: str) -> list[Scanner]:
        return [s for s in self.implemented() if s.category == category]


def build_default_registry() -> ScannerRegistry:
    """Wires up every known adapter: 
    Stubs make the target registry/ExecutionPlan shape visible now
    without claiming capability gitscribe doesn't have: they are
    registered with implemented=False, so .implemented()/.for_category()
    never return them.

    Tree-sitter (structural parsing, step 17) is intentionally not
    registered here - it isn't a subprocess-based, finding-producing tool
    the Scanner protocol fits; it becomes a structural-context input to
    other adapters/the AI assessment stage instead, per section 4's
    "Code Analysis" branch.
    """
    registry = ScannerRegistry()

    from gitscribe.validation.analysis.scanners.bandit_scanner import BanditScanner
    from gitscribe.validation.analysis.scanners.ruff_scanner import RuffScanner

    registry.register(RuffScanner())
    registry.register(BanditScanner())

    from gitscribe.validation.analysis.scanners.stubs import STUB_SCANNERS

    for scanner in STUB_SCANNERS:
        registry.register(
            scanner,
            implemented=False,
            unsupported_reason=(
                f"{scanner.name} adapter not yet implemented - see "
                "validation_layer.md 'Required Implementation Sequence'"
            ),
        )

    return registry
