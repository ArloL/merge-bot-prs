# merge bot prs

Script that auto-merges all open dependabot/renovate PRs across the `arlol` GitHub organization.

## Running

```
uv run merge_bot_prs.py
uv run merge_bot_prs.py --debug       # print mergeStateStatus and CI state per PR
uv run merge_bot_prs.py --count 5     # process at most N PRs in total (useful for testing)
```

Requires `gh` CLI authenticated with sufficient org-level access.

## How it works

1. Searches for all open PRs authored by `app/dependabot` or `app/renovate` across the org (up to 1000 each).
2. Groups PRs by repository, then processes each repo in parallel using a thread pool.
3. Within each repo, PRs are processed serially.

Per-PR logic (`process_pr`):
- Skip if `mergeStateStatus` is `DIRTY` or `DRAFT`.
- If the PR has a `github_actions`/`github-actions` label and is behind the base branch, bring the branch up to date (see below — dependabot is rebased directly, renovate is asked to rebase itself).
- Wait for all CI checks to complete.
- If CI failed, attempt a rebase (in case the failure was due to being out of date) and re-check.
- If CI passed, wait for `mergeStateStatus` to become `CLEAN` or `HAS_HOOKS` (handles rulesets like code scanning that finish after CI). Both this wait and the merge retry loop re-run CodeQL once if it has been green for ≥60s, to work around GitHub's stale code-scanning bug (see below).
- Run `verify_pr` (see Provenance checks below) and merge with `--rebase` only if it returns nothing.

Poll interval for all wait loops is 15 seconds.

## Output

Every line is `HH:MM:SS [repo#pr] message`, written through a lock and flushed immediately — repos are processed in parallel, so the timestamp is what makes an interleaved log readable afterwards.

`process_pr` returns an outcome for **every** exit path, logged as `outcome=<x> in <n>s`, and the run ends with a tally plus a list of everything that did not merge. Outcomes: `merged`, `ci-failed`, `dirty`, `draft`, `merged`/`closed` (resolved elsewhere mid-run), `not-mergeable-<state>`, `not-rebasable`, `not-rebasable-regenerating`, `rule-eval-timeout`, `head-changed`, `error-gh`/`error-unexpected`, and `unsafe-<reason>` for each provenance refusal.

The process exits 1 if any PR ended in an `error-` outcome, so an unsupervised run alerts without anyone reading the log.

Refusal reasons are deliberately short kebab-case strings so the end-of-run `Counter` tally stays readable; the specifics (which commit, which file, which line) go out as a `refusing: ...` log line at the moment of refusal.

Useful detail that is on by default: the failing check names behind `ci_passing=False`, the pending check names while waiting on CI, `old -> new` head SHAs after `update_branch`, and attempt counters on the bounded wait loops. `--debug` adds per-PR author/labels/check-count, `ahead`/`behind` counts, and a per-repo breakdown of what the search found.

## Key design decisions

- **`mergeStateStatus` is checked before merging** — not just CI. GitHub rulesets (e.g. required code scanning) can keep a PR `BLOCKED` even after all CI checks pass. The `wait_for_clean` function polls until the status is mergeable.
- **Getting a behind branch up to date differs by bot**:
  - **dependabot** — the branch is rebased directly via `gh pr update-branch --rebase` (`update_branch`). Commenting `@dependabot rebase` does *not* work: dependabot only rebases when the files it manages would change, so a PR sitting behind on commits that touch unrelated files gets an immediate "Looks like this PR is already up-to-date with main!" and never moves. Re-commenting produces the same reply, so `wait_for_rebase` would spin forever — hence it no longer waits on `is_behind` for dependabot PRs.
  - **renovate** — still triggered by checking `- [x] <!-- rebase-check -->` in the PR body. Renovate's `rebaseCheck` honours the box unconditionally, so it has none of dependabot's refusal behaviour. It is deliberately *not* switched to `update-branch`: that rewrites the commit's committer to the running user, and renovate's `isBranchModified` inspects both author and committer email, so the PR would be treated as human-edited and abandoned.
