from __future__ import annotations

import time

from gitscribe.core.diff_parser import get_raw_diff
from gitscribe.validation.aggregator import aggregate
from gitscribe.validation.ai import (
    AIReviewError,
    review_locally,
)
from gitscribe.validation.deterministic import (
    DeterministicAnalysisError,
    run_deterministic,
)
from gitscribe.validation.mode import (
    DEFAULT_MODE,
    resolve_file_modes,
    split_by_mode,
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
    force_mode: str | None = None,
    sandboxed: bool = False,
) -> ValidationResult:
    """`force_mode` (from `verify --mode`) overrides mode resolution for every
    file in `context` for this run — used together with `verify --path` to
    review a specific file via only one of the two engines. Without it, mode
    is resolved per file from `cfg["file_rules"]`, falling back to "both"
    (the pre-existing behavior) for anything unmatched.

    `sandboxed` gates whether the agentic pass is actually allowed to run at
    all. `validation/ai.py`'s local model is already loopback-only/read-only
    (the isolated sandbox), but that isolation must be an explicit opt-in at
    the CLI surface — mirroring `gitscribe review --sandboxed` — rather than
    silently assumed. If a file resolves to "agentic"/"both" and `sandboxed`
    is False, that file's AI pass is skipped and recorded as an error, which
    fails the gate under the default `fail_closed: true` (safe-by-default,
    per the advisory/gate rule: missing/blocked advisory signal -> don't
    proceed). Deterministic-only files are unaffected either way.
    """
    timings: dict[str, float] = {}
    findings = []
    errors: list[AnalysisError] = []

    if not context.diff.strip():
        return ValidationResult(
            status="PASS",
            timings=timings,
        )

    if force_mode is not None:
        modes = dict.fromkeys(context.files, force_mode)
    else:
        modes = resolve_file_modes(
            context.files,
            cfg.get("file_rules", []),
            default_mode=DEFAULT_MODE,
        )

    static_files, ai_files = split_by_mode(modes)

    started = time.perf_counter()

    if (
        cfg.get("deterministic", {}).get("enabled", True)
        and static_files
    ):
        try:
            det_context = context.model_copy(update={"files": static_files})
            findings.extend(
                run_deterministic(det_context)
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

    run_ai = (
        force_ai or cfg.get("ai", {}).get("enabled", True)
    ) and bool(ai_files)

    if run_ai and not sandboxed:
        errors.append(
            AnalysisError(
                source="ai",
                message=(
                    "agentic review requested for "
                    f"{ai_files} but --sandboxed was not passed; "
                    "the agentic pass only runs inside the isolated "
                    "local-model sandbox. Re-run with --sandboxed."
                ),
            )
        )
        run_ai = False

    if run_ai:
        try:
            ai_diff = (
                context.diff
                if set(ai_files) == set(context.files)
                else get_raw_diff(context.base, context.head, paths=ai_files)
            )
            ai_context = context.model_copy(
                update={"files": ai_files, "diff": ai_diff}
            )
            findings.extend(
                review_locally(
                    ai_context,
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
