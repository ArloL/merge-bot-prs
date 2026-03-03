# merge bot prs

This just makes my life easier by going through my organization and merges all
dependency updates the way I like it.

# Quickstart

```
uv run merge_bot_prs.py
```

# What it does

1. Get all non-archived repositories in the `arlol` GitHub organization.
2. For each repository (in parallel), fetches all open pull requests authored by `app/dependabot` or `app/renovate`.
3. For each PR (serially per repo):
   - If a rebase is already in progress (dependabot body text or renovate's rebase checkbox is checked), waits until the rebase finishes and the PR is up to date.
   - If the PR has the `github_actions` or `github-actions` label and is behind the base branch:
     - For dependabot: posts an `@dependabot rebase` comment (unless one was already posted after the last commit, in which case just waits).
     - For renovate: checks the rebase checkbox in the PR body (unless already checked, in which case just waits).
     - Polls every 15 seconds until the PR is no longer rebasing and is up to date.
   - Once the PR is up to date, checks CI status:
     - If CI is still running, polls every 15 seconds until all checks complete.
     - If CI passed (success, neutral, or skipped), merges the PR with rebase strategy.
     - If CI failed, skips the PR.
