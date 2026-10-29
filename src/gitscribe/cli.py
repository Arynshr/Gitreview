import json
import os
import shutil
import stat
import subprocess
import sys
from enum import StrEnum
from pathlib import Path

import click
import typer
import yaml
from dotenv import find_dotenv, load_dotenv
from pydantic import ValidationError

from gitscribe import console, ui
from gitscribe.config_locator import find_config_path
from gitscribe.core import commit_message as cm, hooks as hook_utils, memory
from gitscribe.core.analysis import linter as linter_mod
from gitscribe.core.analysis.diff_symbols import split_diff_by_file
from gitscribe.core.analysis.rag import (
    answer_query,
    retrieve,
    run_batched_agentic_review,
    write_agentic_findings_by_file,
)
from gitscribe.core.config_schema import GitScribeConfig
from gitscribe.core.diff_parser import (
    GitCommandError,
    _run_git,
    diff_parser_node,
    get_commit_messages,
    get_raw_diff,
)
from gitscribe.core.graph import build_graph
from gitscribe.core.indexer import index_store
from gitscribe.core.llm_client import MissingAPIKeyError
from gitscribe.core.progress import phase
from gitscribe.core.merge_preview import WorktreeError, run_merge_preview
from gitscribe.core.state import GitScribeState
from gitscribe.core.summarizer import summarizer_node

load_dotenv(find_dotenv(usecwd=True))

app = typer.Typer(
    help="GitScribe: A stateful AI-powered Git and code intelligence system",
    no_args_is_help=True,
    # default Typer tracebacks print local variables - build_chat_model's
    # `api_key` would be printed in clear text on any crash
    pretty_exceptions_show_locals=False,
)


def _version_callback(value: bool) -> None:
    if value:
        from importlib import metadata

        try:
            typer.echo(f"gitscribe {metadata.version('gitscribe')}")
        except metadata.PackageNotFoundError:
            typer.echo("gitscribe (development install)")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit"),
) -> None:
    pass

ENV_PATH = Path(".env")
repo_hooks_dir = Path(".git") / "hooks"


class Style(StrEnum):
    default = "default"
    concise = "concise"
    detailed = "detailed"


def load_config(path: str | None = None) -> dict:
    """Load and validate config.yaml, returning a plain dict.

    `path` defaults to `config_locator.find_config_path()` — resolved fresh
    on every call (not baked into the signature) so it always reflects the
    current working directory, not whatever directory the process started
    in. Fails fast with a clear message on bad config. Returns a plain dict
    (already the result of `GitScribeConfig.as_dict()`) — callers should
    use the return value as-is, not call `.as_dict()` on it again.
    """
    path = path or find_config_path()

    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        console.error(f"config file not found: {path}")
        raise typer.Exit(1) from e

    try:
        validated = GitScribeConfig(**raw)
    except ValidationError as e:
        console.error(f"invalid config.yaml:\n{e}")
        raise typer.Exit(1) from e

    return validated.as_dict()


def current_branch() -> str:
    """Reuses diff_parser's git-command wrapper instead of a second
    unguarded subprocess.run(check=True) - outside a git repo (or with git
    missing) this now raises the same GitCommandError callers already
    catch, instead of an uncaught CalledProcessError traceback."""
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()


def is_gh_authenticated() -> bool:
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return result.returncode == 0


