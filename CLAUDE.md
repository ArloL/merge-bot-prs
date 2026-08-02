# merge bot prs

Script that auto-merges all open dependabot/renovate PRs across the `arlol` GitHub organization.

## Running

```
uv run merge_bot_prs.py
uv run merge_bot_prs.py --debug       # print mergeStateStatus and CI state per PR
uv run merge_bot_prs.py --count 5     # process at most N PRs (useful for testing)
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
- If CI passed, wait for `mergeStateStatus` to become `CLEAN` or `HAS_HOOKS` (handles rulesets like code scanning that finish after CI), then merge with `--rebase`. While waiting, if the PR is stuck `BLOCKED` but CodeQL has been green for ≥120s, re-run CodeQL once to work around GitHub's stale code-scanning bug (see below).

Poll interval for all wait loops is 15 seconds.

## Key design decisions

- **`mergeStateStatus` is checked before merging** — not just CI. GitHub rulesets (e.g. required code scanning) can keep a PR `BLOCKED` even after all CI checks pass. The `wait_for_clean` function polls until the status is mergeable.
- **Getting a behind branch up to date differs by bot**:
  - **dependabot** — the branch is rebased directly via `gh pr update-branch --rebase` (`update_branch`). Commenting `@dependabot rebase` does *not* work: dependabot only rebases when the files it manages would change, so a PR sitting behind on commits that touch unrelated files gets an immediate "Looks like this PR is already up-to-date with main!" and never moves. Re-commenting produces the same reply, so `wait_for_rebase` would spin forever — hence it no longer waits on `is_behind` for dependabot PRs.
  - **renovate** — still triggered by checking `- [x] <!-- rebase-check -->` in the PR body. Renovate's `rebaseCheck` honours the box unconditionally, so it has none of dependabot's refusal behaviour. It is deliberately *not* switched to `update-branch`: that rewrites the commit's committer to the running user, and renovate's `isBranchModified` inspects both author and committer email, so the PR would be treated as human-edited and abandoned.
- **After `update_branch`, the script sleeps one poll interval** before checking CI. The status check rollup for the new head is empty until GitHub registers the workflows, and an empty rollup reads as "CI passed" in `check_ci_status`.
- **`is_behind` uses the compare API** directly (`repos/{repo}/compare/{base}...{head}`) rather than trusting `mergeStateStatus`, because `mergeStateStatus` is unreliable when branch protection rules are absent.
- **`run_gh` calls `sys.exit(1)` on any error** — a single unexpected failure will terminate the whole process.
- **Stale CodeQL workaround** (`wait_for_clean` + `codeql_settled`/`codeql_run_ids`/`rerun_codeql`): GitHub sometimes leaves a PR `BLOCKED` forever even though the CodeQL workflow ran and the `CodeQL` status check is green — the `code_scanning` ruleset never registers the results. When the PR is `BLOCKED` and every CodeQL-related check (`"codeql"` in `workflowName`/`name`) has been `COMPLETED`/`SUCCESS` for ≥120s, the CodeQL Analysis workflow run (id parsed from the check `detailsUrl`) is re-run **once** via `gh run rerun`, which re-uploads SARIF and unsticks the rule. If it stays `BLOCKED` after that, the PR is skipped and retried on the next pass. `rerun_codeql` uses `check=False` so a non-re-runnable (e.g. expired) run doesn't terminate the process.

## mergeStateStatus values

- `CLEAN` — ready to merge
- `HAS_HOOKS` — ready to merge (deployment hooks run post-merge)
- `BLOCKED` — blocked by ruleset or branch protection (e.g. code scanning pending)
- `BEHIND` — branch is behind base
- `DIRTY` — merge conflicts
- `DRAFT` — draft PR
- `UNSTABLE` — failing non-required checks
