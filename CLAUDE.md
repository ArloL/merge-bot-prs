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
- If the PR has a `github_actions`/`github-actions` label and is behind the base branch, trigger a rebase and wait for it to complete.
- Wait for all CI checks to complete.
- If CI failed, attempt a rebase (in case the failure was due to being out of date) and re-check.
- If CI passed, wait for `mergeStateStatus` to become `CLEAN` or `HAS_HOOKS` (handles rulesets like code scanning that finish after CI), then merge with `--rebase`.

Poll interval for all wait loops is 15 seconds.

## Key design decisions

- **`mergeStateStatus` is checked before merging** — not just CI. GitHub rulesets (e.g. required code scanning) can keep a PR `BLOCKED` even after all CI checks pass. The `wait_for_clean` function polls until the status is mergeable.
- **Rebase detection differs by bot**: dependabot is triggered via `@dependabot rebase` comment; renovate via checking `- [x] <!-- rebase-check -->` in the PR body.
- **`is_behind` uses the compare API** directly (`repos/{repo}/compare/{base}...{head}`) rather than trusting `mergeStateStatus`, because `mergeStateStatus` is unreliable when branch protection rules are absent.
- **`run_gh` calls `sys.exit(1)` on any error** — a single unexpected failure will terminate the whole process.

## mergeStateStatus values

- `CLEAN` — ready to merge
- `HAS_HOOKS` — ready to merge (deployment hooks run post-merge)
- `BLOCKED` — blocked by ruleset or branch protection (e.g. code scanning pending)
- `BEHIND` — branch is behind base
- `DIRTY` — merge conflicts
- `DRAFT` — draft PR
- `UNSTABLE` — failing non-required checks