def default_branch() -> str | None:
    """Best-effort repo default branch (e.g. 'main'). Returns None if it
    can't be determined (no origin remote, detached HEAD info, etc.) —
    callers treat that as "unknown", not an error.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip().rsplit("/", 1)[-1]


def has_upstream_tracking_branch() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def existing_pr_url(branch: str) -> str | None:
    result = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return prs[0]["url"] if prs else None


def require_api_key() -> None:
    """Fail fast with a clear message before invoking the graph, rather than
    letting a missing key surface as an opaque provider auth error mid-run."""
    if not os.environ.get("API_KEY"):
        console.error("API_KEY not set. Run `gitscribe init` or `export API_KEY=<your-key>`.")
        raise typer.Exit(1)

@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Replace existing hooks (the old one is kept as <name>.bak)"),
):
    """Install the pre-push, pre-merge-commit, post-merge, and commit-msg git hooks into .git/hooks/."""
    if not repo_hooks_dir.exists():
        console.error(".git/hooks not found - run this from a git repo root")
        raise typer.Exit(1)

    # Each hook is installed independently: an existing "pre-push" (e.g.
    # from a prior `gitscribe verify --install-hook` run) must not prevent
    # "pre-merge-commit"/"post-merge"/"commit-msg" from being installed too.
    # Previously a single pre-existing hook aborted the whole command,
    # silently skipping every other hook.
    hook_names = ["pre-push", "pre-merge-commit", "post-merge", "commit-msg"]
    installed = 0
    for hook_name in hook_names:
        dest = repo_hooks_dir / hook_name  # git requires the extensionless name here
        if dest.exists() and not force:
            console.warn(f"{dest} already exists - not overwriting. Re-run with --force to replace it.")
            continue
        src = Path(__file__).parent / "hooks" / f"{hook_name}.sh"
        if not src.exists():
            console.error(f"hook source missing: {src}")
            raise typer.Exit(1)
        if dest.exists() and dest.read_text() != src.read_text():
            shutil.copy2(dest, dest.with_name(f"{hook_name}.bak"))
        dest.write_text(src.read_text())
        if os.name == "posix":
            dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
        installed += 1

    if installed:
        console.success(f"installed {installed} git hook(s) into {repo_hooks_dir}")
    else:
        console.info("all git hooks already present - nothing to install")
    _ensure_merge_preview_alias()
    _ensure_api_key()


def _ensure_merge_preview_alias() -> None:
    """Registers `git merge-preview <branch>` as a shortcut for
    `gitscribe merge-preview <branch>`, auto-configured here so users get
    it for free from `gitscribe init` rather than a separate manual step.
    Merge-preview isn't a real git hook (git has no hook that fires on a
    conflict — it skips pre-merge-commit entirely when the merge stops
    short on conflicts), so a git alias is the closest "native-feeling"
    integration point available.
    """
    existing = subprocess.run(
        ["git", "config", "--get", "alias.merge-preview"], capture_output=True, text=True
    )
    if existing.returncode == 0 and existing.stdout.strip() and "gitscribe merge-preview" not in existing.stdout:
        console.warn(
            f"git alias 'merge-preview' already set to '{existing.stdout.strip()}' - leaving it alone"
        )
        return

    result = subprocess.run(
        ["git", "config", "alias.merge-preview", "!gitscribe merge-preview"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.warn(f"could not configure `git merge-preview` alias: {result.stderr.strip()}")
        return
    console.success("configured `git merge-preview <branch>` as a shortcut for `gitscribe merge-preview <branch>`")


def _ensure_api_key() -> None:
    """Write API_KEY to .env if not already available"""
    load_dotenv(ENV_PATH)
    if os.environ.get("API_KEY"):
        console.info(f"{console.PREFIX} API_KEY already set - leaving .env untouched")
        return

    try:
        api_key = typer.prompt("Enter your LLM API key (provider is set in config.yaml)", hide_input=True)
    except (typer.Abort, click.exceptions.Abort):
        # Non-interactive context (CI, script, piped stdin) - the hook is
        # already installed at this point, so don't fail the whole command
        # over an optional convenience step.
        console.warn(
            "no interactive terminal - skipping API key setup. "
            "Set it later with `export API_KEY=<your-key>` or re-run `gitscribe init`."
        )
        return
    if not api_key.strip():
        console.warn("no key entered - skipping .env write")
        return

    existing = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    lines = [ln for ln in existing.splitlines() if ln and not ln.startswith("API_KEY=")]
    lines.append(f"API_KEY={api_key}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    if os.name == "posix":
        ENV_PATH.chmod(0o600)  # secret: owner-only
    console.success(f"wrote API_KEY to {ENV_PATH.resolve()}")
    # exit code 1 = inside a repo and NOT ignored (128 = not a repo: nothing to warn about)
    if subprocess.run(["git", "check-ignore", "-q", str(ENV_PATH)], capture_output=True).returncode == 1:
        console.warn(f"{ENV_PATH} is not in .gitignore - add it so the API key is never committed")


_PATH_OPT = typer.Option(None, "--path", "-p", help="Limit to these files/folders (repeatable)")
_STREAM_OPT = typer.Option(
    "auto", "--stream",
    help="Progress: auto (live on a TTY), live, json (NDJSON events + final result), off",
)


def _existing_paths(paths: list[str] | None) -> list[str] | None:
    """Fail early on a typo'd --path instead of silently doing nothing."""
    if not paths:
        return None
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        console.error(f"path not found: {', '.join(missing)}")
        raise typer.Exit(2)
    return list(paths)


def _scope_label(paths: list[str] | None) -> str:
    return " · ".join(paths) if paths else ""


def _invoke_generation(config: dict, branch: str, style: str, paths: list[str] | None, stream: str, title: str):
    """Run the generation graph with phase events. With `paths`, the PR diff
    is limited to those paths up front, so summary, risk, retrieval and
    validation only ever see the selected files."""
    raw_diff = ""
    if paths:
        raw_diff = get_raw_diff(paths=paths)
        if not raw_diff.strip():
            raise GitCommandError(f"no changes under {', '.join(paths)} relative to the base branch")
    subtitle = " · ".join(filter(None, [branch, _scope_label(paths)]))
    with ui.open_run(title, stream, subtitle=subtitle) as run:
        graph = build_graph(config, emitter=run.emitter)
        result = graph.invoke(GitScribeState(
            branch_name=branch, style=style, attempt_count=0, fallback_used=False,
            status="pending", raw_diff=raw_diff,
        ))
    run.result(
        status=result.get("status"), title=result.get("pr_title"), body=result.get("pr_body"),
        validation=result.get("validation_status"), failure=result.get("failure_type"),
    )
    return result


