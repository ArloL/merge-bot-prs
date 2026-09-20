# merge bot prs

Script that auto-merges all open dependabot/renovate PRs across the `arlol` GitHub organization.

## Archived repositories are out of scope — always filter them out

**Any query for "remaining" or "stuck" bot PRs must exclude archived repositories, or the answer will be wrong.** An archived repo is read-only: its PRs can never be merged, by anyone, and no tooling can change that. `get_all_prs` already excludes them, so they never appear in a run.

They are also not accumulating. Those repos stopped receiving new PRs when they were archived; what is left is old and frozen. Nothing needs to be done about it, and reporting the raw count as a problem is a false alarm.

The trap is that `gh search prs` does **not** filter them:

```
gh search prs --owner ArloL --state open --author app/dependabot    # includes archived repos
gh api repos/ArloL/<repo> --jq .archived                            # check before counting
```

Measured 2026-08-20, right after a run that merged everything mergeable: 37 open bot PRs org-wide, of which **35 were in 8 archived repos**. Taking that number at face value would suggest the script had failed, when in fact only 2 PRs were in scope and both were correctly refused for real reasons.

## Running

```
uv run merge_bot_prs.py
uv run merge_bot_prs.py --debug       # print mergeStateStatus and CI state per PR
uv run merge_bot_prs.py --count 5     # process at most N PRs in total (useful for testing)
```

Requires `gh` CLI authenticated with sufficient org-level access.

## How it works

1. Searches for all open PRs authored by `app/dependabot` or `app/renovate` across the org (up to 1000 each).
2. Groups PRs by repository, then processes each repo in parallel using a thread pool. The search repeats every `SEARCH_INTERVAL` (300s) for as long as the run lasts, so PRs opened mid-run are picked up without waiting for anything else to finish.
3. Within each repo, PRs are processed serially.

Per-PR logic (`process_pr`):
- If `mergeStateStatus` is `DIRTY`, ask the bot to regenerate the branch (`dirty-regenerating`); skip if `DRAFT`.
- If the PR has a `github_actions`/`github-actions` label and is behind the base branch, bring the branch up to date (see below — dependabot is rebased directly, renovate is asked to rebase itself).
- Wait for all CI checks to complete.
- If CI failed, attempt a rebase (in case the failure was due to being out of date) and re-check.
- If CI passed, wait for `mergeStateStatus` to become `CLEAN` or `HAS_HOOKS` (handles rulesets like code scanning that finish after CI). If code scanning blocks while the branch is behind base, its merge ref is stale and no amount of waiting helps — the branch is refreshed instead (see below).
- Run `verify_pr` (see Provenance checks below) and merge with `--rebase` only if it returns nothing. This tail runs at most twice: a stale merge ref forces a new head, which invalidates the verification.

Poll interval for all wait loops is 15 seconds.

## Output

Every line is `HH:MM:SS [repo#pr] message`, written through a lock and flushed immediately — repos are processed in parallel, so the timestamp is what makes an interleaved log readable afterwards.

`process_pr` returns an outcome for **every** exit path, logged as `outcome=<x> in <n>s`, and the run ends with a tally plus a list of everything that did not merge. Outcomes: `merged`, `ci-failed`, `dirty`, `draft`, `merged`/`closed` (resolved elsewhere mid-run), `not-mergeable-<state>`, `not-rebasable`, `not-rebasable-regenerating`, `dirty-regenerating`, `rebase-stalled`, `stale-merge-ref`, `rule-eval-timeout`, `head-changed`, `error-gh`/`error-unexpected`, and `unsafe-<reason>` for each provenance refusal.

The process exits 1 if any PR ended in an `error-` outcome, so an unsupervised run alerts without anyone reading the log.

Refusal reasons are deliberately short kebab-case strings so the end-of-run `Counter` tally stays readable; the specifics (which commit, which file, which line) go out as a `refusing: ...` log line at the moment of refusal.

Useful detail that is on by default: the failing check names behind `ci_passing=False`, the pending check names while waiting on CI, `old -> new` head SHAs after `update_branch`, and attempt counters on the bounded wait loops. `--debug` adds per-PR author/labels/check-count, `ahead`/`behind` counts, and a per-repo breakdown of what the search found.

## Key design decisions

