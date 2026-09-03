from __future__ import annotations

import time

from gitscribe.validation.aggregator import aggregate
from gitscribe.validation.ai import (
    AIReviewError,
    review_locally,
)
from gitscribe.validation.deterministic import (
    DeterministicAnalysisError,
    run_deterministic,
)
from gitscribe.validation.models import (
    AnalysisError,
    ChangeContext,
    ValidationResult,
)
from gitscribe.validation.policy import evaluate


def validate_change(
    context: ChangeContext,
    cfg: dict,
    force_ai: bool = False,
) -> ValidationResult:
    timings: dict[str, float] = {}
    findings = []
    errors: list[AnalysisError] = []

    if not context.diff.strip():
        return ValidationResult(
            status="PASS",
            timings=timings,
        )

    started = time.perf_counter()

    if cfg.get(
        "deterministic",
        {},
    ).get(
        "enabled",
        True,
    ):
        try:
            findings.extend(
                run_deterministic(context)
            )
        except DeterministicAnalysisError as exc:
            errors.append(
                AnalysisError(
                    source="deterministic",
                    message=str(exc),
                )
            )

    timings["deterministic_s"] = (
        time.perf_counter() - started
    )

    started = time.perf_counter()

    run_ai = force_ai or cfg.get(
        "ai",
        {},
    ).get(
        "enabled",
        True,
    )

    if run_ai:
        try:
            findings.extend(
                review_locally(
                    context,
                    cfg["ai"],
                    [
                        finding
                        for finding in findings
                        if finding.source
                        == "deterministic"
                    ],
                )
            )
        except AIReviewError as exc:
            errors.append(
                AnalysisError(
                    source="ai",
                    message=str(exc),
                )
            )

    timings["ai_s"] = (
        time.perf_counter() - started
    )

    findings = aggregate(findings)

    status = (
        "PASS"
        if evaluate(
            findings,
            cfg,
            errors=bool(errors),
            diff=context.diff,
        )
        else "FAIL"
    )

    return ValidationResult(
        status=status,
        findings=findings,
        errors=errors,
        timings=timings,
    )
