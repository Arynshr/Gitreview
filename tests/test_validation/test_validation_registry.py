"""Scanner registry & health preflight (`validation/analysis/registry.py`)."""

from __future__ import annotations

import pytest

from gitscribe.validation.analysis.registry import ScannerRegistry, build_default_registry
from gitscribe.validation.analysis.scanners.ruff_scanner import RuffScanner


def test_default_registry_has_the_closed_scanner_set() -> None:
    registry = build_default_registry()

    assert {s.name for s in registry.implemented()} == {"ruff", "betterleaks", "semgrep", "osv-scanner"}


def test_categories_are_routed_correctly() -> None:
    registry = build_default_registry()

    assert {s.name for s in registry.for_category("security")} == {"betterleaks", "semgrep"}
    assert [s.name for s in registry.for_category("dependency")] == ["osv-scanner"]
    assert [s.name for s in registry.for_category("quality")] == ["ruff"]


def test_stub_scanners_are_listed_but_never_implemented() -> None:
    registry = build_default_registry()

    for reg in registry.all():
        if not reg.implemented:
            assert reg.unsupported_reason
            assert reg.scanner.supports(None) is False  # stubs can never be planned
            assert reg.scanner.preflight()
            assert reg.scanner.name not in {s.name for s in registry.implemented()}


def test_unknown_scanner_lookup_returns_none() -> None:
    assert build_default_registry().get("gitscribe-path-check") is None
    assert build_default_registry().get("nope") is None


def test_duplicate_registration_is_rejected() -> None:
    registry = ScannerRegistry()
    registry.register(RuffScanner())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(RuffScanner())


@pytest.mark.parametrize("name", ["ruff", "semgrep", "osv-scanner", "betterleaks"])
def test_preflight_reports_missing_binary_and_passes_when_present(monkeypatch, name) -> None:
    scanner = build_default_registry().get(name).scanner

    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    assert scanner.preflight() == f"{name} not found on PATH"

    monkeypatch.setattr("shutil.which", lambda n, *a, **k: f"/bin/{n}")
    assert scanner.preflight() is None