def _show_validation(result: dict) -> None:
    """Surface what validation found when it failed or errored - the graph
    only carries a count/summary, so without this the user can't tell what
    to fix."""
    if result.get("validation_status") not in ("FAIL", "ERROR"):
        return
    console.warn(f"validation {result['validation_status']}: {result.get('validation_summary', '')}")
    colors = {"critical": "error", "high": "error", "error": "error", "medium": "warning"}
    for severity, text in result.get("validation_findings", []):
        console.line(f"  [{severity.upper()}] {text}", colors.get(severity, "info"))
    shown = len(result.get("validation_findings", []))
    total = result.get("validation_findings_count", 0)
    if total > shown:
        console.info(f"  ... {total - shown} more")
    console.info("  full report: gitscribe verify    (skip for one run: --no-validate)")


def _apply_validate_flag(config: dict, validate: bool | None) -> dict:
    """Per-run override of the validation stage inside generation.
    None = follow config.yaml; False = skip validation entirely (no scanners
    run, generation is never gated); True = run it even if config disables it.
    """
    if validate is not None:
        config.setdefault("validation", {})["enabled"] = validate
    return config


@app.command()
def generate(
    dry_run: bool = typer.Option(False, "--dry-run", help="Skip saving to memory; still calls the LLM"),
    style: Style = typer.Option(Style.default, "--style", help="Style preset for the generated description"),
    validate: bool | None = typer.Option(
        None, "--validate/--no-validate",
        help="Run or skip the validation stage for this run (default: follow config.yaml)",
    ),
    path: list[str] | None = _PATH_OPT,
    stream: str = _STREAM_OPT,
):
    """Generate a PR description for the current branch's diff (or only the --path files)."""
    require_api_key()
    config = _apply_validate_flag(load_config(), validate)
    paths = _existing_paths(path)

    try:
        result = _invoke_generation(config, current_branch(), style.value, paths, stream, "generate")
    except GitCommandError as e:
        console.error(str(e))
        raise typer.Exit(1) from e

    console.info(f"\nTitle: {result.get('pr_title')}\n")
    console.info(result.get("pr_body", "(no body generated)"))
    _show_validation(result)

    status = result.get("status")

    if status == "skipped":
        console.warn("skipped: diff below trivial threshold, used template fallback")
        return

    if status == "failed":
        # generator_node/failure_router set these before template_fallback
        # took over; surface them instead of silently reporting success on
        # a generic template - this used to be indistinguishable from a
        # real successful generation.
        reason = result.get("failure_type", "unknown")
        last_error = (result.get("last_error") or "")[:200]
        console.error(
            f"LLM generation failed after {result.get('attempt_count', '?')} attempt(s) "
            f"({reason}: {last_error}) - used a generic template instead."
        )
        if dry_run:
            console.info("\n[dry-run: not saved to memory]")
            return
        memory.save_pr(result["branch_name"], result["pr_title"], result["pr_body"])
        console.warn("saved fallback template to memory (not a real generated description)")
        return

    if dry_run:
        console.info("\n[dry-run: not saved to memory]")
        return

    if status == "success":
        memory.save_pr(result["branch_name"], result["pr_title"], result["pr_body"])
        console.success("saved to memory")


