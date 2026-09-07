from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import typer

from gitscribe.cli import app
from gitscribe.config_locator import find_config_path
from gitscribe.core.hooks import get_cached_validation, store_cached_validation
from gitscribe.validation.config import (
    load_validation_config,
)
from gitscribe.validation.hook import (
    install_pre_merge_hook,
    install_pre_push_hook,
    uninstall_pre_merge_hook,
    uninstall_pre_push_hook,
)
from gitscribe.validation.mode import VALID_MODES
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

# Import for its module-level `app.add_typer(sandbox_app, name="sandbox")`
# side effect. The installed console script is
# `gitscribe = gitscribe.validation.cli:app` (see pyproject.toml/setup.cfg
# entry_points) - this module is the actual entrypoint Python imports at
# startup, so anything that registers a subcommand onto the shared `app`
# but isn't imported from here (directly or transitively) never runs its
# registration code at all, regardless of how correct that module's own
# code is. `sandbox` previously wasn't reachable from any import path the
# real CLI actually takes, which is why `gitscribe sandbox` didn't exist
# despite validation/sandbox.py being correct in isolation.
from gitscribe.validation import sandbox as _sandbox  # noqa: F401


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


def _pre_merge_range() -> tuple[str, str] | None:
    """Mirrors `merge_check_cmd`'s base/head pair (core `gitscribe
    merge-check`) so the new validation gate reviews exactly the same
    change a merge would introduce. Returns None when no merge is in
    progress (git provides no stdin protocol for pre-merge-commit the way
    it does for pre-push, so this is a plain filesystem check, not review
    logic - the actual scanning/AI/policy work still happens downstream).
    """
    if not Path(".git/MERGE_HEAD").is_file():
        return None

    return ("HEAD", "MERGE_HEAD")


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
    paths: list[str] | None = None,
    force_mode: str | None = None,
    sandboxed: bool = False,
) -> ValidationResult:
    # Caching only applies to whole-change reviews (no --path/--mode
    # narrowing): a partial ad-hoc review isn't something a hook would
    # ever re-run identically, and caching it under the same key as a
    # full review of the same range would risk returning the wrong
    # scope's result. get_cached_validation/store_cached_validation are
    # themselves gated on a clean working tree (see core/hooks.py) since
    # the deterministic/AI passes read files off disk, not git blobs.
    cacheable = paths is None and force_mode is None

    if cacheable:
        cached = get_cached_validation(cfg, base, head, sandboxed, force_ai)
        if cached is not None:
            return ValidationResult.model_validate(cached)

    context = resolve_change(
        base=base,
        head=head,
        extra_ignore_patterns=cfg.get(
            "ignore_patterns",
            [],
        ),
        paths=paths,
    )

    result = validate_change(
        context,
        cfg,
        force_ai=force_ai,
        force_mode=force_mode,
        sandboxed=sandboxed,
    )

    # Only cache a genuinely completed scan. A result with `errors` (e.g.
    # "required scanner not found", an LLM timeout) reflects an
    # *environment* problem, not a property of this diff - it can resolve
    # on the very next invocation with nothing about the code changing.
    # Caching it would make a transient failure (like ruff not being
    # installed yet) stick around and get silently replayed as this
    # diff's permanent status even after the environment is fixed.
    if cacheable and not result.errors:
        store_cached_validation(cfg, base, head, result.model_dump(), sandboxed, force_ai)

    return result


