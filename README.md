# GitScribe

**A local-first, bring-your-own-key (BYOK) Git and code-intelligence CLI.**
GitScribe writes PR descriptions and commit messages, builds a searchable graph of your code, lints and
security-verifies your changes, and previews merge conflicts, all from the terminal, with live progress for
every long-running step.

- **Local-first.** The code index, embeddings and run history live in SQLite on your machine. Embeddings are
  computed locally by default. Nothing is sent to an LLM unless you run a command that needs one.
- **BYOK.** You supply the LLM provider and key. GitScribe never proxies your traffic and has no server.
- **Safe by default in git.** Hooks fail open on their own internal errors, so GitScribe can't wedge a push or
  merge because a model was unreachable.
- **Scriptable.** Every long-running command can stream NDJSON events (`--stream json`) for CI, editors and agents.

---

## Contents

1. [Quick start](#quick-start)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Commands](#commands)
   - [Progress output and `--stream`](#progress-output-and---stream)
   - [PR descriptions: `generate`, `create-pr`](#pr-descriptions-generate-create-pr)
   - [Commits: `commit-message`, `commit`](#commits-commit-message-commit)
   - [Code index: `index`, `query`, `graph`](#code-index-index-query-graph)
   - [Lint and review: `lint`, `review`](#lint-and-review-lint-review)
   - [Security verification: `verify`](#security-verification-verify)
   - [Merge tools: `merge-preview`, `merge-check`, `merge-conflict`](#merge-tools)
   - [Git hooks: `init`](#git-hooks-init)
   - [Local sandbox model: `sandbox`](#local-sandbox-model-sandbox)
6. [Using GitScribe in CI and scripts](#using-gitscribe-in-ci-and-scripts)
7. [How it works](#how-it-works)
8. [Data, storage and privacy](#data-storage-and-privacy)
9. [Troubleshooting](#troubleshooting)
10. [Development](#development)
11. [Roadmap](#roadmap)

---

## Quick start

```bash
# 1. install (from a clone of this repo)
pip install -e .

# 2. in the repository you want to use it on
cd your-repo
cp config.example.yaml config.yaml      # or create config.yaml, see "Configuration"
export API_KEY=<your-llm-provider-key>  # or run `gitscribe init`, which offers to save it to .env

# 3. install the git hooks (optional but recommended)
gitscribe init

# 4. try the services
gitscribe index                          # build the code graph + embeddings
gitscribe commit --path src/api          # AI commit message for the modified files under src/api
gitscribe generate                       # PR description for the current branch
gitscribe verify --working               # security + lint verification of uncommitted work
```

> Run GitScribe from the **repository root**. The local databases are created relative to the current directory
> (see [Data, storage and privacy](#data-storage-and-privacy)).

---

## Requirements

| Requirement | Needed for | Notes |
|---|---|---|
| Python 3.11+ | everything | Check the `requires-python` in `pyproject.toml` for the exact floor. |
| `git` | everything | |
| An LLM API key | `generate`, `create-pr`, `commit` (without `-m`), `commit-message`, `query` (without `--raw`), `review`, `merge-preview` | Provider is chosen in `config.yaml`. |
| `ruff` | `lint`, `review`, `verify` | Installed as a project dependency. |
| `gh` (GitHub CLI), authenticated | `create-pr` | Run `gh auth login` once. |
| `semgrep`, `osv-scanner`, `betterleaks` | `verify` | External binaries on `PATH`. A missing scanner is reported as `unavailable`, not silently skipped. See `gitscribe verify --list-scanners`. |
| `llama-server` ([llama.cpp](https://github.com/ggml-org/llama.cpp)) | the local AI review pass (`--sandboxed`, `--force-agentic`) and the pre-push hook | Optional. GitScribe detects it but never installs it. |

---

## Installation

```bash
git clone <this-repo-url> gitscribe
cd gitscribe
python -m venv .venv && source .venv/bin/activate
pip install -e .
gitscribe --version
```

The LLM client is provider-agnostic (it uses LangChain's `init_chat_model`), so install the integration package for
**your** provider, for example `pip install langchain-groq` for the default `groq` provider or
`pip install langchain-openai` for `openai`.

Verify the install:

```bash
gitscribe --help
gitscribe verify --list-scanners
```

---

## Configuration

### Where the config lives

`config.yaml` is resolved the same way for every command:

1. the `GITSCRIBE_CONFIG` environment variable, if set;
2. otherwise the nearest `config.yaml` found walking **up** from the current directory, stopping at the repo
   root (the first directory containing `.git`);
3. otherwise `./config.yaml`.

So you can run any command from a subdirectory and still use the root config. The file is validated on every run;
an invalid value fails fast with the exact field.

### Minimal config

```yaml
llm:
  provider: groq                 # any provider your langchain integration supports
  model: <primary-model-id>
  fallback_model: <fallback-model-id>   # used if the primary fails or is rate limited
  # base_url: https://...        # optional: OpenAI-compatible gateways, proxies, local servers
  temperature: 0.2
  max_tokens: 1000
```

Everything else has a default. A fuller example with the settings you are most likely to change:

```yaml
llm:
  provider: groq
  model: <primary-model-id>
  fallback_model: <fallback-model-id>
  max_tokens: 1000

embedding:
  provider: local                # local = no API calls, no cost
  model: all-MiniLM-L6-v2

failure_handling:
  max_retries: 2
  retry_backoff_seconds: 2

hooks:
  pre_push:     { block_on_risk: false }   # true = a high-risk diff blocks `git push`
  merge_check:  { block_on_risk: false }
  post_merge:   { auto_tag: false, push_tag: false }
  commit_msg:   { enabled: true }          # enforce Conventional Commits

validation:
  enabled: true
  fail_closed: true              # a scanner that errors fails the gate
  fail_on: [critical, high]
  block_secrets: true
  block_new_vulnerabilities: true
  block_pr_generation: false     # true = a failing verification stops PR generation
  deterministic:
    semgrep_config: p/default    # do NOT use "auto": it needs metrics, which GitScribe turns off
    timeout_seconds: 120
  ai:
    enabled: true
    base_url: http://127.0.0.1:8080   # local llama.cpp server
    model: qwen2.5-coder-3b-instruct-q4_k_m

ignore_patterns:                 # extra gitignore-style patterns excluded from diffs
  - "*.lock"
  - "dist/"
```

### Settings reference

| Section | Key | Default | Meaning |
|---|---|---|---|
| `llm` | `provider` | `groq` | Chat-model provider. |
| | `model`, `fallback_model` | required | Primary and fallback models. |
| | `base_url` | none | Override the provider endpoint. |
| | `temperature`, `max_tokens` | `0.2`, `1000` | Generation parameters. |
| `retrieval` | `min_prs`, `max_prs`, `branch_prefix_match` | `3`, `10`, `true` | How many past PRs are retrieved as style/context for PR generation. |
| `risk_classifier` | `enabled`, `trivial_threshold`, `structural_weight` | `true`, `0.15`, `0.3` | Risk scoring that decides how much work generation does and what hooks warn about. |
| `failure_handling` | `max_retries`, `retry_backoff_seconds` | `2`, `2` | Retry policy before the template fallback. |
| `embedding` | `provider`, `model` | `local`, `all-MiniLM-L6-v2` | Embeddings for the code index. Local by default so indexing never costs API calls. |
| `merge_preview` | `escalate_below_confidence`, `blast_radius_depth`, `worktree_cleanup` | `high`, `2`, `true` | Conflict-resolution behaviour. |
| `review.lint` / `review.agentic` | `enabled`, `max_context_tokens`, `hops`, ... | enabled, `6000`, `2` | Controls for `gitscribe review`. |
| `hooks.*` | see example above | | Per-hook behaviour. |
| `validation` | `enabled`, `fail_closed`, `fail_on`, `block_secrets`, `block_new_vulnerabilities`, `block_pr_generation` | see example | The verification gate. |
| `validation.deterministic` | `timeout_seconds`, `max_workers`, `semgrep_config`, `betterleaks_live_validation` | | Scanner execution. |
| `validation.ai` | `enabled`, `base_url`, `model`, `api_key_env`, `max_context_tokens`, `max_output_tokens`, `max_turns` | | The local AI review pass. |
| `validation.file_rules` | `[{path, mode}]` | `[]` | Per-path review mode (`static`, `agentic`, `both`). |
| top level | `ignore_patterns` | `[]` | Extra patterns excluded from every diff. |

### Environment variables

| Variable | Purpose |
|---|---|
| `API_KEY` | Your LLM provider key. Read from the environment or a local `.env`. `gitscribe init` can write it for you (file mode `600`). |
| `GITSCRIBE_CONFIG` | Explicit path to `config.yaml`. |
| `VALIDATION_API_KEY` | Optional key for the local sandbox endpoint (name configurable via `validation.ai.api_key_env`). |
| `GITSCRIBE_HOME` | Where the sandbox model and manifest are cached (default `~/.cache/gitscribe`). |
| `GITSCRIBE_LLAMA_RUN_DIR` | Where the local `llama-server` PID/state is kept. |

> **Never commit `.env`.** `gitscribe init` warns if it is not in `.gitignore`.

---

## Commands

Run `gitscribe --help` or `gitscribe <command> --help` for the authoritative option list.

| Command | What it does | Needs LLM |
|---|---|---|
| `init` | Install git hooks and set up your API key | no |
| `generate` | Generate a PR description for the current branch | yes |
| `create-pr` | Generate a description and open the PR with `gh` | yes |
| `commit-message` | Print a Conventional Commit message for staged or selected changes | yes |
| `commit` | Commit staged changes or the changes under `--path` | yes (not with `-m`) |
| `index` | Build or refresh the code graph and embeddings | no |
| `query` | Ask a question about the indexed codebase | yes (not with `--raw`/`--json`) |
| `graph` | Blast-radius and dependency view for a symbol | no |
| `lint` | List ruff findings; `--fix` and `--format` repair them | no |
| `review` | Lint plus agentic review, persisted as findings | optional |
| `verify` | Security and quality verification gate | optional (local) |
| `merge-preview` | Speculative merge with proposed conflict resolutions | yes |
| `sandbox` | Manage the local llama.cpp model used by `verify` | no |

Internal commands run by the git hooks: `pre-push`, `merge-check`, `post-merge`, `commit-msg`, `merge-conflict`.

### Progress output and `--stream`

`index`, `generate`, `create-pr`, `commit`, `commit-message`, `query` and `verify` show live progress, so
you are never staring at a blank screen. The display is **inline**: a compact phase list with progress bars that
updates in place. It never takes over the screen.

```
◆ index  src/api
✔ discover files   files 12  0.0s
✔ hash files       0.0s
⠋ parse symbols    ██████████░░░░░░░░░░░░░░ 5/12 src/api/routes.py
```

`--stream` modes (default `auto`):

| Mode | Behaviour |
|---|---|
| `auto` | Live view on a terminal, silent when output is piped. |
| `live` | Force the live view. |
| `json` | **NDJSON events on stdout**, one JSON object per line, ending with a `result` line. Human messages move to stderr. |
| `off` | No progress output. |

Event types (every event has `type` and `ts`):

| Event | Fields |
|---|---|
| `phase.started` | `name` |
| `phase.progress` | `name`, `current`, `total`, `label` |
| `phase.finished` | `name`, `seconds`, plus per-phase detail (for example `files`, `symbols`) |
| `phase.failed` | `name`, `seconds`, `error` |
| `plan.ready` | `jobs` (verify) |
| `scanner.started` / `scanner.finished` | `scanner`, `findings`, `seconds`, `error` (verify) |
| `finding.found` | `scanner`, `severity`, `category`, `file`, `line`, `message` (verify) |
| `gate.decision` | `status`, `reason`, `findings`, `errors`, `severities` (verify) |
| `result` | command-specific final payload |

```bash
gitscribe index --stream json | jq -c 'select(.type=="phase.finished")'
```

Every event is also logged at `DEBUG` on the `gitscribe.events` logger with the payload in `record.event`.

### PR descriptions: `generate`, `create-pr`

```bash
gitscribe generate                         # diff of origin/main...HEAD
gitscribe generate --style concise         # default | concise | detailed
gitscribe generate --path src/api          # only describe changes under src/api
gitscribe generate --dry-run               # don't save to local memory (still calls the LLM)
gitscribe generate --no-validate           # skip the validation stage for this run
gitscribe create-pr                        # push the branch if needed, then open the PR via gh
```

Pipeline: read diff, summarize, validate, assess risk, retrieve similar past PRs, generate. If generation fails it
retries, switches to `fallback_model`, and finally falls back to a deterministic template, so you always get output.

- **`--path`** (repeatable) narrows the diff **before** summary, risk scoring, retrieval and validation, so only the
  selected files influence the description. For `create-pr` it narrows the generated *description* only; the PR still
  contains every commit on the branch.
- **Validation inside generation.** By default `validation.block_pr_generation` is `false`: validation findings are
  shown but never stop the description from being written. Set it to `true` to make a failing gate skip the LLM.
  When validation fails, the top findings are printed (file, line, message) with a pointer to `gitscribe verify` for
  the full report. Use `--no-validate` / `--validate` to skip or force it for one run.

### Commits: `commit-message`, `commit`

```bash
# stage something, then:
gitscribe commit-message                   # prints only the message (stdout), progress goes to stderr
git commit -m "$(gitscribe commit-message)"

# commit only the modified files under a folder, with an AI-written message
gitscribe commit --path src/api            # shows the message, asks: [y]es / [e]dit / [n]o

# you write the message, no LLM call
gitscribe commit --path src/api -m "fix(api): handle empty payloads"

gitscribe commit --path src/api --dry-run  # show message + files, commit nothing
gitscribe commit --path src/api --yes      # no prompt (required in scripts/CI)
gitscribe commit --path src/api --edit     # open the message in $EDITOR first
gitscribe commit --path src/api -u         # also include new (untracked) files under the path
```

- **Scope rule.** With `--path`, GitScribe commits **only** the changes under those paths. Other staged work stays
  staged. Without `--path` it uses what is already staged. The message describes exactly what gets committed.
- **Conventional Commits.** Messages must satisfy the repo's `commit-msg` hook (`feat|fix|build|chore|ci|docs|style|refactor|perf|test`,
  optional lowercase scope, optional `!`). An invalid message is never committed non-interactively.
- **Token budget.** The prompt contains the file stat summary plus the patch truncated to 12,000 characters, which
  keeps it inside small per-minute token limits.
- **Non-interactive.** Without `--yes` (or `--dry-run`) a non-TTY session exits with an explanation instead of hanging.
- `--no-verify` skips git hooks, including the `commit-msg` check.

### Code index: `index`, `query`, `graph`

```bash
gitscribe index                            # incremental: only changed files are re-parsed
gitscribe index --path src/api             # (re)index only this folder or file
gitscribe index --force                    # full rebuild
gitscribe index --json                     # machine-readable stats

gitscribe query "where do we validate webhook signatures?"
gitscribe query "..." --raw                # retrieved context only, no LLM call
gitscribe query "..." --json               # structured retrieval, no LLM call, no cost
gitscribe query "..." --top-k 8

gitscribe graph PaymentService             # callers and callees, depth 2
gitscribe graph PaymentService --depth 3 --json
```

- The index is a SQLite symbol graph (functions, classes, imports, call/inheritance edges) plus per-symbol
  embeddings. Python only for now; parsing uses the standard `ast` module.
- Rebuilds are **incremental** (content-hash based). Phases: discover, hash, parse, link, embed, finalize.
- **Scoped runs are safe.** `--path` never treats files outside the scope as deleted, so you can refresh one
  folder without disturbing the rest of the index.
- `query` retrieves symbols by embedding similarity, expands over the graph, and (unless `--raw`/`--json`) asks the
  LLM to answer with citations as `name file:line`.
- `graph` accepts exact or partial symbol names and lists ambiguous matches instead of guessing.

### Lint and review: `lint`, `review`

```bash
gitscribe lint                             # every finding: file, line:col, severity, rule, message
gitscribe lint --path src/api              # limit to a folder/file
gitscribe lint --diff                      # preview what --fix/--format would change; modifies nothing
gitscribe lint --fix                       # apply ruff's safe fixes, then re-lint
gitscribe lint --fix --unsafe-fixes        # also apply fixes ruff marks unsafe
gitscribe lint --format                    # also run `ruff format`
gitscribe lint --fix --format              # the usual one-shot repair
gitscribe lint --json                      # for scripts
gitscribe lint --no-fails-on-error         # always exit 0
```

`lint` exits **1** when error-severity findings remain (`F`, `E9`, `S` rule families), and **2** if ruff itself
fails (for example an invalid ruff config). It never reports "no findings" when ruff crashed.

```bash
gitscribe review --lint-only               # static pass only, no LLM cost
gitscribe review                           # lint + batched agentic review of the diff
gitscribe review --sandboxed               # route the agentic pass through the local model
```

`review` stores its findings in the local database for use by `query` and risk scoring; it prints a summary and
points you to `gitscribe lint` for details.

### Security verification: `verify`

The verification gate. It answers: *is this change safe to ship?*

**What runs** (four scanners plus an optional local AI pass):

| Guarantee | Scanner |
|---|---|
| No secret ships | Betterleaks (always on, every file) |
| No known-vulnerable dependency | OSV-Scanner |
| No pattern-matched security bug | Semgrep |
| Code quality (Python) | Ruff |
| Semantic second opinion | Local AI review (llama.cpp), optional |

```bash
gitscribe verify                           # committed changes: origin/main...HEAD
gitscribe verify --staged                  # what you are about to commit
gitscribe verify --working                 # all uncommitted changes (working tree vs HEAD)
gitscribe verify --base main --head feature
gitscribe verify --path src/api            # restrict to changed files matching a path/glob
gitscribe verify --list-scanners           # what is installed and healthy

gitscribe verify --view diff               # findings printed inline under the changed lines
gitscribe verify --stream json             # NDJSON events + final result (CI, editors, agents)
gitscribe verify --json                    # one JSON document
gitscribe verify --sarif out.sarif         # SARIF for GitHub code scanning etc.
gitscribe verify --debug                   # full evidence in the report
```

Other options: `--mode static|agentic|both`, `--force-agentic`, `--sandboxed`, `--pre-push`, `--pre-merge`,
`--install-hook` / `--uninstall-hook`.

**The report** shows a verdict banner with the exact reason the gate failed, a severity-by-category grid, the
top-risk files, findings grouped by file with evidence and a suggested fix, and a **scanner health** table
(`ok`, `error`, `unavailable`, `not needed`). If there is nothing to scan it says so and names the range, for
example *"nothing to verify: no changes in origin/main...HEAD (use --staged or --working for uncommitted work)"*.

**How the gate decides** (all configurable under `validation`):

| Rule | Fails when |
|---|---|
| `fail_on` | a finding has a severity in the list (default `critical`, `high`) |
| `block_secrets` | any finding is secret-like, regardless of severity |
| `fail_closed` | any scanner errored (so a broken scanner can never produce a false PASS) |

Exit code is `0` on PASS and non-zero otherwise, so it drops straight into hooks and CI.

> A scanner that is not installed is reported as `unavailable`. Install `semgrep`, `osv-scanner` and `betterleaks`
> to get full coverage. With `fail_closed: true`, a scanner error fails the gate.

### Merge tools

```bash
gitscribe merge-preview feature/x          # also: git merge-preview feature/x (alias set by `init`)
gitscribe merge-preview feature/x --base release/1.2 --json
gitscribe merge-conflict                   # list files currently in conflict
```

`merge-preview` merges the branch inside a **disposable worktree**, never touching your working tree, index or
branches. If it conflicts, it proposes a resolution per hunk, grounded in the code graph's blast radius and each
branch's PR intent, with a confidence rating. Low-confidence hunks are flagged for manual review rather than shown as
proposals. **Nothing is applied automatically.**

### Git hooks: `init`

```bash
gitscribe init                             # install hooks, alias, and offer to save your API key
gitscribe init --force                     # replace existing hooks (old ones kept as <name>.bak)
```

| Hook | Runs | Behaviour |
|---|---|---|
| `pre-push` | `gitscribe pre-push` then `gitscribe verify --pre-push --sandboxed` | Warns on high-risk diffs; blocks only if `hooks.pre_push.block_on_risk: true`. Opens a PR via `create-pr` when the branch has no upstream. |
| `pre-merge-commit` | merge risk check | Same model, `hooks.merge_check`. |
| `post-merge` | records the merge in local memory | Optional auto-tagging (`post_merge.auto_tag`). |
| `commit-msg` | Conventional Commits check | Skipped silently if `gitscribe` is not on `PATH`. |

**Hooks fail open.** If a hook's own check can't run (no `origin/main`, expired reflog, model unreachable), it prints a
warning and lets git continue. Only a genuine finding can block, and only if you configured it to. Override any hook
once with `--no-verify`.

The `pre-push` hook requires `gitscribe` on `PATH` and uses the local sandbox model; see below.

### Local sandbox model: `sandbox`

The AI review pass runs against a **local** llama.cpp server, so your code never leaves the machine.

```bash
gitscribe sandbox init                     # detect llama-server, download + verify the model
gitscribe sandbox start
gitscribe sandbox status
gitscribe sandbox stop
```

GitScribe never installs `llama-server` itself; `init` tells you what to install if it is missing. Model files are
checksummed on first download and re-verified on every reuse.

---

## Using GitScribe in CI and scripts

```bash
# gate a pull request
gitscribe verify --base origin/main --head HEAD --sarif gitscribe.sarif --stream json > events.ndjson
echo $?        # 0 = PASS

# machine-readable results from a service
gitscribe index --stream json | tail -n1                 # {"type": "result", ...}
gitscribe lint --json | jq 'length'

# non-interactive commit (CI bots)
gitscribe commit --path services/billing --yes
```

Guidelines:

- Use `--stream json` or `--json` for parsing; never scrape the live view.
- In `--stream json` mode stdout is **only** NDJSON. Human messages go to stderr.
- `commit` requires `--yes` (or `-m` plus `--yes`) outside a terminal.
- Set `API_KEY` as a CI secret. Do not write `.env` in CI.

---

## How it works

```
                         ┌──────────────────────────── CLI (typer + rich) ───────────────────────────┐
                         │ generate  create-pr  commit  index  query  graph  lint  review  verify   │
                         └───────┬───────────────────────────────────────────────┬───────────────────┘
                                 │ phase / scanner events (gitscribe.events)     │
             ┌───────────────────▼──────────────────┐               ┌────────────▼─────────────┐
             │ Generation graph (LangGraph)          │               │ Validation graph          │
             │ diff → summarize → validate → risk →  │──validate────▶│ resolve → plan → execute  │
             │ retrieve → generate → retry/fallback  │               │ (scanners, concurrent)    │
             └───────┬──────────────────┬────────────┘               │ → aggregate → policy gate │
                     │                  │                            └────────────┬──────────────┘
          ┌──────────▼───────┐   ┌──────▼─────────┐                  ┌────────────▼───────────────┐
          │ Memory (SQLite)  │   │ Code index     │                  │ ruff · semgrep · osv ·     │
          │ past PRs         │   │ symbols, edges,│                  │ betterleaks · local AI     │
          └──────────────────┘   │ embeddings     │                  └────────────────────────────┘
                                 └────────────────┘
```

- **Events.** One event model (`gitscribe/events.py`) drives the live terminal view, the NDJSON stream and the
  debug logs, so every renderer sees the same facts.
- **LLM client.** `core/llm_client.py` builds the chat model from `config.yaml`; the same code path serves every
  provider. Calls are timed and recorded to a local JSONL log.
- **Index.** `core/indexer/`: `parser` (AST symbols), `graph_builder` (call/import/inheritance edges),
  `embedder` (local embeddings), `merkle` (change detection), `index_store` (public API: `reindex`, `search`,
  `blast_radius`). Storage is SQLite; traversal uses recursive CTEs.
- **Validation.** `validation/`: resolver, mode selection, scanner registry/runner, aggregator, policy and
  reporters (terminal, NDJSON, SARIF).

```
src/gitscribe/
├── cli.py                 # command surface
├── ui.py, console.py      # inline UI, plain output helpers
├── events.py              # shared event model
├── config_locator.py      # config.yaml resolution
├── hooks/*.sh             # git hook scripts installed by `init`
├── core/                  # generation graph, LLM client, memory, indexer, analysis, merge preview
└── validation/            # verification gate, scanners, policy, reports, sandbox
```

---

## Data, storage and privacy

Created **relative to the current directory** (so run from the repo root):

| Path | Contents |
|---|---|
| `Storage/gitscribe.db` | Local memory (past PR descriptions) |
| `Storage/gitscribe_index.db` | Code symbols, edges, embeddings, file hashes |
| `Storage/logs/llm_calls.jsonl` | Per-call LLM timing/usage log |
| `~/.cache/gitscribe/` | Sandbox model weights and manifest |

Add `Storage/` and `.env` to `.gitignore`.

**What leaves your machine:** only the prompts for commands marked "Needs LLM" above, sent to the provider you
configured with your key. The prompt contains the diff (truncated to a budget), file names, and retrieved
context. Embeddings are computed locally by default. `semgrep_config: p/default` downloads rules from the Semgrep registry
(rules in, no code out); set it to a local ruleset path for fully offline use. The sandbox AI pass is loopback-only.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `API_KEY not set` | `export API_KEY=...` or run `gitscribe init`. |
| `config file not found` / `invalid config.yaml` | Create `config.yaml` at the repo root or set `GITSCRIBE_CONFIG`. The message names the invalid field. |
| `semgrep failed: Cannot create auto config when metrics are off` | `semgrep_config` is `auto`. Set it to `p/default` or a local ruleset path. |
| Generation says `validation_blocked: FAIL ...` and used a template | `validation.block_pr_generation` is `true` and verification failed. The findings are printed; fix them, run once with `--no-validate`, or set `block_pr_generation: false`. |
| `verify` reports PASS with nothing listed | No changes in `origin/main...HEAD`. Use `--staged` or `--working`, or pass `--base/--head`. |
| A scanner shows `unavailable` / `error` | The binary is missing or failed. `gitscribe verify --list-scanners`, then install it. With `fail_closed: true` a scanner error fails the gate. |
| Provider returns 429 / token-per-minute errors | Lower `llm.max_tokens`, use `--path` to shrink the diff, or switch `llm.fallback_model`. Commit messages cap the patch at 12,000 characters. |
| `ruff failed: ...` from `lint` | ruff itself errored (often an invalid `pyproject.toml`/`ruff.toml`). Fix that config; GitScribe will not report this as "no findings". |
| `no changes under <path>` | The path has no modified files vs `HEAD` (commit) or vs the base branch (generate). |
| `path not found` | A `--path` typo. GitScribe fails early rather than doing nothing. |
| `commit` exits with "non-interactive session" | Add `--yes` (or `--dry-run`). |
| `git push` blocked: `gitscribe` not found | The `pre-push` hook needs `gitscribe` on `PATH`. Activate the virtualenv or remove the hook. |
| `query` finds nothing | Run `gitscribe index` first, or rephrase. |
| A hook misbehaves | Bypass once with `--no-verify`; reinstall with `gitscribe init --force`. |

For a full event trace of any run, use `--stream json`, or set the `gitscribe` logger to `DEBUG`.

---

## Development

```bash
pip install -e ".[dev]"      # or: pip install -e . pytest
pytest -q                    # unit + integration tests
pytest tests/test_validation -q
```

- Tests build throwaway git repositories and stub LLM and scanner calls; no network or API key is needed.
- A failing validation test prints a structured NDJSON trace of the events and log records that led to the failure.
- `src/gitscribe/vulnerable_fixture.py` is an intentionally vulnerable file used to exercise the scanners. It will
  (correctly) trip `verify`; keep it out of release builds and verification of your own changes, ideally by moving it
  under `tests/fixtures/`.
- Conventions: Conventional Commits (enforced by the `commit-msg` hook), ruff for lint/format, new long-running work
  reports progress through `gitscribe.core.progress.phase()` so it appears in every output mode.

---

## Roadmap

GitScribe started as a PR-description generator and is growing into a local-first, BYOK code-analysis platform,
shipped in independently useful stages:

- **Done:** PR generation pipeline, parsing/indexing layer (Python), analysis and RAG, CLI platform commands,
  validation gate with scanners and reports, commit workflow, scoped and streamed services.
- **Next:** multi-language indexing (tree-sitter), incremental index keyed off the diff, `agents.md` and git
  workflow for automated repo scans, remaining services (`lint`/`review`/`graph`) on the shared event stream.
- **Non-goals for now:** a new linter (GitScribe wraps ruff), a graph database (SQLite until proven insufficient),
  routing embeddings through a paid API by default.

---

## License

Add your license here (and a `LICENSE` file at the repo root).