@app.command(name="create-pr")
def create_pr(
    style: Style = typer.Option(Style.default, "--style"),
    push: bool = typer.Option(True, "--push/--no-push", help="Push the branch to origin first if it has no upstream tracking branch"),
    validate: bool | None = typer.Option(
        None, "--validate/--no-validate",
        help="Run or skip the validation stage for this run (default: follow config.yaml)",
    ),
    path: list[str] | None = _PATH_OPT,
    stream: str = _STREAM_OPT,
):
    """Generate a PR description and open the PR via `gh`.

    Handles the common `gh pr create` failure causes proactively instead
    of just reporting them after the fact:
      - branch not pushed to remote -> pushes it (unless --no-push)
      - PR already exists for this branch -> prints its URL and exits, no error
      - on the repo's default branch -> caught early with a clear message
    """
    if not shutil.which("gh"):
        console.error("`gh` CLI not found. Install it: https://cli.github.com")
        raise typer.Exit(1)
    if not is_gh_authenticated():
        console.error("`gh` is not authenticated. Run `gh auth login` first.")
        raise typer.Exit(1)

    try:
        branch = current_branch()
    except GitCommandError as e:
        console.error(str(e))
        raise typer.Exit(1) from e
    base = default_branch()
    if base and branch == base:
        console.error(f"you're on '{branch}', the repo's default branch — checkout a feature branch first.")
        raise typer.Exit(1)

    existing_url = existing_pr_url(branch)
    if existing_url:
        console.info(f"{console.PREFIX} a PR already exists for '{branch}': {existing_url}")
        return

    if not has_upstream_tracking_branch():
        if not push:
            console.error(
                f"'{branch}' has no upstream on origin. "
                f"Push it first (`git push -u origin {branch}`) or re-run without --no-push.",
            )
            raise typer.Exit(1)
        console.info(f"{console.PREFIX} '{branch}' has no upstream — pushing to origin...")
        try:
            # --no-verify: this push is create-pr's own internal step (to
            # satisfy `gh pr create`'s remote-branch requirement), not a
            # user-initiated push. Without --no-verify this re-fires the
            # installed pre-push hook (gitscribe pre-push), which -- on a
            # branch with no upstream -- itself calls `gitscribe create-pr`
            # again, which pushes again, re-firing the hook again: an
            # unbounded recursive loop on the very first push of a new
            # branch. The risk check has already run once for this diff;
            # re-running it on GitScribe's own plumbing push serves no
            # purpose and is the actual cause of the loop.
            subprocess.run(["git", "push", "-u", "origin", branch, "--no-verify"], check=True)
        except subprocess.CalledProcessError as e:
            console.error("`git push` failed — see output above.")
            raise typer.Exit(e.returncode or 1) from e

    require_api_key()
    config = _apply_validate_flag(load_config(), validate)
    paths = _existing_paths(path)
    try:
        result = _invoke_generation(config, branch, style.value, paths, stream, "create-pr")
    except GitCommandError as e:
        console.error(str(e))
        raise typer.Exit(1) from e

    _show_validation(result)
    status = result.get("status")
    if status == "failed":
        reason = result.get("failure_type", "unknown")
        last_error = (result.get("last_error") or "")[:200]
        console.error(
            f"LLM generation failed after {result.get('attempt_count', '?')} attempt(s) "
            f"({reason}: {last_error}) - not opening a PR with a generic template. "
            "Check API_KEY / model config / network and retry."
        )
        raise typer.Exit(1)
    if status == "skipped":
        console.warn("diff below trivial threshold - opening PR with a generic template description")

    try:
        subprocess.run([
            "gh", "pr", "create",
            "--title", result["pr_title"],
            "--body", result["pr_body"],
        ], check=True)
    except subprocess.CalledProcessError as e:
        console.error(
            "`gh pr create` failed (see gh's output above for the reason). "
            "PR description was generated but not opened."
        )
        raise typer.Exit(e.returncode or 1) from e

    memory.save_pr(result["branch_name"], result["pr_title"], result["pr_body"])


