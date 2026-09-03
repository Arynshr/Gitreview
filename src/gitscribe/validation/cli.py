from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable

import typer

from gitscribe.cli import app
from gitscribe.validation.config import (
    load_validation_config,
)
from gitscribe.validation.hook import (
    install_pre_push_hook,
)
from gitscribe.validation.models import (
    AnalysisError,
    ValidationResult,
)
from gitscribe.validation.orchestrator import (
    validate_change,
)
from gitscribe.validation.policy import (
    sort_findings,
)
from gitscribe.validation.resolver import (
    resolve_change,
)


def _default_new_branch_base() -> str:
    result = subprocess.run(
        [
            "git",
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if (
        result.returncode == 0
        and result.stdout.strip()
    ):
        return result.stdout.strip().replace(
            "refs/remotes/",
            "",
        )

    return "origin/main"


def _pre_push_ranges() -> list[tuple[str, str]]:
    ranges: list[tuple[str, str]] = []

    for raw in sys.stdin:
        parts = raw.split()

        if len(parts) != 4:
            continue

        (
            _local_ref,
            local_sha,
            _remote_ref,
            remote_sha,
        ) = parts

        if local_sha == "0" * 40:
            continue

        if remote_sha == "0" * 40:
            base = _default_new_branch_base()
        else:
            base = remote_sha

        ranges.append(
            (
                base,
                local_sha,
            )
        )

    return ranges


def _report(
    result: ValidationResult,
    as_json: bool,
    debug: bool,
) -> None:
    if as_json:
        typer.echo(
            result.model_dump_json(
                indent=2
            )
        )
        return

    typer.echo(
        "\nGitScribe Verification\n"
    )

    typer.echo(
        f"Result: {result.status}"
    )

    typer.echo(
        f"Findings: {len(result.findings)}"
    )

    for finding in sort_findings(
        result.findings
    ):
        if (
            finding.file
            and finding.line
        ):
            location = (
                f"{finding.file}:"
                f"{finding.line}"
            )
        else:
            location = (
                finding.file
                or "(repository)"
            )

        source = "/".join(
            finding.sources
            or [finding.source]
        )

        typer.echo(
            f"\n[{finding.severity.upper()}] "
            f"{finding.category} "
            f"({source})"
        )

        typer.echo(location)
        typer.echo(finding.message)

        typer.echo(
            f"Remediation: "
            f"{finding.remediation}"
        )

    if result.errors:
        typer.echo(
            "\nAnalysis errors:"
        )

        for error in result.errors:
            typer.echo(
                f"  [{error.source}] "
                f"{error.message}"
            )

    if debug and result.timings:
        typer.echo("\nTimings:")

        for (
            name,
            seconds,
        ) in result.timings.items():
            typer.echo(
                f"  {name}: "
                f"{seconds:.3f}s"
            )


def _run_one(
    base: str,
    head: str,
    cfg: dict,
    force_ai: bool = False,
) -> ValidationResult:
    context = resolve_change(
        base=base,
        head=head,
        extra_ignore_patterns=cfg.get(
            "ignore_patterns",
            [],
        ),
    )

    return validate_change(
        context,
        cfg,
        force_ai=force_ai,
    )


def register_verify_command(
    command_app: typer.Typer,
    config_loader: Callable[
        [str],
        dict,
    ]
    | None = None,
) -> None:
    loader = (
        config_loader
        or load_validation_config
    )

    @command_app.command(
        name="verify"
    )
    def verify(
        base: str = typer.Option(
            "origin/main",
            "--base",
        ),
        head: str = typer.Option(
            "HEAD",
            "--head",
        ),
        pre_push: bool = typer.Option(
            False,
            "--pre-push",
            help=(
                "Read Git pre-push "
                "ref updates from stdin."
            ),
        ),
        install_hook: bool = typer.Option(
            False,
            "--install-hook",
            help=(
                "Install or safely upgrade "
                "the GitScribe pre-push hook."
            ),
        ),
        agentic: bool = typer.Option(
            False,
            "--agentic",
            help=(
                "Force the local AI review pass "
                "to run even if validation.ai.enabled "
                "is false in config.yaml."
            ),
        ),
        as_json: bool = typer.Option(
            False,
            "--json",
        ),
        debug: bool = typer.Option(
            False,
            "--debug",
        ),
    ) -> None:
        if install_hook:
            try:
                message = (
                    install_pre_push_hook()
                )
            except RuntimeError as exc:
                typer.echo(
                    str(exc),
                    err=True,
                )
                raise typer.Exit(
                    1
                ) from exc

            typer.echo(
                f"pre-push hook: {message}"
            )
            return

        try:
            cfg = loader(
                "config.yaml"
            )
        except Exception as exc:
            typer.echo(
                "validation configuration "
                f"failed: {exc}",
                err=True,
            )
            raise typer.Exit(
                1
            ) from exc

        if not cfg.get(
            "enabled",
            True,
        ):
            typer.echo(
                "GitScribe validation is "
                "disabled by configuration."
            )
            return

        started = time.perf_counter()
        results: list[
            ValidationResult
        ] = []

        try:
            ranges = (
                _pre_push_ranges()
                if pre_push
                else [(base, head)]
            )

            if not ranges:
                typer.echo(
                    "no push updates "
                    "require validation"
                )
                return

            for (
                range_base,
                range_head,
            ) in ranges:
                results.append(
                    _run_one(
                        range_base,
                        range_head,
                        cfg,
                        force_ai=agentic,
                    )
                )

        except Exception as exc:
            result = ValidationResult(
                status="FAIL",
                errors=[
                    AnalysisError(
                        source="change-resolver",
                        message=str(exc),
                    )
                ],
                timings={
                    "total_s": (
                        time.perf_counter()
                        - started
                    )
                },
            )

            _report(
                result,
                as_json,
                debug,
            )

            raise typer.Exit(
                1
            ) from exc

        merged_findings = []
        errors = []
        timings = {
            "total_s": (
                time.perf_counter()
                - started
            )
        }

        for result in results:
            merged_findings.extend(
                result.findings
            )

            errors.extend(
                result.errors
            )

            for (
                key,
                value,
            ) in result.timings.items():
                timings[key] = (
                    timings.get(
                        key,
                        0.0,
                    )
                    + value
                )

        final = ValidationResult(
            status=(
                "FAIL"
                if any(
                    result.status
                    == "FAIL"
                    for result in results
                )
                else "PASS"
            ),
            findings=merged_findings,
            errors=errors,
            timings=timings,
        )

        _report(
            final,
            as_json,
            debug,
        )

        if final.status != "PASS":
            raise typer.Exit(1)


register_verify_command(app)


if __name__ == "__main__":
    app()