- **After `update_branch`, the script sleeps one poll interval** before checking CI. The status check rollup for the new head is empty until GitHub registers the workflows, and an empty rollup reads as "CI passed" in `check_ci_status`.
- **`is_behind` uses the compare API** directly (`repos/{repo}/compare/{base}...{head}`) rather than trusting `mergeStateStatus`, because `mergeStateStatus` is unreliable when branch protection rules are absent.
- **`--count` is a whole-run budget, not a batch size.** `main` re-searches the org each pass and keeps going until no unprocessed PRs remain, so slicing the per-pass list capped the batch and then looped anyway — `--count 20` drained the org 20 at a time. It is now `max(0, count - len(processed))`; the `max` matters, because a negative slice end trims from the tail instead of yielding nothing. This went unnoticed for two runs because both died before reaching pass 2.
- **`run_gh` retries transient errors, then calls `sys.exit(1)`** — 3 retries with 2/4/8s backoff when the stderr matches `TRANSIENT_GH_ERRORS` (connection failures, 502/503/504), fatal otherwise. `gh` exits 1 for a network blip exactly as it does for "no such PR", so the message is the only thing that separates them; retrying a real error would just stall.
- **`run_gh` raises `GhError`, it does not `sys.exit`.** This was the single biggest source of unreliability. `sys.exit(1)` raises `SystemExit` in a `ThreadPoolExecutor` worker, which kills only that thread — so its repo's remaining PRs got no `outcome=` line, and `main` re-raised at `future.result()` after every other repo finished, so the end-of-run tally never printed. It fired twice in two observed runs, once on a network blip and once on `This branch can't be rebased`, and `tee` masked the exit code as 0 both times. `process_repository` now catches per PR: `GhError` becomes `error-gh`, anything else becomes `error-unexpected` with a traceback to stderr. The blind `except Exception` is deliberate and carries a `noqa` — nothing may escape and take the tally with it.
- **Ordinary GitHub refusals are outcomes, not crashes** (`MERGE_REFUSALS`). `This branch can't be rebased` describes the PR, not a failure of ours: it is what a repo with `required_linear_history` returns for a branch GitHub will not rebase, on a PR that is otherwise `CLEAN` with every check green. Add to the dict rather than letting a known refusal reach `GhError`.
- **`not-rebasable` means both sides moved the same file, and it never heals on its own.** Diagnosed on `angular-playground#339`: the PR bumps `package-lock.json`, and `main` had changed `package-lock.json` in the 5 commits it was behind. Replaying the commit conflicts, and `required_linear_history` rules out a merge — so `mergeStateStatus` reads `CLEAN` while the merge is impossible. Nothing brings it back, because `rebase_when_behind` only runs for PRs labelled `github_actions`/`github-actions` and this one is labelled `npm`; left alone it falls further behind every day and is refused identically forever. `request_branch_regeneration` asks the bot to rebuild the branch and returns `not-rebasable-regenerating`, leaving the result for the next run to verify from scratch.
- **Regenerating is not the same as rebasing.** Only the bot can resolve a lockfile conflict, by re-resolving dependencies against the new base. `gh pr update-branch --rebase` would replay the same conflicting commit, and without `--rebase` it writes a merge commit that `required_linear_history` then rejects. For renovate the rebase-check box does a full rebuild; for dependabot the comment must be `@dependabot recreate`, not `rebase`. **The dependabot path is unexercised** — every stuck PR observed so far has been renovate's.
- **Retries apply even when `check=False`.** `run_gh` returns stdout either way, so an unretried blip would silently read as an empty response rather than an error.
- **Stale CodeQL workaround** (`rerun_stale_codeql` + `codeql_settled`/`codeql_run_ids`/`rerun_codeql`): GitHub sometimes never registers a green CodeQL run with the `code_scanning` ruleset, so the PR can never merge. When every CodeQL-related check (`"codeql"` in `workflowName`/`name`) has been `COMPLETED`/`SUCCESS` for ≥60s, the CodeQL Analysis workflow run (id parsed from the check `detailsUrl`) is re-run **once** via `gh run rerun`, which re-uploads SARIF and unsticks the rule. `rerun_codeql` uses `check=False` so a non-re-runnable (e.g. expired) run doesn't terminate the process.
- **The re-run must happen in `merge_pr`, not just `wait_for_clean`.** It lived only in `wait_for_clean`'s `BLOCKED` branch at first, and in a full one-hour run over ~95 PRs it fired exactly zero times while 13 PRs ended in `rule-eval-timeout`. `mergeStateStatus` reads `CLEAN`, so `wait_for_clean` returns immediately and the stale rule only surfaces as the merge error `Repository rule violations found / Code scanning is waiting for results from CodeQL`. Without a re-run there, `merge_pr` just burns all 20 attempts. `wait_for_clean` keeps its copy for the genuinely-`BLOCKED` case; both go through `rerun_stale_codeql`, which returns whether it actually fired — setting the once-only flag without checking that return would spend the single re-run on nothing.
- **`merge_pr` re-reads the PR before re-running CodeQL**, both to get fresh `completedAt` timestamps and to re-check `headRefOid`: the merge retry loop can run for minutes, and a re-run plus a fresh CI wait extends that, so a commit landing in the window must report `head-changed` rather than inherit `verify_pr`'s verdict.
- **The 60s threshold is a compromise, not a measurement.** Too low and a normal registration lag triggers a pointless re-run (~3 min of CI); too high and the 5-minute merge budget expires first. In practice a stuck PR's CodeQL is minutes-to-hours old by the time we try to merge, so the re-run fires on the first refused attempt and the retry loop never gets past 1/20 — the threshold only matters for a PR whose CodeQL finished seconds ago.