def register_verify_command(
    command_app: typer.Typer,
    config_loader: Callable[
        ...,
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
        pre_merge: bool = typer.Option(
            False,
            "--pre-merge",
            help=(
                "Resolve the change from .git/MERGE_HEAD for the "
                "pre-merge-commit hook (base=HEAD, head=MERGE_HEAD). "
                "No-op (exit 0) if no merge is in progress."
            ),
        ),
        install_hook: bool = typer.Option(
            False,
            "--install-hook",
            help=(
                "Install or safely upgrade the GitScribe pre-push and "
                "pre-merge-commit hooks."
            ),
        ),
        uninstall_hook: bool = typer.Option(
            False,
            "--uninstall-hook",
            help=(
                "Remove only the GitScribe-authored lines from the "
                "pre-push and pre-merge-commit hooks, preserving any "
                "other content (e.g. the legacy risk-classifier calls "
                "installed by `gitscribe init`). Deletes a hook file "
                "entirely if nothing else was in it."
            ),
        ),
        force_agentic: bool = typer.Option(
            False,
            "--force-agentic",
            help=(
                "Force the sandboxed AI review pass to run even if "
                "validation.ai.enabled is false in config.yaml. This only "
                "overrides that enabled gate - it is never a dependency on "
                "the deterministic/static path's results, which always "
                "run independently regardless of this flag. Implies "
                "--sandboxed: gitscribe verify's AI reviewer only ever "
                "runs inside the local, loopback-only sandbox - there is "
                "no cloud/agentic mode here, unlike `gitscribe review "
                "--sandboxed`. For a permanent/CI setting instead of "
                "remembering this flag every time, set "
                "validation.ai.force: true in config.yaml."
            ),
        ),
        agentic: bool = typer.Option(
            False,
            "--agentic",
            hidden=True,
            help="Deprecated alias for --force-agentic.",
        ),
        path: list[str] = typer.Option(
            None,
            "--path",
            help=(
                "Restrict review to specific changed file(s) or glob(s) "
                "instead of the full diff. Repeatable. Errors if nothing "
                "in the diff matches."
            ),
        ),
        mode: str = typer.Option(
            None,
            "--mode",
            help=(
                "Override review mode for this run: static, agentic, or "
                "both. Typically paired with --path to review one file one "
                "way. Without --mode, per-file mode comes from "
                "validation.file_rules in config.yaml, defaulting to "
                "'both' (pre-existing behavior) where unspecified."
            ),
        ),
        sandboxed: bool = typer.Option(
            False,
            "--sandboxed",
            help=(
                "Required for the agentic pass to actually run. Routes it "
                "through the isolated, loopback-only local-model sandbox "
                "(same infra as `gitscribe review --sandboxed`). Any file "
                "resolved to agentic/both mode is skipped (and recorded as "
                "an error, failing the gate under fail_closed) if this "
                "isn't passed."
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
                push_message = install_pre_push_hook()
                merge_message = install_pre_merge_hook()
            except RuntimeError as exc:
                typer.echo(
                    str(exc),
                    err=True,
                )
                raise typer.Exit(
                    1
                ) from exc

            typer.echo(
                f"pre-push hook: {push_message}"
            )
            typer.echo(
                f"pre-merge-commit hook: {merge_message}"
            )
            return

        if uninstall_hook:
            push_message = uninstall_pre_push_hook()
            merge_message = uninstall_pre_merge_hook()
            typer.echo(f"pre-push hook: {push_message}")
            typer.echo(f"pre-merge-commit hook: {merge_message}")
            return

        if agentic and not force_agentic:
            typer.echo(
                "note: --agentic is deprecated, use --force-agentic instead "
                "(same behavior).",
                err=True,
            )
            force_agentic = True

        if mode is not None and mode not in VALID_MODES:
            typer.echo(
                f"--mode must be one of {VALID_MODES}, got {mode!r}",
                err=True,
            )
            raise typer.Exit(1)

        if pre_push and pre_merge:
            typer.echo(
                "--pre-push and --pre-merge are mutually exclusive.",
                err=True,
            )
            raise typer.Exit(1)

        if (pre_push or pre_merge) and (path or mode):
            typer.echo(
                "--path/--mode select files for a single ad-hoc review and "
                "aren't supported with --pre-push/--pre-merge (which "
                "review whatever ref range the hook provides). Use "
                "validation.file_rules in config.yaml to scope "
                "hook-triggered reviews instead.",
                err=True,
            )
            raise typer.Exit(1)

        if force_agentic and not sandboxed:
            # See --force-agentic's help text: for `verify` these two are
            # not independent knobs the way `gitscribe review`'s cloud vs.
            # local-sandboxed split is. Silently leaving sandboxed False
            # here just reproduces the "agentic requested but skipped as
            # an error under fail_closed" failure mode.
            sandboxed = True
            typer.echo(
                "note: --force-agentic implies --sandboxed for `gitscribe "
                "verify` (see --force-agentic --help)."
            )

        # Hook-triggered runs (--pre-push/--pre-merge) defer config loading
        # into the per-range loop below and resolve it from each range's
        # pre-change ref, not the working tree - see
        # validation.config._read_config_text's docstring for why: the
        # diff under review could itself have edited config.yaml (e.g. to
        # set validation.enabled: false, or redirect validation.ai's
        # base_url/api_key_env), and a hook must not trust that edit
        # before a human has reviewed and merged it. An ad hoc
        # `--base/--head` run stays on the working-tree config: a human
        # explicitly chose that range and can already see the file
        # themselves.
        hook_triggered = pre_push or pre_merge
        cfg: dict | None = None

        if not hook_triggered:
            try:
                cfg = loader(find_config_path())
            except Exception as exc:
                typer.echo(
                    f"validation configuration failed: {exc}",
                    err=True,
                )
                raise typer.Exit(1) from exc

            if not cfg.get("enabled", True):
                typer.echo(
                    "GitScribe validation is disabled by configuration."
                )
                return

        started = time.perf_counter()
        results: list[
            ValidationResult
        ] = []

        try:
            if pre_push:
                ranges = _pre_push_ranges()
            elif pre_merge:
                merge_range = _pre_merge_range()
                ranges = [merge_range] if merge_range else []
            else:
                ranges = [(base, head)]

            if not ranges:
                typer.echo(
                    "no merge in progress"
                    if pre_merge
                    else "no push updates require validation"
                )
                return

            for (
                range_base,
                range_head,
            ) in ranges:
                if hook_triggered:
                    try:
                        range_cfg = loader(find_config_path(), ref=range_base)
                    except Exception as exc:
                        typer.echo(
                            f"validation configuration failed: {exc}",
                            err=True,
                        )
                        raise typer.Exit(1) from exc

                    if not range_cfg.get("enabled", True):
                        typer.echo(
                            f"GitScribe validation is disabled by "
                            f"configuration at {range_base}."
                        )
                        continue
                else:
                    range_cfg = cfg

                results.append(
                    _run_one(
                        range_base,
                        range_head,
                        range_cfg,
                        force_ai=force_agentic,
                        paths=path or None,
                        force_mode=mode,
                        sandboxed=sandboxed,
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