@app.command()
def index(
    repo_root: str = typer.Option(".", help="Repo root to index"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output"),
    force: bool = typer.Option(False, "--force", help="Bypass hash check, full rebuild"),
    path: list[str] | None = _PATH_OPT,
    stream: str = _STREAM_OPT,
):
    """Build/refresh the code graph + embeddings. Incremental by default
    (Merkle hash skip/reuse) — pass --force for a full rebuild. With --path,
    only those files/folders are (re)indexed and the rest of the index is
    left untouched.
    """
    config = load_config()  # already a dict — see load_config() docstring
    paths = _existing_paths(path)
    with ui.open_run("index", stream, machine=json_output, subtitle=_scope_label(paths)) as run:
        result = index_store.reindex(repo_root, config, force=force, paths=paths, emitter=run.emitter)

    conn = index_store._get_connection()
    symbol_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    resolved_count = conn.execute("SELECT COUNT(*) FROM edges WHERE resolved = 1").fetchone()[0]
    conn.close()

    stats = {
        "symbols": symbol_count,
        "edges": edge_count,
        "resolved_edges": resolved_count,
        "files_changed": result.files_changed,
        "files_skipped": result.files_skipped,
        "skipped_entirely": result.skipped_entirely,
    }

    run.result(**stats)
    if json_output:
        typer.echo(json.dumps(stats, indent=2))
    elif result.skipped_entirely:
        console.info("index unchanged since last run, nothing to do")
    else:
        console.success(
            f"indexed {symbol_count} symbols, {edge_count} edges ({resolved_count} resolved) "
            f"— {result.files_changed} file(s) reprocessed, {result.files_skipped} skipped"
        )


@app.command(name="review")
def review_cmd(
    lint_only: bool = typer.Option(False, "--lint-only", help="Static pass only, no LLM cost"),
    as_json: bool = typer.Option(False, "--json", help="Scriptable output"),
    base: str = typer.Option("origin/main", help="Diff base for the agentic pass"),
    head: str = typer.Option("HEAD", help="Diff head for the agentic pass"),
    sandboxed: bool = typer.Option(
        False,
        "--sandboxed",
        help="Route the agentic pass through the local llama.cpp server "
        "(gitscribe.validation's config) instead of the cloud BYOK provider.",
    ),
):
    """Run lint + agentic review, write review_findings, print summary."""
    config = load_config()
    lint_count = 0
    if config.get("review", {}).get("lint", {}).get("enabled", True):
        try:
            findings = linter_mod.run_ruff(".")
            lint_count = linter_mod.write_lint_findings(findings)
        except (linter_mod.RuffNotFoundError, linter_mod.RuffError) as e:
            console.warn(f"lint pass skipped: {e}")
    agentic_count = 0
    if not lint_only and config.get("review", {}).get("agentic", {}).get("enabled", True):
        try:
            raw_diff = get_raw_diff(base=base, head=head)
        except GitCommandError as e:
            console.warn(f"skipping agentic review, couldn't get diff: {e}")
            raw_diff = ""
        if raw_diff.strip():
            # Gate + batch: files with no error-severity lint findings and
            # low blast radius are skipped entirely (lint's bandit-style
            # rules already caught anything dangerous on the current
            # tree); everything else is packed into as few LLM calls as
            # fit the token budget instead of one call per file.
            per_file_diffs = split_diff_by_file(raw_diff)
            try:
                results, anchors, reviewed_files = run_batched_agentic_review(
                    per_file_diffs, config, sandboxed=sandboxed
                )
                agentic_count = write_agentic_findings_by_file(results, anchors)
                skipped = len(per_file_diffs) - len(reviewed_files)
                console.info(
                    f"agentic review: {len(reviewed_files)} file(s) sent to "
                    f"{'the local model' if sandboxed else 'the LLM'}, "
                    f"{skipped} skipped by gate, {agentic_count} finding(s)"
                )
            except Exception as e:
                console.warn(f"agentic review failed: {e}")
        else:
            console.info("empty diff, skipping agentic review (index staleness is a separate condition)")
    if as_json:
        typer.echo(json.dumps({"lint_findings": lint_count, "agentic_findings": agentic_count}))
    else:
        console.success(f"review: {lint_count} lint finding(s), {agentic_count} agentic finding(s)")
        if lint_count:
            console.info("  details: gitscribe lint    (auto-repair: gitscribe lint --fix --format)")

@app.command()
def query(
    text: str = typer.Argument(..., help="Natural language query"),
    top_k: int = typer.Option(5, help="Number of matches"),
    json_output: bool = typer.Option(False, "--json", help="Raw retrieval context as JSON, no LLM call"),
    raw: bool = typer.Option(False, "--raw", help="Print raw retrieved context only, skip LLM synthesis"),
    stream: str = _STREAM_OPT,
):
    """Ask a question about the indexed codebase.

    By default this synthesizes an answer via the configured LLM, grounded
    in retrieved symbols (cited as name/file:line). Use --raw for the
    context-block output only, or --json for structured retrieval data
    with no LLM call (and no cost).
    """
    config = load_config()
    try:
        with ui.open_run("query", stream, machine=json_output or raw, subtitle=text[:60], to_stderr=True) as run:
            with phase(run.emitter, "retrieve") as info:
                context = retrieve(text, config, top_k=top_k)
                info["symbols"] = len(context.snippets)
    except Exception as e:
        console.error(f"retrieval failed: {e}")
        raise typer.Exit(code=1) from e

    if not context.snippets:
        console.warn(
            "no relevant symbols found for that query. Run `gitscribe index` first "
            "if you haven't, or try rephrasing."
        )
        raise typer.Exit(code=1)

    if json_output:
        typer.echo(context.model_dump_json(indent=2))
        return

    if raw:
        typer.echo(context.as_prompt_block())
        return

    require_api_key()
    try:
        with ui.open_run("query", stream, subtitle="answer", to_stderr=True) as run:
            with phase(run.emitter, "answer"):
                answer = answer_query(text, context, config)
        typer.echo(answer)
        typer.echo("\n--- context used ---")
        typer.echo(context.as_prompt_block())
    except MissingAPIKeyError as e:
        console.error(str(e))
        raise typer.Exit(code=1) from e
    except Exception as e:
        console.warn(f"answer synthesis failed ({e}); showing raw context instead:")
        typer.echo(context.as_prompt_block())


def _print_lint(findings, paths_label: str) -> None:
    if not findings:
        console.success("no lint findings")
        return
    by_file = linter_mod.findings_by_file(findings)
    for file, items in by_file.items():
        console.line(f"\n{file}", severity="info")
        for f in sorted(items, key=lambda x: (x.lineno, x.col)):
            mark = "  [fixable]" if f.fixable else ""
            console.line(
                f"  {f.lineno}:{f.col:<4} {f.severity:7} {f.code:7} {f.message}{mark}", severity=f.severity
            )
    errors = sum(1 for f in findings if f.severity == "error")
    fixable = sum(1 for f in findings if f.fixable)
    console.info(
        f"\n{len(findings)} finding(s): {errors} error(s), {len(findings) - errors} warning(s); "
        f"severity score {linter_mod.severity_score(findings):.2f}"
    )
    if fixable:
        console.info(f"{fixable} auto-fixable -> gitscribe lint --fix{paths_label}   (preview first: --diff)")


@app.command()
def lint(
    repo_root: str = typer.Option(".", help="Repo root to lint"),
    path: list[str] | None = _PATH_OPT,
    json_output: bool = typer.Option(False, "--json"),
    fails_on_error: bool = typer.Option(True, help="Exit 1 if any error-severity finding exists"),
    fix: bool = typer.Option(False, "--fix", help="Apply ruff's safe auto-fixes, then re-lint"),
    unsafe_fixes: bool = typer.Option(False, "--unsafe-fixes", help="With --fix: also apply fixes ruff marks unsafe"),
    format_code: bool = typer.Option(False, "--format", help="Also run `ruff format`"),
    diff: bool = typer.Option(False, "--diff", help="Preview what --fix/--format would change; modify nothing"),
):
    """List ruff findings (file, line, rule, message); `--fix` / `--format` repair them."""
    paths = _existing_paths(path)
    label = f" --path {' --path '.join(paths)}" if paths else ""
    try:
        if diff:
            out = linter_mod.preview_fixes(repo_root, paths, unsafe_fixes)
            if format_code:
                out += linter_mod.format_code(repo_root, paths, diff=True)
            console.info(out.strip() or "nothing to change")
            return

        before = linter_mod.run_ruff(repo_root, paths)
        if fix or format_code:
            if fix:
                linter_mod.apply_fixes(repo_root, paths, unsafe_fixes)
            if format_code:
                linter_mod.format_code(repo_root, paths)
        findings = linter_mod.run_ruff(repo_root, paths) if (fix or format_code) else before
    except linter_mod.RuffNotFoundError as e:
        console.error(str(e))
        raise typer.Exit(1) from e
    except linter_mod.RuffError as e:
        console.error(f"ruff failed: {e}")
        raise typer.Exit(2) from e

    if json_output:
        typer.echo(json.dumps([f.model_dump() for f in findings], indent=2))
    else:
        if fix or format_code:
            console.success(f"fixed {max(len(before) - len(findings), 0)} of {len(before)} finding(s); {len(findings)} remain")
        _print_lint(findings, label)

    if fails_on_error and any(f.severity == "error" for f in findings):
        raise typer.Exit(code=1)


def _find_symbol(conn, symbol: str) -> tuple[int, str]:
    """Resolves a user-typed name to a single (symbol_id, matched_name).
    Exact match wins if unambiguous; otherwise falls back to a substring
    search. Exits via typer.Exit on no-match or ambiguous-match.
    """
    exact = conn.execute("SELECT id, name, file FROM symbols WHERE name = ?", (symbol,)).fetchall()
    if len(exact) == 1:
        return exact[0]["id"], exact[0]["name"]
    if len(exact) > 1:
        console.error(f"ambiguous symbol '{symbol}' — found in multiple files:")
        for r in exact:
            console.line(f"  {r['file']}")
        console.info("Re-run with a more specific name; disambiguation by file not yet supported.")
        raise typer.Exit(code=1)

    fuzzy = conn.execute(
        "SELECT id, name, file FROM symbols WHERE name LIKE ? ORDER BY LENGTH(name) ASC LIMIT 10",
        (f"%{symbol}%",),
    ).fetchall()
    if not fuzzy:
        console.error(f"no symbol matching '{symbol}' found. Run `gitscribe index` first?")
        raise typer.Exit(code=1)
    if len(fuzzy) > 1:
        console.warn(f"no exact match for '{symbol}'. Closest matches:")
        for r in fuzzy:
            console.line(f"  {r['name']}  ({r['file']})")
        console.info("Re-run with one of the names above.")
        raise typer.Exit(code=1)

    console.warn(f"no exact match for '{symbol}' — using closest match '{fuzzy[0]['name']}'")
    return fuzzy[0]["id"], fuzzy[0]["name"]


@app.command()
def graph(
    symbol: str = typer.Argument(..., help="Symbol name to inspect (exact or partial)"),
    depth: int = typer.Option(2, help="Traversal depth"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Blast radius / dependency view for a named symbol."""
    conn = index_store._get_connection()
    try:
        symbol_id, matched_name = _find_symbol(conn, symbol)
    finally:
        conn.close()
    results = index_store.blast_radius(symbol_id, max_depth=depth)

    if json_output:
        typer.echo(json.dumps([r.model_dump() for r in results], indent=2))
        return

    console.info(f"Blast radius for '{matched_name}' (depth {depth}):")
    for direction, heading in (("caller", "Callers (who depends on it)"), ("callee", "Callees (what it depends on)")):
        group = [r for r in results if r.direction == direction]
        console.info(f"\n{heading}:")
        if not group:
            console.info("  (none)")
            continue
        for r in group:
            indent = "  " * r.depth
            console.info(f"{indent}└─ {r.name}  ({r.file}:{r.lineno})  [depth {r.depth}]")

def _prepare_message(paths, include_untracked, message, stream, title, to_stderr=False):
    """Shared by `commit-message` and `commit`: collect -> (generate).
    Returns (changes, message, run)."""
    config = load_config()
    if message is None:
        require_api_key()
    subtitle = _scope_label(paths) or "staged changes"
    try:
        with ui.open_run(title, stream, subtitle=subtitle, to_stderr=to_stderr) as run:
            with phase(run.emitter, "collect") as info:
                changes = cm.collect_changes(paths, include_untracked)
                info["files"] = len(changes.files) + len(changes.untracked)
            if changes.empty:
                where = f"under {', '.join(paths)}" if paths else "staged (use --path to pick files)"
                raise cm.CommitMessageError(f"no changes {where}")
            if message is None:
                message = cm.generate_message(changes, config, run.emitter)
    except (GitCommandError, cm.CommitMessageError) as e:
        console.error(str(e))
        raise typer.Exit(1) from e
    return changes, message, run


@app.command(name="commit-message")
def commit_message_cmd(
    path: list[str] | None = _PATH_OPT,
    include_untracked: bool = typer.Option(False, "--include-untracked", "-u", help="Also describe new (untracked) files under --path"),
    stream: str = _STREAM_OPT,
):
    """Write a Conventional Commit message for staged changes (or --path). Prints only the message
    on stdout, so it composes: git commit -m "$(gitscribe commit-message)"."""
    _, message, run = _prepare_message(_existing_paths(path), include_untracked, None, stream, "commit-message", to_stderr=True)
    problem = cm.validate_message(message)
    if problem:
        console.warn(problem)
    run.result(message=message, valid=problem is None)
    if not run.json_stream:
        typer.echo(message)


@app.command()
def commit(
    path: list[str] | None = _PATH_OPT,
    message: str | None = typer.Option(None, "--message", "-m", help="Use this message; no LLM call (manual mode)"),
    include_untracked: bool = typer.Option(False, "--include-untracked", "-u", help="Also add new (untracked) files under --path"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Commit without the confirmation prompt"),
    edit: bool = typer.Option(False, "--edit", "-e", help="Open the message in $EDITOR before committing"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the message and what would be committed; commit nothing"),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip git hooks (including the commit-msg check)"),
    stream: str = _STREAM_OPT,
):
    """Commit staged changes, or only the modified files under --path, with a generated or manual message.

    Only the selected change set is committed; other staged work is left alone.
    """
    paths = _existing_paths(path) if path else None
    changes, message, run = _prepare_message(paths, include_untracked, message, stream, "commit")

    interactive = not yes and not dry_run
    if interactive and (run.json_stream or not sys.stdin.isatty()):
        console.error("non-interactive session: pass --yes to commit (or --dry-run to preview).")
        raise typer.Exit(1)

    while True:
        if edit:
            edited = typer.edit(message)
            message, edit = (edited.strip() if edited else message), False
        problem = cm.validate_message(message)
        console.info(f"\n{message}\n")
        console.info("  " + ", ".join([*changes.files, *changes.untracked][:8]) + (" …" if len(changes.files) + len(changes.untracked) > 8 else ""))
        if problem:
            console.warn(problem)
        if dry_run:
            return
        if not interactive:
            if problem:
                console.error("refusing to commit an invalid message non-interactively; fix it with -m or --edit.")
                raise typer.Exit(1)
            break
        choice = typer.prompt("commit? [y]es / [e]dit / [n]o", default="y").strip().lower()[:1]
        if choice == "n":
            console.warn("aborted, nothing committed")
            raise typer.Exit(1)
        if choice == "e":
            edit = True
            continue
        if problem:
            console.warn("fix the message first (press e to edit)")
            continue
        break

    try:
        sha = cm.stage_and_commit(message, paths, include_untracked, no_verify)
    except GitCommandError as e:
        console.error(str(e))
        raise typer.Exit(1) from e
    run.result(message=message, commit=sha)
    console.success(f"committed {sha}: {message.splitlines()[0]}")


@app.command(name="pre-push")
def pre_push_cmd():
    cfg = load_config()
    hard_block = cfg.get("hooks", {}).get("pre_push", {}).get("block_on_risk", False)
    try:
        result = hook_utils.get_cached_risk(cfg, base="origin/main", head="HEAD")
    except Exception as e:  # noqa: BLE001 - a hook must never block git because of its own failure
        console.warn(f"pre-push risk check skipped ({e})")
        result = None
    threshold = cfg["risk_classifier"]["trivial_threshold"] * 3

    if result and result["risk_score"] >= threshold:
        console.warn(f"high-risk diff ({result['risk_score']:.2f}): {result['risk_reasoning']}")
        if hard_block:
            console.error("blocked — override with: git push --no-verify")
            raise typer.Exit(code=1)

    no_upstream = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "@{u}"], capture_output=True
    ).returncode != 0
    if no_upstream:
        if shutil.which("gitscribe"):
            subprocess.run(["gitscribe", "create-pr"])


@app.command(name="merge-check")
def merge_check_cmd():
    cfg = load_config()
    hard_block = cfg.get("hooks", {}).get("merge_check", {}).get("block_on_risk", False)
    try:
        result = hook_utils.get_cached_risk(cfg, base="HEAD", head="MERGE_HEAD")
    except Exception as e:  # noqa: BLE001 - fail open, same reasoning as pre-push
        console.warn(f"merge-check skipped ({e})")
        return
    threshold = cfg["risk_classifier"]["trivial_threshold"] * 4

    if result["risk_score"] >= threshold:
        console.warn(f"merge risk ({result['risk_score']:.2f}): {result['risk_reasoning']}")
        if hard_block:
            console.error("blocked — override with: git merge --no-verify")
            raise typer.Exit(code=1)
    console.success(f"merge-check passed (risk {result['risk_score']:.2f})")


@app.command(name="post-merge")
def post_merge_cmd(ff: str = typer.Option("0")):
    cfg = load_config()
    branch = current_branch()

    if not Path(".git/MERGE_MSG").exists() and ff == "1":
        return  # fast-forward, nothing merged to record

    try:
        diff = get_raw_diff(base="HEAD@{1}", head="HEAD")
        commits = get_commit_messages(base="HEAD@{1}", head="HEAD")
    except GitCommandError as e:  # no reflog entry (fresh clone / expired) - nothing to record
        console.warn(f"post-merge: could not read the merge range, skipping memory update ({e})")
        return
    state = GitScribeState(branch_name=branch, raw_diff=diff, commit_messages=commits)

    files = diff_parser_node(state, cfg)
    state.files_changed = files["files_changed"]
    summary = summarizer_node(state)
    memory.save_pr(branch, title=f"Merged: {branch}", body="\n".join(summary["change_summary"]))

    if cfg.get("hooks", {}).get("post_merge", {}).get("auto_tag", False):
        bump = hook_utils.BUMP_FILE.read_text().strip() if hook_utils.BUMP_FILE.exists() else "patch"
        tag = hook_utils.next_tag(bump)
        subprocess.run(["git", "tag", tag])
        print(f"gitscribe: tagged {tag} ({bump})")
        if cfg["hooks"]["post_merge"].get("push_tag", False):
            subprocess.run(["git", "push", "origin", tag])


@app.command(name="commit-msg")
def commit_msg_cmd(msg_file: str = typer.Argument(...)):
    cfg = load_config()
    if not cfg.get("hooks", {}).get("commit_msg", {}).get("enabled", True):
        return

    raw = Path(msg_file).read_text()
    subject = raw.splitlines()[0] if raw.splitlines() else ""
    if subject.startswith(("Merge ", "Revert ")):
        return

    if not hook_utils.COMMIT_RE.match(subject):
        print(f"✗ not a Conventional Commit:\n  {subject}", file=sys.stderr)
        print("  format: <type>(<scope>)!: <description>", file=sys.stderr)
        print("  types: feat fix build chore ci docs style refactor perf test", file=sys.stderr)
        raise typer.Exit(code=1)

    bump = hook_utils.bump_for_commit(subject, raw)
    hook_utils.BUMP_FILE.write_text(bump)


@app.command(name="merge-conflict")
def merge_conflict_cmd():
    files = hook_utils.conflicted_files()
    if not files:
        console.success("no conflicts detected")
        return
    console.warn(f"{len(files)} file(s) with conflicts:")
    for f in files:
        console.line(f"  - {f}", severity="warning")


@app.command(name="merge-preview")
def merge_preview_cmd(
    branch: str = typer.Argument(..., help="Branch to speculatively merge"),
    base: str = typer.Option("HEAD", help="Ref to merge into (defaults to the current branch)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON instead of formatted text"),
):
    """Speculatively merge `branch` into `base` inside a disposable worktree
    and, if it conflicts, propose a resolution per hunk grounded in blast
    radius and each branch's recovered PR intent. Never touches your real
    working tree, index, or branches — nothing here is auto-applied.

    Also available as `git merge-preview <branch>` after `gitscribe init`.
    """
    cfg = load_config()
    require_api_key()
    try:
        ours = current_branch()
        report = run_merge_preview(cfg, ours_branch=ours, theirs_branch=branch, base_ref=base)
    except (WorktreeError, MissingAPIKeyError, GitCommandError) as e:
        console.error(str(e))
        raise typer.Exit(1) from e

    if json_output:
        typer.echo(report.model_dump_json(indent=2))
        return

    if report.clean:
        console.success(f"no conflicts merging '{branch}' into '{ours}'")
        return

    console.warn(
        f"{report.total_hunks} conflict(s) across {len(report.files)} file(s) "
        f"merging '{branch}' into '{ours}'"
    )
    for file_report in report.files:
        console.info(f"\n{file_report.file}")
        if not file_report.resolutions:
            console.line("  binary or unparseable - resolve manually", severity="warning")
            continue

        for res in file_report.resolutions:
            tag = "escalated" if res.escalated else "batch"
            severity = "info" if res.confidence == "high" else "warning"
            console.line(f"  hunk {res.hunk_index} - confidence: {res.confidence} ({tag})", severity=severity)
            console.info(f"    {res.rationale}")
            if res.confidence == "high":
                console.info("    proposed resolution:")
                for line in res.resolved_text.splitlines():
                    console.info(f"      {line}")
            else:
                console.info("    -> flagged for manual review, not shown as a proposal")

    console.info(
        f"\n{report.total_safe}/{report.total_hunks} hunk(s) high-confidence. "
        "Nothing has been applied — this is a preview. Run the real merge "
        "yourself once you've reviewed it."
    )

if __name__ == "__main__":
    app()
