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
     - For dependabot: rebases the branch directly with `gh pr update-branch --rebase` (asking dependabot to rebase itself does not work when the PR is only behind on unrelated commits).
     - For renovate: checks the rebase checkbox in the PR body (unless already checked, in which case just waits).
     - Polls every 15 seconds until the PR is no longer rebasing and is up to date.
   - Once the PR is up to date, checks CI status:
     - If CI is still running, polls every 15 seconds until all checks complete.
     - If CI failed, skips the PR.
   - Before merging, checks that the PR really is the bot's work: every commit authored by the bot and signed by GitHub, the branch not a fork, no workflow file changed beyond its `uses:` lines, and at least one status check actually ran. Anything else is skipped and reported rather than merged.
   - Merges with rebase strategy, pinned to the exact commit that was verified.