## Provenance checks

`verify_pr` runs immediately before the merge and refuses anything it cannot vouch for. It is deliberately narrow: it guards against someone with write access pushing to a bot branch, not against a compromised dependency itself.

- **`gh` reports bot logins two different ways.** `gh pr view --json author` gives the app slug (`app/dependabot`), the REST commits API gives the bot user (`dependabot[bot]`). Hence the separate `BOT_AUTHORS` and `BOT_COMMIT_LOGINS`. Getting this wrong is quiet: `is_dependabot` silently sends every dependabot PR down the renovate path.
- **The commit author login is forgeable; the signature is not.** Anyone with write access can push a commit with dependabot's author email, and GitHub resolves `author.login` from that email. So `commit_provenance_problem` requires each commit to be *both* authored by a bot *and* signed (`verification.verified`), because genuine bot commits are created through the API and signed by GitHub's web-flow key.
- **`update_branch` is the one legitimate unsigned commit.** Rebasing via `gh pr update-branch` re-creates the commit with the running user as committer and no signature — so an unsigned commit is accepted only when its committer is `gh_login()`. This exception is why the check cannot simply be `verified == true`; a sweep of 80 merged bot PRs found exactly one commit in this shape, and it was ours.
- **Workflow diffs may only move `uses:` lines and version pins** (`workflow_diff_problem`, `ALLOWED_WORKFLOW_LINES`). A same-repo PR runs the workflows from its own branch with the repository's secrets, making a workflow edit the highest-value thing to smuggle into a bot PR. The original `uses:`-only rule was too tight: a renovate PR bumping `node-version: 22` was refused as `unsafe-workflow-edited` — the earlier claim that a 25-PR sample showed no false positives did not hold up. `VERSION_PIN_LINE` also allows `<something>version:` scalars, and its value charset excludes `$`, `{`, `}` and mid-value quotes, so `node-version: ${{ secrets.X }}` still fails to match and is still refused. A workflow file with no `patch` in the API response (renamed, or diff too large to inline) is refused rather than skipped.
- **Changed file paths are otherwise not allowlisted.** Real bot PRs in this org touch `.rb` Homebrew formulae, `pyproject.toml`, `Dockerfile.noarg`, lockfiles and more; an allowlist would be a maintenance treadmill with a high false-positive rate. The strictness budget is spent on workflows instead.
- **An empty `statusCheckRollup` is refused** (`no-checks`). `check_ci_status` calls `all()` over the rollup, so no checks reads as "CI passed" — every repo in the org currently reports 9+ checks, so this only fires on something genuinely wrong.
- **`merge_pr` pins `--match-head-commit`** to the head `verify_pr` inspected. `wait_for_clean` and the merge retry loop can each burn five minutes, and without the pin a commit landing in that window would inherit the earlier verification. A mismatch reports `head-changed` and is left for the next run; the error is matched on `"Head branch was modified"`, distinct from the transient `"Base branch was modified"` flake. If GitHub ever reworded it, the unrecognised-error path is `sys.exit(1)` — noisy, but still fail-closed.

## mergeStateStatus values

- `CLEAN` — ready to merge
- `HAS_HOOKS` — ready to merge (deployment hooks run post-merge)
- `BLOCKED` — blocked by ruleset or branch protection (e.g. code scanning pending)
- `BEHIND` — branch is behind base
- `DIRTY` — merge conflicts
- `DRAFT` — draft PR
- `UNSTABLE` — failing non-required checks
