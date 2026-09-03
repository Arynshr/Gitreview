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

from gitscribe import console
from gitscribe.config_locator import find_config_path
from gitscribe.core import hooks as hook_utils, memory
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
from gitscribe.core.merge_preview import WorktreeError, run_merge_preview
from gitscribe.core.state import GitScribeState
from gitscribe.core.summarizer import summarizer_node

load_dotenv(find_dotenv(usecwd=True))

app = typer.Typer(help="GitScribe: A stateful AI-powered Git and code intelligence system")

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
def init():
    """Install the pre-push, pre-merge-commit, post-merge, and commit-msg git hooks into .git/hooks/."""
    if not repo_hooks_dir.exists():
        console.error(".git/hooks not found - run this from a git repo root")
        raise typer.Exit(1)

    dest = repo_hooks_dir / "pre-push"
    if dest.exists():
        console.warn(f"{dest} already exists - not overwriting. Remove it first if you want to reinstall.")
        return

    hook_names = ["pre-push", "pre-merge-commit", "post-merge", "commit-msg"]
    for hook_name in hook_names:
        src = Path(__file__).parent / "hooks" / f"{hook_name}.sh"
        dest = repo_hooks_dir / hook_name  # git requires the extensionless name here
        if not src.exists():
            console.error(f"hook source missing: {src}")
            raise typer.Exit(1)
        dest.write_text(src.read_text())
        if os.name == "posix":
            dest.chmod(dest.stat().st_mode | stat.S_IEXEC)

    console.success(f"installed {len(hook_names)} git hooks into {repo_hooks_dir}")
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
    console.success(f"wrote API_KEY to {ENV_PATH.resolve()}")


@app.command()
def generate(
    dry_run: bool = typer.Option(False, "--dry-run", help="Skip saving to memory; still calls the LLM"),
    style: Style = typer.Option(Style.default, "--style", help="Style preset for the generated description"),
):
    """Generate a PR description for the current branch's diff."""
    require_api_key()
    config = load_config()
    graph = build_graph(config)

    try:
        initial_state = GitScribeState(
            branch_name=current_branch(),
            style=style.value,
            attempt_count=0,
            fallback_used=False,
            status="pending",
        )
        result = graph.invoke(initial_state)
    except GitCommandError as e:
        console.error(str(e))
        raise typer.Exit(1) from e

    console.info(f"\nTitle: {result.get('pr_title')}\n")
    console.info(result.get("pr_body", "(no body generated)"))

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
    config = load_config()
    graph = build_graph(config)
    try:
        result = graph.invoke(GitScribeState(
            branch_name=branch,
            style=style.value,
            attempt_count=0,
            fallback_used=False,
            status="pending",
        ))
    except GitCommandError as e:
        console.error(str(e))
        raise typer.Exit(1) from e

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
):
    """Build/refresh the code graph + embeddings. Incremental by default
    (Merkle hash skip/reuse) — pass --force for a full rebuild.
    """
    config = load_config()  # already a dict — see load_config() docstring
    result = index_store.reindex(repo_root, config, force=force)

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
        findings = linter_mod.run_ruff(".")
        lint_count = linter_mod.write_lint_findings(findings)
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

@app.command()
def query(
    text: str = typer.Argument(..., help="Natural language query"),
    top_k: int = typer.Option(5, help="Number of matches"),
    json_output: bool = typer.Option(False, "--json", help="Raw retrieval context as JSON, no LLM call"),
    raw: bool = typer.Option(False, "--raw", help="Print raw retrieved context only, skip LLM synthesis"),
):
    """Ask a question about the indexed codebase.

    By default this synthesizes an answer via the configured LLM, grounded
    in retrieved symbols (cited as name/file:line). Use --raw for the
    context-block output only, or --json for structured retrieval data
    with no LLM call (and no cost).
    """
    config = load_config()
    try:
        context = retrieve(text, config, top_k=top_k)
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


@app.command()
def lint(
    repo_root: str = typer.Option(".", help="Repo root to lint"),
    json_output: bool = typer.Option(False, "--json"),
    fails_on_error: bool = typer.Option(True, help="Exit 1 if any error-severity finding exists"),
):
    try:
        findings = linter_mod.run_ruff(repo_root)
    except linter_mod.RuffNotFoundError as e:
        console.error(str(e))
        raise typer.Exit(1) from e

    if json_output:
        typer.echo(json.dumps([f.model_dump() for f in findings], indent=2))
    else:
        if not findings:
            console.success("no findings")
        for f in findings:
            location = f"{f.file}:{f.lineno}"
            console.line(f"{f.severity:8} {location:50} {f.code:6} {f.message}", severity=f.severity)
        if findings:
            typer.echo(f"\n{len(findings)} findings, severity score {linter_mod.severity_score(findings):.2f}")

    has_errors = any(f.severity == "error" for f in findings)
    if fails_on_error and has_errors:
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
    symbol_id, matched_name = _find_symbol(conn, symbol)
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

@app.command(name="pre-push")
def pre_push_cmd():
    cfg = load_config()
    hard_block = cfg.get("hooks", {}).get("pre_push", {}).get("block_on_risk", False)
    result = hook_utils.get_cached_risk(cfg, base="origin/main", head="HEAD")
    threshold = cfg["risk_classifier"]["trivial_threshold"] * 3

    if result["risk_score"] >= threshold:
        print(f"⚠ high-risk diff ({result['risk_score']:.2f}): {result['risk_reasoning']}")
        if hard_block:
            print("blocked — override with: git push --no-verify")
            raise typer.Exit(code=1)

    no_upstream = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "@{u}"], capture_output=True
    ).returncode != 0
    if no_upstream:
        subprocess.run(["gitscribe", "create-pr"])


@app.command(name="merge-check")
def merge_check_cmd():
    cfg = load_config()
    hard_block = cfg.get("hooks", {}).get("merge_check", {}).get("block_on_risk", False)
    result = hook_utils.get_cached_risk(cfg, base="HEAD", head="MERGE_HEAD")
    threshold = cfg["risk_classifier"]["trivial_threshold"] * 4

    if result["risk_score"] >= threshold:
        print(f"⚠ merge risk ({result['risk_score']:.2f}): {result['risk_reasoning']}")
        if hard_block:
            print("blocked — override with: git merge --no-verify")
            raise typer.Exit(code=1)
    print(f"✓ merge-check passed (risk {result['risk_score']:.2f})")


@app.command(name="post-merge")
def post_merge_cmd(ff: str = typer.Option("0")):
    cfg = load_config()
    branch = current_branch()

    if not Path(".git/MERGE_MSG").exists() and ff == "1":
        return  # fast-forward, nothing merged to record

    diff = get_raw_diff(base="HEAD@{1}", head="HEAD")
    commits = get_commit_messages(base="HEAD@{1}", head="HEAD")
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
        print("No conflicts detected.")
        return
    print(f"gitscribe: {len(files)} file(s) with conflicts:")
    for f in files:
        print(f"  - {f}")


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
    ours = current_branch()

    try:
        report = run_merge_preview(cfg, ours_branch=ours, theirs_branch=branch, base_ref=base)
    except (WorktreeError, MissingAPIKeyError) as e:
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
