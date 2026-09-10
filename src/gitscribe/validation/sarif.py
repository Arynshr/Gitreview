"""
Cross-tool unified report: SARIF 2.1.0.

`Finding` (validation/models.py) is already the one schema every scanner
(ruff, bandit, gitleaks, pip-audit, path-check, the AI pass) normalizes
into - that part of "unified reporting" already existed before this
module. What was missing is a report *format* that downstream tooling
(GitHub code scanning, most CI dashboards, IDE SARIF viewers) actually
understands, so a mixed-tool run reads as one coherent report instead of
GitScribe-specific JSON/text only `gitscribe verify` itself can render.

This module has exactly one job: `Finding` -> SARIF. It does not
re-derive pass/fail (that's `validation.policy.evaluate`, already done by
the time a `ValidationResult` reaches here) and does not talk to the
filesystem beyond what the caller (`validation/cli.py`) does with the
string this returns.
"""

from __future__ import annotations

import json

from gitscribe.validation.models import Finding, Severity, ValidationResult

# SARIF's `level` is a closed, small vocabulary - it does not have five
# severities. `critical`/`high` collapse to "error" (fails a gate/blocks
# a PR in every SARIF consumer that treats "error" as blocking), `medium`
# to "warning", and `low`/`info` to "note". This is a real information
# loss relative to GitScribe's own five-value Severity, which is why the
# native `--json` output (unchanged) remains the source of truth for
# anything that needs the finer-grained value - SARIF is an additional,
# normalized view for external tooling, not a replacement.
_SARIF_LEVEL: dict[Severity, str] = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
    "info": "note",
}


def _rule(finding: Finding) -> dict:
    return {
        "id": finding.rule_id,
        "name": finding.rule_id,
        "shortDescription": {"text": f"{finding.category} ({finding.rule_id})"},
        "properties": {
            "category": finding.category,
            # SARIF has no native "confidence" - carried as a rule
            # property rather than dropped, so a consumer that does look
            # for it (or a human reading the raw file) still has it.
            "gitscribeConfidence": finding.confidence,
            "gitscribeSource": finding.source,
        },
    }


def _result(finding: Finding) -> dict:
    location: dict = {
        "physicalLocation": {
            "artifactLocation": {"uri": finding.file or "(repository)"},
        }
    }

    if finding.line is not None:
        location["physicalLocation"]["region"] = {"startLine": finding.line}

    return {
        "ruleId": finding.rule_id,
        "level": _SARIF_LEVEL.get(finding.severity, "warning"),
        "message": {
            "text": (
                f"{finding.message}\n\nEvidence: {finding.evidence}\n"
                f"Remediation: {finding.remediation}"
            )
        },
        "locations": [location],
        "partialFingerprints": {"gitscribeFindingId": finding.id},
    }


def to_sarif(result: ValidationResult, tool_name: str = "GitScribe", tool_version: str = "0.1") -> str:
    """Render a `ValidationResult` as a SARIF 2.1.0 log (one `run`, one
    synthetic `tool.driver` representing GitScribe's own aggregated
    output - not one SARIF run per underlying scanner, since aggregation/
    dedup across scanners has already happened in
    `validation.aggregator.aggregate` by the time a `ValidationResult`
    exists, and SARIF has no clean way to say "this one finding was
    produced by two tools that agreed" without re-introducing that
    distinction. Each `rule.id` still equals the originating scanner's
    own rule ID (e.g. a Ruff or Bandit code, `GITLEAKS`, `AI-security`),
    and `gitscribeSource`/`gitscribeConfidence` rule properties preserve
    which pipeline (deterministic vs. ai) produced it.
    """
    findings = result.findings
    rules_by_id = {finding.rule_id: _rule(finding) for finding in findings}

    sarif = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": tool_name,
                        "version": tool_version,
                        "informationUri": "https://github.com/gitscribe/gitscribe",
                        "rules": list(rules_by_id.values()),
                    }
                },
                "results": [_result(finding) for finding in findings],
            }
        ],
    }

    return json.dumps(sarif, indent=2)