- **`mergeStateStatus` is checked before merging** — not just CI. GitHub rulesets (e.g. required code scanning) can keep a PR `BLOCKED` even after all CI checks pass. The `wait_for_clean` function polls until the status is mergeable.
- **Getting a behind branch up to date differs by bot**:
  - **dependabot** — the branch is rebased directly via `gh pr update-branch --rebase` (`update_branch`). Commenting `@dependabot rebase` does *not* work: dependabot only rebases when the files it manages would change, so a PR sitting behind on commits that touch unrelated files gets an immediate "Looks like this PR is already up-to-date with main!" and never moves. Re-commenting produces the same reply, so `wait_for_rebase` would spin forever — hence it no longer waits on `is_behind` for dependabot PRs.
  - **renovate** — still triggered by checking `- [x] <!-- rebase-check -->` in the PR body. Renovate's `rebaseCheck` honours the box unconditionally, so it has none of dependabot's refusal behaviour. It is deliberately *not* switched to `update-branch`: that rewrites the commit's committer to the running user, and renovate's `isBranchModified` inspects both author and committer email, so the PR would be treated as human-edited and abandoned.
- **`gh pr update-branch` only queues the rebase, so `update_branch` polls until the head actually moves** and returns the refreshed PR. Reading the head straight after the call gets the *old* SHA — every rebase in an observed run logged `branch updated 2299149e -> 2299149e` while the real new head was `3c7decb9`. A stale head hands the caller the previous head's rollup, which is green because that CI ran to completion, so a commit nobody tested reads as "CI passed". Do not go back to a single post-call read.
- **`gh pr view` lags REST by minutes, so `update_branch` polls for 5 of them.** GraphQL — what `gh pr view --json headRefOid` reads — serves a replica that can sit minutes behind. On `template-graal#58` the rebase commit's committer date was 2s after the `update-branch` call, while `pr view` still returned the old SHA 60s later. The old 12×5s budget therefore gave up on a rebase that had already landed. The give-up line now also logs `head_sha(pr)`, the REST head, which separates the two cases: a rebase GitHub never performed, versus one `pr view` cannot see yet.
- **A stalled rebase returns `None` all the way out, it never falls through** (`rebase-stalled`). When `update_branch` gives up, `rebase_when_behind` used to return the *original* PR, and `process_pr` then read `check_ci_status` off it — the old head's rollup, which is green because that CI ran to completion against a base that has since moved. #58 was merged this way on 2026-09-01; branch protection happened to hold the merge until the real head's CI finished, so the outcome was fine, but nothing in the script was doing that work. In a repo without required status checks the same path merges untested code. `rebase_when_behind` now returns `None`, and every caller fails closed: `process_pr` reports `rebase-stalled` before the CI gate, the post-CI-failure call reports `ci-failed`, and `refresh_stale_merge_ref`'s callers keep waiting and end at `not-mergeable-blocked`.
- **After the head moves, the script still sleeps one poll interval** before checking CI. That covers a different gap: the rollup for the new head is empty until GitHub registers the workflows, and an empty rollup also reads as "CI passed" in `check_ci_status`.
- **`is_behind` uses the compare API** directly (`repos/{repo}/compare/{base}...{head}`) rather than trusting `mergeStateStatus`, because `mergeStateStatus` is unreliable when branch protection rules are absent.
- **`--count` is a whole-run budget, not a batch size.** `main` re-searches the org on a timer and keeps going until no unprocessed PRs remain, so slicing the per-round list capped the batch and then looped anyway — `--count 20` drained the org 20 at a time. It is now `max(0, count - len(processed))`; the `max` matters, because a negative slice end trims from the tail instead of yielding nothing. This went unnoticed for two runs because both died before reaching a second round.
- **The org is re-searched on a timer, not once per batch** (`SEARCH_INTERVAL`). `main` used to submit one worker per repo and then block on `as_completed` until every one finished, so a batch lasted as long as its slowest repo and PRs opened meanwhile sat untouched. Measured 2026-09-01: `drifty` held a worker for ~4h while every other repo was idle and done. `main` now loops on `wait(..., FIRST_COMPLETED)`, re-searching every 300s and submitting workers for repos that have unprocessed PRs and nothing in flight.
- **A repo with a worker in flight is skipped, not topped up** (`active`). PRs within a repo must stay serial — merging one puts the next behind — so new PRs found for a busy repo are left unprocessed and collected by a later round. Feeding an in-flight worker would need a thread-safe per-repo queue, and the payoff is only that a repo starts its next PR sooner.
- **Do not search on every worker completion.** `wait` returns on the first repo to finish, so without the `searched` floor a burst of short repos searches once each — two `gh search prs` calls apiece, against a 30/min search limit whose 403 is *not* in `TRANSIENT_GH_ERRORS`, so it would be fatal to the whole run rather than retried.
- **`run_gh` retries transient errors, then calls `sys.exit(1)`** — 3 retries with 2/4/8s backoff when the stderr matches `TRANSIENT_GH_ERRORS` (connection failures, 502/503/504), fatal otherwise. `gh` exits 1 for a network blip exactly as it does for "no such PR", so the message is the only thing that separates them; retrying a real error would just stall.
- **`run_gh` raises `GhError`, it does not `sys.exit`.** This was the single biggest source of unreliability. `sys.exit(1)` raises `SystemExit` in a `ThreadPoolExecutor` worker, which kills only that thread — so its repo's remaining PRs got no `outcome=` line, and `main` re-raised at `future.result()` after every other repo finished, so the end-of-run tally never printed. It fired twice in two observed runs, once on a network blip and once on `This branch can't be rebased`, and `tee` masked the exit code as 0 both times. `process_repository` now catches per PR: `GhError` becomes `error-gh`, anything else becomes `error-unexpected` with a traceback to stderr. The blind `except Exception` is deliberate and carries a `noqa` — nothing may escape and take the tally with it.
- **Ordinary GitHub refusals are outcomes, not crashes** (`MERGE_REFUSALS`). `This branch can't be rebased` describes the PR, not a failure of ours: it is what a repo with `required_linear_history` returns for a branch GitHub will not rebase, on a PR that is otherwise `CLEAN` with every check green. Add to the dict rather than letting a known refusal reach `GhError`.
- **`not-rebasable` means both sides moved the same file, and it never heals on its own.** Diagnosed on `angular-playground#339`: the PR bumps `package-lock.json`, and `main` had changed `package-lock.json` in the 5 commits it was behind. Replaying the commit conflicts, and `required_linear_history` rules out a merge — so `mergeStateStatus` reads `CLEAN` while the merge is impossible. Nothing brings it back, because `rebase_when_behind` only runs for PRs labelled `github_actions`/`github-actions` and this one is labelled `npm`; left alone it falls further behind every day and is refused identically forever. `request_branch_regeneration` asks the bot to rebuild the branch and returns `not-rebasable-regenerating`, leaving the result for the next run to verify from scratch.
- **`DIRTY` gets the same treatment as `not-rebasable`.** Skipping a conflicted bot PR leaves it to rot: `angular-playground#322` sat `DIRTY` for 19 days, skipped once per run, and carries two dead `@dependabot rebase` comments from someone trying by hand. Regenerating resolved the conflict in 60 seconds — and revealed the actual blocker, a peer dependency conflict (`typescript@7` against `@angular/build`'s `>=6.0 <6.1`), which is correctly reported as `ci-failed` rather than merged. Surfacing a real failure is the point; a skipped PR looks identical to one nobody has looked at.
- **A recreate is acknowledged by the branch moving, not by a reply** (`recreate_already_requested`). Dependabot does not answer a `@dependabot recreate`, so "our request is still the last comment" latches true forever once asked. Read that way the guard became a permanent stall: #322 was reported `dirty` on every run for 12 days, even though the 2026-08-20 request *was* honoured and the branch rebuilt on 2026-08-31, then re-conflicted. The check is now `head_committed_at(pr) <= <last request>` — a newer head means the bot acted and may be asked again. Keep the guard itself: without it the script adds a comment per run, which is the mess #322 already carries two dead `@dependabot rebase` comments from. Renovate has its own guard in `rebase_already_triggered`.
- **Regenerating is not the same as rebasing.** Only the bot can resolve a lockfile conflict, by re-resolving dependencies against the new base. `gh pr update-branch --rebase` would replay the same conflicting commit, and without `--rebase` it writes a merge commit that `required_linear_history` then rejects. For renovate the rebase-check box does a full rebuild; for dependabot the comment must be `@dependabot recreate`, not `rebase`. **The dependabot path is unexercised** — every stuck PR observed so far has been renovate's.
- **Retries apply even when `check=False`.** `run_gh` returns stdout either way, so an unretried blip would silently read as an empty response rather than an error.
- **Code scanning blocks because base moved, and only a new head commit fixes it** (`refresh_stale_merge_ref`). CodeQL analyses `refs/pull/N/merge` — the PR head merged with base *as of the run*. When base moves, GitHub recomputes that ref to a new SHA, the `code_scanning` ruleset finds no analysis for it, and the PR is unmergeable forever. Waiting cannot resolve it: the analysis it is waiting for is for a merge commit that will never be built. So the remedy is to move the head — `refresh_stale_merge_ref` delegates to `rebase_when_behind`, which produces a merge ref current enough for a fresh CodeQL run to key its results to.
- **`gh run rerun` cannot fix this, and the earlier workaround that used it was wrong.** A re-run re-uses the `GITHUB_SHA` the run recorded, so it re-uploads SARIF for the stale merge commit — the one nobody is asking about. The re-runs that appeared to unstick PRs were most likely ones whose base had not actually moved. `codeql_settled`/`codeql_run_ids`/`rerun_codeql`/`rerun_stale_codeql` are deleted; do not reintroduce a re-run path.
- **The signal is "code scanning blocking *and* behind base", not "CodeQL green for ≥60s".** The old age threshold was a proxy for base having moved, and it also matched perfectly healthy PRs. Being behind base *is* the cause, so `compare_counts` is the check. When the branch is **not** behind, the merge ref matches what CodeQL analysed and the block is genuine registration lag — keep waiting; that is the one case the bounded poll loop is right for.
- **`merge_pr` must not refresh the branch itself; it returns `stale-merge-ref` and lets `process_pr` do it.** `--match-head-commit` pins the merge to the head `verify_pr` vouched for, and refreshing deliberately moves that head. `process_pr` wraps `wait_for_clean` → `verify_pr` → `merge_pr` in a **two-pass** loop: pass two re-verifies the new head from scratch. If base moves again during our own CI, pass two is stale too and `stale-merge-ref` is the reported outcome — correct, because the next run starts over. `wait_for_clean` refreshes in place instead, once, since nothing is verified yet at that point.
- **`caffeinate -w <our pid>` is spawned at startup** (`keep_awake`), not wrapped around the script. A run waits on CI for hours and a sleeping Mac stops it mid-PR; tying the helper to the pid means it dies with us on every exit path, so no `finally` has to reach it.
- **Bail out of `merge_pr` immediately on `stale-merge-ref`, do not spend the retry budget.** The 20×15s loop exists for rules GitHub is still evaluating; a stale merge ref is not pending, it is absent. In an observed one-hour run 13 PRs reached `rule-eval-timeout` this way — five wasted minutes each.

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

# clear bot notifications

`clear_bot_notifications.py` empties the GitHub notification inbox of things
that never need a human: releases from the org's own repos, and dependabot or
renovate PRs that have already been merged. It imports `run_gh`, `log`,
`paginated` and `GhError` from `merge_bot_prs.py`.

```
uv run clear_bot_notifications.py             # unread only (the normal run)
uv run clear_bot_notifications.py --dry-run
uv run clear_bot_notifications.py --all       # sweep read notifications too
```

## REST cannot see "done", so unread is the only usable scope

`DELETE /notifications/threads/{id}` marks a thread done; `PATCH` only marks it
read. But **nothing reads the done bit back**. A thread marked done and one
nobody has touched are identical over REST — both `unread: false`,
`last_read_at: null` — and `?all=true` keeps returning done threads
indefinitely. The web inbox's Unread/Read/Done/Saved states live in a store the
REST API does not expose.

Measured 2026-09-20, right after 344 threads were marked done by hand — the
web UI and REST disagree completely about the same account:

| | web UI | REST |
| --- | --- | --- |
| inbox | 57 (56 unread, 1 read) | no equivalent |
| done | 521, a real folder | invisible |
| filters | `is:done`, `is:unread`, `is:saved` | `all=true`/`all=false` |

`?all=true` returned 561 that morning: every thread with recent activity,
done or not. A `--all` dry run duly offered to clear 361 it had just cleared.

Three consequences:

- **The default scope is unread.** Marking done also clears unread, so that is
  what makes a second run cheap and idempotent. `--all` is for a first
  catch-up sweep and re-clears everything, every time it is used.
- **The unread default misses read-but-not-done threads** — one of the 57
  above. They are only reachable via `--all`, and only by re-clearing the
  other 560 along the way.
- **Verify with the unread count, never the `?all=true` count.** The latter
  looks unchanged after a successful run and reads as a failure.

There is no better API to switch to. GraphQL has no notifications schema at
all — introspection turns up only org email restrictions and team settings —
and REST silently ignores the web UI's `query` parameter: `?query=is:done`,
`?query=is:unread` and `?query=garbage` all return the identical 578 threads.
Done state is readable only from the web UI's own HTML.

## Scope decisions

- **Releases are filtered to the org, merged bot PRs are not.** A release from
  someone else's project is something the user chose to watch; a merged bot PR
  is finished business wherever it lives (the inbox carries them from
  `haeger-sales-platform` too).
- **Both bots count as merged-bot-pr**, via `BOT_COMMIT_LOGINS`. The REST
  commits API spelling (`renovate[bot]`) is the right one here, not the app
  slug `app/renovate` that `gh pr view --json author` returns.
