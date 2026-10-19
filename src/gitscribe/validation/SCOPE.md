# Validation service: scope reassessment

## Objective (the one thing this service is for)

Give every `gitscribe verify` run three language-agnostic guarantees,
plus one semantic pass that generalizes without new per-language tooling:

1. **No secret ever ships.** Unconditional, every file, every language,
   every mode. (Betterleaks)
2. **No known-vulnerable dependency ships.** One scanner, every
   ecosystem it covers, not one tool per package manager. (OSV-Scanner)
3. **No pattern-matched security bug ships.** One general-purpose,
   multi-language static analyzer - not a different tool per language.
   (Semgrep)
4. **A semantic/contextual second opinion**, for the class of issue no
   pattern-matcher catches, that scales to a new language by writing a
   better prompt, not by integrating a new binary. (the local AI pass,
   `validation/ai.py`)

Plus **one** pragmatic, zero-config quality linter for the single most
common target language today (Ruff, Python). That is the entire default
tool list. Four scanners. Not ten.

## Why this reassessment happened

The registry had grown to Ruff, Bandit, Betterleaks, Semgrep,
OSV-Scanner, Trivy, ESLint, Bearer, Clang-Tidy, and CodeQL, plus a
separate Syft/SBOM utility - accreted one "next phase" at a time without
ever asking whether the new tool earned its place against what was
already there. The result was:

- **Real redundancy**: three to four tools independently doing
  pattern-based security analysis on overlapping languages, and two
  tools doing Python dependency-vulnerability scanning.
- **No scaling story for a new language.** The implicit pattern - "new
  language shows up, add a new scanner binary for it" - is exactly what
  doesn't scale. A service that needs a new subprocess dependency, a new
  JSON/SARIF parser, and a new install-script entry for every language
  it wants to support is not a scalable architecture; it's a linear
  maintenance burden with no ceiling.
- **Operational mismatches papered over with flags** (`enable_codeql`,
  long custom timeouts) rather than asked whether the tool belonged in
  this service at all.

The fix is not "make the unused ones opt-in" - that was already tried
(CodeQL, Betterleaks live-validation) and just relocates the complexity.
The fix is removing what doesn't pay for itself.

## What was cut, and why specifically

| Tool | Why it's gone |
|---|---|
| **Bandit** | Python security findings are a subset of what Semgrep's own Python ruleset already covers. Keeping both means two tools reporting the same class of issue on the same files under different rule-ID namespaces - the exact duplication Bandit itself was introduced to resolve (it replaced Ruff's `S`-rules for this reason originally). One general-purpose SAST tool, not one per language. |
| **Bearer** | Same shape of redundancy as Bandit, one layer up: its security/privacy rules on Ruby/JS/TS/Java overlap Semgrep's own registry for those languages. Newer, smaller community, another SARIF-ingestion path to maintain for marginal unique coverage. |
| **CodeQL** | Not just redundant - a genuine architecture mismatch. Every other scanner is a single subprocess sized for a per-push gate (~60s). CodeQL needs a whole-program database build first, realistically minutes. It was already the one tool that needed its own opt-in flag and its own timeout budget to avoid breaking the rest of the bundle - that's a tool that doesn't belong in this service's execution model, not a tool that needs a bigger timeout. |
| **pip-audit** | OSV.dev's database already aggregates the PyPA advisory feed OSV-Scanner reads from, alongside every other ecosystem OSV-Scanner covers. Keeping pip-audit meant one dependency scanner per language again - the same anti-pattern as Bandit, for dependencies instead of code patterns. (Real, small trade-off: pip-audit can also audit an *activated* virtualenv's installed packages directly, not just lockfiles. OSV-Scanner is lockfile/manifest-based only. Acceptable loss for cutting an entire parallel tool.) |
| **Trivy** | Its only non-overlapping capability (IaC misconfiguration - Dockerfiles, Terraform, Kubernetes manifests) is infrastructure validation, not code validation. Out of this service's stated objective, not merely low-value. |
| **ESLint** | The clearest instance of the "new language, new scanner" anti-pattern. It also only ever fired when the *target* repo already had its own ESLint config - the narrowest, most conditional adapter in the whole set, which was itself a signal it was a edge-case add-on rather than core. |
| **Clang-Tidy** | Same anti-pattern as ESLint, plus it had no native JSON output (regex-parsed stdout) and required a `compile_commands.json` most target repos won't have. Three strikes: narrow trigger condition, fragile parsing, per-language sprawl. |
| **Syft (SBOM generation)** | Never actually wired into the graph/CLI in the first place - a capability nobody could invoke yet. Removing unused, unreachable code is not a loss. |

## What stayed, and why it's different

- **Ruff**: Python only, same shape of risk as ESLint/Clang-Tidy on
  paper - but it's zero-config (unlike ESLint, which refuses to run
  without the target repo's own setup) and cheap enough that it doesn't
  carry the same cost. Kept as the one deliberate exception, not a
  precedent for adding a linter per language.
- **Semgrep, OSV-Scanner, Betterleaks**: each is the *sole* tool in its
  category, each one is multi-language/multi-ecosystem by design, and
  none of the three overlaps either of the others' job. This is what
  "one tool, one job, scales across languages" looks like - the model
  for what gets added in the future, if anything does.

## The rule for adding a tool back

Before adding anything to this registry again: name the exact category
(secrets / dependency vulns / pattern-based SAST / semantic review) it
covers, and show that none of the four tools above already covers it for
the languages in question. If the honest answer is "it covers more
languages/rules in the same category," that's a case for expanding the
existing tool's configuration (a bigger Semgrep ruleset, a wider OSV
ecosystem list), not for registering a fifth scanner.
