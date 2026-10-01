#!/usr/bin/env python3
"""
Processes all open pull requests in the arlol organization made by dependabot
or renovate, and tries to do the right thing to merge them.
"""

import argparse
import functools
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime

# New bot PRs appear while a run is going -- a repo can hold a worker for hours
# -- so the org is searched again on this interval rather than once per batch.
SEARCH_INTERVAL = 300

# Repos are processed in parallel, so lines from different PRs interleave.
# The timestamp is what makes the resulting log readable after the fact, and
# the lock keeps concurrent writes from splicing into each other.
_log_lock = threading.Lock()


def log(label, message, stream=sys.stdout):
    with _log_lock:
        print(f"{datetime.now().astimezone():%H:%M:%S} {label} {message}",
              file=stream, flush=True)


# Blips talking to api.github.com, not answers about the PR. Matched on the
# message because gh exits 1 for these exactly as it does for a real failure,
# and retrying a genuine error (no such PR, no permission) would only stall.
TRANSIENT_GH_ERRORS = (
    "error connecting to",
    "connection reset by peer",
    "i/o timeout",
    "tls handshake timeout",
    "unexpected eof",
    "http 502",
    "http 503",
    "http 504",
)


class GhError(Exception):
    """A gh call failed in a way we cannot interpret.

    Raised rather than exited: sys.exit inside a ThreadPoolExecutor worker
    raises SystemExit in that thread alone, which silently drops the rest of
    the repository's PRs and, once main reaches future.result(), suppresses the
    end-of-run tally. An unsupervised run has nobody watching the console.
    """


def keep_awake():
    """Hold off idle sleep for as long as this process lives.

    A run waits on CI for hours; a sleeping Mac stops it mid-PR. caffeinate -w
    watches our pid and exits on its own, so there is nothing to clean up on
    any of the script's exit paths, including sys.exit from an uncaught GhError.
    """
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
    except OSError as error:
        log("[run]", f"no caffeinate, the mac may sleep mid-run: {error}")


def is_transient_gh_error(stderr):
    lowered = stderr.lower()
    return any(signature in lowered for signature in TRANSIENT_GH_ERRORS)


def run_gh(args, label="", check=True, retries=3, backoff=2):
    for attempt in range(retries + 1):
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,     # the caller's `check` decides, via the branch below
        )
        if result.returncode == 0:
            return result.stdout
        # Worth retrying even when check=False: the caller gets stdout either
        # way, so without this a blip silently reads as an empty response.
        if attempt < retries and is_transient_gh_error(result.stderr):
            delay = backoff * 2 ** attempt
            log(label, f"transient gh error, retrying in {delay}s "
                       f"(attempt {attempt + 1}/{retries}): {result.stderr.strip()}")
            time.sleep(delay)
            continue
        if check:
            log(label, f"Error: {result.stderr.strip()}", stream=sys.stderr)
            raise GhError(result.stderr.strip())
        return result.stdout


def paginated(endpoint, label=""):
    """Every element of a paginated array endpoint, as dicts.

    `--paginate` prints one JSON array per page, which json.loads cannot read,
    and `--slurp` (which would merge them) is rejected alongside `--jq`. Asking
    jq for `.[]` sidesteps both: one compact object per line, pages included.
    """
    output = run_gh(["api", "--paginate", endpoint, "--jq", ".[]"], label=label)
    return [json.loads(line) for line in output.splitlines() if line]


@functools.cache
def gh_login():
    """The account gh is authenticated as."""
    return run_gh(["api", "user", "--jq", ".login"]).strip()


# The two APIs spell the same two accounts differently: `gh pr view --json
# author` reports the app slug, the REST commits API reports the bot user.
DEPENDABOT_AUTHOR = "app/dependabot"
BOT_AUTHORS = (DEPENDABOT_AUTHOR, "app/renovate")
BOT_COMMIT_LOGINS = ("dependabot[bot]", "renovate[bot]")
BOT_BRANCH_PREFIXES = ("dependabot/", "renovate/")


def get_all_prs(organization="arlol", debug=False, quiet=False):
    prs = []
    for author in BOT_AUTHORS:
        prs_output = run_gh([
            "search", "prs",
            "archived:false",
            "--owner", organization,
            "--author", author,
            "--state", "open",
            "--json", "number,repository",
            "--limit", "1000",
        ])
        found = json.loads(prs_output)
        # archived:false is deliberate — archived repos are read-only, so their
        # PRs can be neither merged nor closed. Say so, otherwise the PRs they
        # hide look like PRs the script silently forgot.
        if not quiet:
            log("[search]", f"{author}: {len(found)} open PRs (archived repos excluded)")
        prs.extend(found)
    result = [{
        "number": pr["number"],
        "repository": pr["repository"]["nameWithOwner"],
        "label": f"[{pr['repository']['nameWithOwner']}#{pr['number']}]",
    } for pr in prs]
    if debug and not quiet:
        by_repo = Counter(pr["repository"] for pr in result)
        for repository, count in sorted(by_repo.items()):
            log("[search]", f"  {repository}: {count}")
    return result


def get_pr(pr):
    result = json.loads(run_gh([
        "pr", "view", str(pr["number"]),
        "--repo", pr["repository"],
        "--json", ("author,baseRefName,body,comments,headRefName,headRefOid,"
                   "headRepositoryOwner,isCrossRepository,labels,mergeStateStatus,number,"
                   "reviewDecision,state,statusCheckRollup"),
    ], label=pr["label"]))
    result["repository"] = pr["repository"]
    result["label"] = pr["label"]
    return result


def head_sha(pr):
    """The head SHA per REST, which does not lag the way `gh pr view` does."""
    return run_gh([
        "api", f"repos/{pr['repository']}/pulls/{pr['number']}",
        "--jq", ".head.sha",
    ], label=pr["label"]).strip()


def compare_counts(pr):
    """(ahead_by, behind_by) of the head against the base branch."""
    counts = run_gh([
        "api", f"repos/{pr["repository"]}/compare/{pr['baseRefName']}...{pr['headRefOid']}",
        "--jq", "[.ahead_by, .behind_by] | @tsv",
    ], label=pr["label"]).split()
    return int(counts[0]), int(counts[1])


def is_behind(pr):
    return compare_counts(pr)[1] > 0


def is_open(pr):
    return pr["state"] == "OPEN"


def is_dependabot(pr):
    # Exact, not a substring: `"dependabot" in login` would also accept a human
    # account called something like dependabot-helper.
    return pr["author"]["login"] == DEPENDABOT_AUTHOR


def is_currently_rebasing(pr):
    return (
        "Dependabot is rebasing this PR" in pr["body"]
        or "- [x] <!-- rebase-check -->" in pr["body"]
    )


def update_branch(pr, poll_interval=5, max_attempts=60):
    """Rebase the branch onto its base via GitHub, bypassing the bot entirely.

    Returns the PR with its new head, or None if the branch did not move.
    """
    result = subprocess.run(
        ["gh", "pr", "update-branch", str(pr["number"]),
         "--repo", pr["repository"], "--rebase"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log(pr["label"], f"update-branch failed: {result.stderr.strip()}",
            stream=sys.stderr)
        return None
    # GitHub only queues the rebase, and `gh pr view` reads a GraphQL replica
    # that lags well behind it: on template-graal#58 the rebase commit existed
    # 2s after this call (REST committer date) while `pr view` still served the
    # old SHA a minute later. Returning early hands the caller the *previous*
    # head's rollup -- green, because that CI ran to completion -- so the budget
    # here is minutes, not seconds.
    old_head = pr["headRefOid"]
    for attempt in range(1, max_attempts + 1):
        fresh = get_pr(pr)
        if fresh["headRefOid"] != old_head:
            log(pr["label"], f"branch updated {old_head[:8]} -> "
                             f"{fresh['headRefOid'][:8]} (after {attempt} checks)")
            return fresh
        if not is_open(fresh):
            return fresh
        time.sleep(poll_interval)
    # REST is the side that was current, so it says which of the two this was:
    # a rebase GitHub never performed, or one `pr view` still cannot see.
    log(pr["label"], f"head still {old_head[:8]} "
                     f"{max_attempts * poll_interval}s after update-branch "
                     f"(REST reads {head_sha(pr)[:8]}), giving up")
    return None


def rebase_already_triggered(pr):
    """Returns True if renovate has already been asked to rebase and we should wait."""
    return "- [x] <!-- rebase-check -->" in pr["body"]


def trigger_rebase(pr):
    new_body = pr["body"].replace(
        "- [ ] <!-- rebase-check -->",
        "- [x] <!-- rebase-check -->",
    )
    run_gh(["pr", "edit", str(pr["number"]), "--repo", pr["repository"], "--body", new_body], label=pr["label"])


DEPENDABOT_RECREATE = "@dependabot recreate"


def head_committed_at(pr):
    """When the current head commit was committed."""
    return datetime.fromisoformat(run_gh(
        ["api", f"repos/{pr['repository']}/commits/{pr['headRefOid']}",
         "--jq", ".commit.committer.date"],
        label=pr["label"],
    ).strip())


def recreate_already_requested(pr):
    """True if a recreate we asked for is still outstanding.

    The branch moving is the acknowledgement -- dependabot does *not* reply to
    a recreate, so "our comment is the last one" is true forever once asked.
    #322 was refused as `dirty` on every run for 12 days on that reading: the
    2026-08-20 request was honoured, the branch was rebuilt on 2026-08-31, it
    re-conflicted afterwards, and the guard still suppressed every new request.
    A head newer than the last request means the bot acted and may be asked
    again; anything older means it has not got there yet, and re-commenting
    would just repeat the mess of two dead `@dependabot rebase` comments #322
    already carries.
    """
    requests = [comment for comment in (pr.get("comments") or [])
                if DEPENDABOT_RECREATE in comment["body"]]
    if not requests:
        return False
    return head_committed_at(pr) <= datetime.fromisoformat(requests[-1]["createdAt"])


def request_branch_regeneration(pr):
    """Ask the bot to rebuild the branch against the current base.

    Not the same as bringing a behind branch up to date. GitHub refuses the
    rebase when the PR and its base have both moved the same file -- a lockfile
    bump replayed onto a base whose lockfile has since changed -- and under
    required_linear_history a merge is not an alternative. Only the bot can
    resolve that, by regenerating the lockfile against the new base.

    Returns whether a request was made. Deliberately does not wait: the bot
    takes minutes, and the next run verifies whatever lands from scratch.
    """
    if is_dependabot(pr):
        # recreate, not rebase: rebase replays the same conflicting commit, and
        # dependabot refuses outright when the files it manages are unchanged.
        if recreate_already_requested(pr):
            log(pr["label"], "recreate already requested, leaving for the next run")
            return False
        run_gh(["pr", "comment", str(pr["number"]), "--repo", pr["repository"],
                "--body", DEPENDABOT_RECREATE], label=pr["label"])
        log(pr["label"], "asked dependabot to recreate the branch")
        return True
    if rebase_already_triggered(pr):
        log(pr["label"], "rebase already requested, leaving for the next run")
        return False
    trigger_rebase(pr)
    log(pr["label"], "asked renovate to regenerate the branch")
    return True


WORKFLOW_DIR = ".github/workflows/"
USES_LINE = re.compile(r"^[+-]\s*(?:-\s*)?uses:\s")
# Bots bump toolchain pins inside workflows too -- node-version: 22 and
# friends. Refusing those was a live false positive on a renovate PR. The value
# charset deliberately excludes $ { } and quotes mid-value, so an expression
# like ${{ secrets.X }} still fails to match and is still refused.
VERSION_PIN_LINE = re.compile(
    r"^[+-]\s*(?:-\s*)?[a-z0-9_-]*version:\s*['\"]?[\w.+*-]+['\"]?\s*$",
    re.IGNORECASE,
)
ALLOWED_WORKFLOW_LINES = (USES_LINE, VERSION_PIN_LINE)


def commit_provenance_problem(pr):
    """Reason the commits are not provably the bots', or None.

    A pushed commit can claim any author email, and GitHub resolves
    `author.login` from that email — so the login on its own is forgeable by
    anyone with write access to the repository. What cannot be forged is the
    signature: commits the bots create through the API are signed by GitHub's
    web-flow key. The one legitimate unsigned case is our own update_branch,
    which re-creates the commit with the running user as committer.
    """
    commits = paginated(
        f"repos/{pr['repository']}/pulls/{pr['number']}/commits?per_page=100",
        pr["label"],
    )
    if not commits:
        log(pr["label"], "refusing: no commits")
        return "no-commits"
    for commit in commits:
        sha = commit["sha"][:8]
        author = (commit.get("author") or {}).get("login")
        committer = (commit.get("committer") or {}).get("login")
        if author not in BOT_COMMIT_LOGINS:
            log(pr["label"], f"refusing: commit {sha} authored by {author or '?'}")
            return "foreign-commit"
        if not commit["commit"]["verification"]["verified"] and committer != gh_login():
            log(pr["label"], f"refusing: commit {sha} is unsigned and was committed "
                             f"by {committer or '?'}, not {gh_login()}")
            return "unsigned-commit"
    return None


def workflow_diff_problem(pr):
    """Reason a workflow file change is not a plain action bump, or None.

    A same-repo pull request runs the workflows from its own branch with the
    repository's secrets, so a workflow edit is the most valuable thing to slip
    into a bot PR. Genuine bumps only ever move `uses:` lines or version pins.
    """
    files = paginated(
        f"repos/{pr['repository']}/pulls/{pr['number']}/files?per_page=100",
        pr["label"],
    )
    for file in files:
        if not file["filename"].startswith(WORKFLOW_DIR):
            continue
        patch = file.get("patch")
        if patch is None:
            # No patch means the file was renamed or the diff was too large to
            # inline. Either way we cannot see what changed, so we refuse.
            log(pr["label"], f"refusing: no diff available for {file['filename']} "
                             f"({file.get('status')})")
            return "workflow-diff-unavailable"
        for line in patch.splitlines():
            if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
                continue
            if not any(allowed.match(line) for allowed in ALLOWED_WORKFLOW_LINES):
                log(pr["label"], f"refusing: {file['filename']} changes more than "
                                 f"uses: lines and version pins: {line.strip()}")
                return "workflow-edited"
    return None


def verify_pr(pr):
    """Reason to refuse this PR, or None if it is safe to merge.

    Runs immediately before the merge, so that everything it inspects belongs
    to pr["headRefOid"] — which merge_pr then pins with --match-head-commit.
    """
    author = pr["author"]["login"]
    if author not in BOT_AUTHORS:
        log(pr["label"], f"refusing: author is {author}")
        return "foreign-author"
    if pr["isCrossRepository"]:
        owner = (pr["headRepositoryOwner"] or {}).get("login", "?")
        log(pr["label"], f"refusing: head branch lives in a fork owned by {owner}")
        return "cross-repository"
    if not pr["headRefName"].startswith(BOT_BRANCH_PREFIXES):
        log(pr["label"], f"refusing: unexpected head branch {pr['headRefName']}")
        return "foreign-branch"
    if pr["reviewDecision"] == "CHANGES_REQUESTED":
        log(pr["label"], "refusing: a review requested changes")
        return "changes-requested"
    if not pr["statusCheckRollup"]:
        # check_ci_status calls all() over the rollup, so an empty one reads as
        # "CI passed". Without this a PR whose checks never ran would sail
        # through the CI gate on its way here.
        log(pr["label"], "refusing: no status checks ran at all")
        return "no-checks"
    return commit_provenance_problem(pr) or workflow_diff_problem(pr)


def print_merge_diagnostics(pr):
    """Print why GitHub is refusing to merge: fresh merge state, required
    checks, and the branch protection / ruleset that governs the base branch."""
    fresh = get_pr(pr)
    ahead, behind = compare_counts(fresh)
    log(pr["label"], f"diagnostics: mergeStateStatus={fresh['mergeStateStatus']} "
                     f"state={fresh['state']} head={fresh['headRefOid'][:8]} "
                     f"base={fresh['baseRefName']} ahead={ahead} behind={behind}")
    for check in fresh["statusCheckRollup"]:
        status = check.get("status") or check.get("state")
        conclusion = check.get("conclusion")
        required = check.get("isRequired")
        log(pr["label"], f"diagnostics: check {check_name(check)}: status={status} "
                         f"conclusion={conclusion} required={required}")

    # Classic branch protection (404 when only rulesets are configured).
    protection = run_gh([
        "api", f"repos/{pr['repository']}/branches/{fresh['baseRefName']}/protection",
    ], label=pr["label"], check=False).strip()
    if protection:
        log(pr["label"], f"diagnostics: branch protection: {protection}")

    # Rulesets that apply to the base branch (the modern equivalent).
    rules = run_gh([
        "api", f"repos/{pr['repository']}/rules/branches/{fresh['baseRefName']}",
    ], label=pr["label"], check=False).strip()
    if rules:
        log(pr["label"], f"diagnostics: active rules: {rules}")


# Refusals that describe the pull request rather than a failure of ours. Each
# gets its own outcome so an unsupervised run reports it and moves on.
MERGE_REFUSALS = {
    "This branch can't be rebased": "not-rebasable",
}

# The rule violation GitHub reports when a code_scanning ruleset has no analysis
# for the PR's current merge ref.
CODE_SCANNING_PENDING = "Code scanning is waiting for results"


def merge_pr(pr, poll_interval=15, max_attempts=20, debug=False):
    """Attempt merge, retrying while GitHub re-evaluates rules asynchronously.

    Returns the outcome: "merged", "head-changed" if something landed on the
    branch after verify_pr vouched for it, "stale-merge-ref" if code scanning is
    blocking on a merge ref that base has moved out from under, or
    "rule-eval-timeout" if rule evaluation never settles in time.
    """
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        result = subprocess.run(
            # --match-head-commit binds the merge to the head verify_pr
            # inspected, so a commit that lands while we wait out the rule
            # evaluation below cannot ride in on that verification.
            ["gh", "pr", "merge", str(pr["number"]), "--repo", pr["repository"], "--rebase",
             "--match-head-commit", pr["headRefOid"]],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            if attempt > 1:
                log(pr["label"], f"merge succeeded on attempt {attempt}")
            return "merged"
        if "Head branch was modified" in result.stderr:
            # Retrying would only re-check the same stale SHA. Leave it for the
            # next run, which will verify whatever is on the branch by then.
            log(pr["label"], f"head moved away from {pr['headRefOid'][:8]} while "
                             f"merging, refusing")
            return "head-changed"
        # Both errors mean GitHub is still evaluating async rules (e.g. code
        # scanning) that briefly reported the PR as CLEAN. Keep polling.
        retryable = (
            "Repository rule violations found" in result.stderr
            or "the base branch policy prohibits the merge" in result.stderr
        )
        if not retryable:
            for message, outcome in MERGE_REFUSALS.items():
                if message in result.stderr:
                    log(pr["label"], f"{outcome}: {result.stderr.strip()}")
                    return outcome
            log(pr["label"], f"Error: {result.stderr.strip()}", stream=sys.stderr)
            if debug:
                print_merge_diagnostics(pr)
            raise GhError(result.stderr.strip())
        # This, not BLOCKED in wait_for_clean, is where the stale merge ref
        # usually surfaces: mergeStateStatus reads CLEAN and only the merge call
        # admits code scanning is waiting on an analysis that will never arrive.
        # Bail out rather than spend the budget on a wait that cannot succeed --
        # the head has to move, and only process_pr may move it, because
        # --match-head-commit is pinned to what verify_pr vouched for.
        if CODE_SCANNING_PENDING in result.stderr and compare_counts(pr)[1]:
            log(pr["label"], f"code scanning waiting on a merge ref base has "
                             f"moved past: {result.stderr.strip()}")
            return "stale-merge-ref"
        log(pr["label"], f"rule evaluation pending, waiting {poll_interval}s "
                         f"(attempt {attempt}/{max_attempts}): {result.stderr.strip()}")
        time.sleep(poll_interval)
    return "rule-eval-timeout"


PASSING_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def check_name(check):
    return check.get("name") or check.get("context") or check.get("__typename", "?")


def check_passing(check):
    if "status" in check:
        return check["conclusion"] in PASSING_CONCLUSIONS
    return check.get("state") == "SUCCESS"


def failing_checks(pr):
    """`name=conclusion` for every check that is not passing.

    Without this a PR just reports ci_passing=False and you have to open it in a
    browser to find out which job broke.
    """
    return [
        f"{check_name(c)}={c.get('conclusion') or c.get('state')}"
        for c in pr["statusCheckRollup"]
        if not check_passing(c)
    ]


def pending_checks(pr):
    """Names of checks that have not finished yet."""
    return [
        check_name(c)
        for c in pr["statusCheckRollup"]
        if (c.get("status", "COMPLETED") != "COMPLETED"
            if "status" in c
            else c.get("state") in {"EXPECTED", "PENDING"})
    ]


def check_ci_status(pr):
    """Returns (ci_running, ci_passing) tuple."""
    checks = pr["statusCheckRollup"]
    ci_running = bool(pending_checks(pr))
    ci_passing = all(check_passing(check) for check in checks)
    return ci_running, ci_passing


def wait_for_rebase(pr, poll_interval=15):
    waited = 0
    while True:
        pr = get_pr(pr)
        if not is_open(pr):
            log(pr["label"], f"{pr['state']} while waiting for rebase")
            return pr
        if is_currently_rebasing(pr):
            log(pr["label"], f"rebasing, waiting {poll_interval}s (waited {waited}s)...")
            time.sleep(poll_interval)
            waited += poll_interval
            continue
        # Only renovate is expected to close the gap on its own; a dependabot
        # branch that is still behind is handled by update_branch, and waiting
        # on it here would spin forever.
        if not is_dependabot(pr):
            ahead, behind = compare_counts(pr)
            if behind:
                log(pr["label"], f"still behind by {behind} (ahead {ahead}), "
                                 f"waiting {poll_interval}s (waited {waited}s)...")
                time.sleep(poll_interval)
                waited += poll_interval
                continue
        return pr


def wait_for_known_merge_state(pr, poll_interval=5, max_attempts=12):
    """The PR once GitHub has finished recomputing mergeability.

    Every merge into base resets its open PRs to UNKNOWN, so a repo's second
    PR onwards routinely starts there. Read as-is, a conflicted PR slips past
    the DIRTY check: angular-playground#380 went on to fail update-branch on
    the conflict and was reported ci-failed instead of being regenerated.
    """
    for _ in range(max_attempts):
        if pr["mergeStateStatus"] != "UNKNOWN" or not is_open(pr):
            return pr
        time.sleep(poll_interval)
        pr = get_pr(pr)
    return pr


def wait_for_clean(pr, debug=False, poll_interval=15, max_attempts=20):
    refreshed = False
    attempts = 0
    while attempts < max_attempts:
        pr = get_pr(pr)
        if not is_open(pr):
            return pr
        merge_state = pr["mergeStateStatus"]
        if merge_state in {"CLEAN", "HAS_HOOKS"}:
            return pr
        if merge_state in {"DIRTY", "DRAFT"}:
            log(pr["label"], f"{merge_state} while waiting for clean, skipping")
            return pr
        if merge_state == "BLOCKED" and not refreshed:
            # Nothing is verified yet, so moving the head is free here -- unlike
            # in merge_pr, where verify_pr has already vouched for a SHA.
            fresh = refresh_stale_merge_ref(pr, debug)
            refreshed = True
            if fresh is not None:
                pr = fresh
                if not is_open(pr):
                    return pr
                attempts = 0              # fresh budget for the new merge ref
                continue
        log(pr["label"], f"{merge_state}, waiting {poll_interval}s "
                         f"(attempt {attempts + 1}/{max_attempts})...")
        time.sleep(poll_interval)
        attempts += 1
    log(pr["label"], f"gave up waiting for a mergeable state after "
                     f"{max_attempts} attempts, last state {pr['mergeStateStatus']}")
    return pr


def wait_for_ci(pr, poll_interval=15):
    waited = 0
    while True:
        pr = get_pr(pr)
        if not is_open(pr):
            log(pr["label"], f"{pr['state']} while waiting for CI")
            return pr
        pending = pending_checks(pr)
        if pending:
            log(pr["label"], f"CI still running, waiting {poll_interval}s "
                             f"(waited {waited}s, pending: {', '.join(sorted(pending))})...")
            time.sleep(poll_interval)
            waited += poll_interval
            continue
        if waited:
            log(pr["label"], f"CI finished after {waited}s")
        return pr


def rebase_when_behind(pr, debug=False, poll_interval=15):
    """The PR brought up to date with its base, or None if that did not happen.

    None is fail-closed on purpose: the caller still holds a PR whose rollup
    belongs to the old head, and that rollup is green because that CI ran to
    completion against a base that has since moved.
    """
    ahead, behind = compare_counts(pr)
    if debug:
        log(pr["label"], f"ahead={ahead} behind={behind} base={pr['baseRefName']} "
                         f"head={pr['headRefOid'][:8]}")
    if not behind:
        return pr

    if is_dependabot(pr):
        # `@dependabot rebase` is useless here: dependabot only rebases when the
        # files it manages would change, so a PR sitting behind on unrelated
        # commits gets "already up-to-date" and never moves. Rebase it ourselves.
        log(pr["label"], f"behind by {behind}, rebasing via update-branch")
        fresh = update_branch(pr)
        if fresh is None:
            return None
        pr = fresh
        if not is_open(pr):
            return pr
        # The rollup for the new head is empty until GitHub registers the
        # workflows, and an empty rollup reads as "CI passed".
        time.sleep(poll_interval)
    else:
        if rebase_already_triggered(pr):
            log(pr["label"], f"behind by {behind}, rebase already requested, waiting")
        else:
            trigger_rebase(pr)
            log(pr["label"], f"behind by {behind}, requested rebase via rebase-check box")

        pr = wait_for_rebase(pr)
        if not is_open(pr):
            return pr

    return wait_for_ci(pr)


def refresh_stale_merge_ref(pr, debug=False):
    """Unstick a PR that code scanning refuses to clear, by giving it a new head.

    CodeQL analyses refs/pull/N/merge -- the PR head merged with base *as of the
    run*. When base moves, GitHub recomputes that ref to a new SHA and the
    code_scanning ruleset finds no analysis for it, so it waits forever. Waiting
    cannot help, and neither can `gh run rerun`: a re-run re-uses the SHA the run
    recorded, so it re-uploads SARIF for the merge commit nobody is asking about.
    Only a new head commit produces a merge ref CodeQL can analyse.

    Returns the refreshed PR, or None -- either because the branch is already up
    to date, so code scanning is genuinely still registering and waiting is
    right, or because the refresh did not land. Both callers treat None as "keep
    waiting, then report BLOCKED", which is the safe reading of either.
    """
    _, behind = compare_counts(pr)
    if not behind:
        return None
    log(pr["label"], f"code scanning blocked and behind by {behind}: base moved, "
                     f"so the merge ref has no analysis -- refreshing the branch")
    return rebase_when_behind(pr, debug)


def process_pr(pr, debug=False):
    """Drive one PR as far as it will go. Returns a short outcome string.

    Every exit path returns one, so that no PR can disappear from the run
    without the log saying what became of it.
    """
    if debug:
        log(pr["label"], f"mergeStateStatus={pr['mergeStateStatus']} "
                         f"author={pr['author']['login']} "
                         f"labels={sorted(l['name'] for l in pr['labels'])} "
                         f"checks={len(pr['statusCheckRollup'])}")

    if is_currently_rebasing(pr):
        log(pr["label"], "bot is rebasing, waiting...")
        pr = wait_for_rebase(pr)
        if not is_open(pr):
            return pr["state"].lower()

    pr = wait_for_known_merge_state(pr)
    merge_state = pr["mergeStateStatus"]
    if merge_state == "DIRTY" and request_branch_regeneration(pr):
        # Same failure shape as not-rebasable: only the bot can resolve the
        # conflict, and skipping leaves the PR to rot. #322 sat DIRTY for 19
        # days being skipped once per run.
        return "dirty-regenerating"
    if merge_state in {"DIRTY", "DRAFT"}:
        return merge_state.lower()

    label_names = {label["name"] for label in pr["labels"]}
    if label_names & {"github_actions", "github-actions"}:
        fresh = rebase_when_behind(pr, debug)
        if fresh is None:
            # Falling through here would read CI off the old head and merge on
            # checks nobody ran against the current base.
            return "rebase-stalled"
        pr = fresh
        if not is_open(pr):
            return pr["state"].lower()

    ci_running, ci_passing = check_ci_status(pr)
    if debug:
        log(pr["label"], f"ci_running={ci_running} ci_passing={ci_passing}")
    if ci_running:
        pr = wait_for_ci(pr)
        if not is_open(pr):
            return pr["state"].lower()
        _, ci_passing = check_ci_status(pr)

    if not ci_passing:
        log(pr["label"], f"CI failing: {', '.join(failing_checks(pr)) or 'unknown'}")
        fresh = rebase_when_behind(pr, debug)
        if fresh is None:
            return "ci-failed"
        pr = fresh
        if not is_open(pr):
            return pr["state"].lower()
        _, ci_passing = check_ci_status(pr)

    if not ci_passing:
        # Previously this fell off the end of the function in silence, so a PR
        # with genuinely red CI looked identical to one that was never seen.
        log(pr["label"], f"CI still failing after rebase check: "
                         f"{', '.join(failing_checks(pr)) or 'unknown'}")
        return "ci-failed"

    # Two passes at most: a stale merge ref is fixed by giving the PR a new
    # head, which invalidates verify_pr's verdict, so the whole tail has to run
    # again against the refreshed branch. If base moves during our own CI the
    # second pass is stale too -- report it and let the next run try.
    for attempt in (1, 2):
        pr = wait_for_clean(pr, debug)
        if not is_open(pr):
            return pr["state"].lower()
        # Base can move while we wait on CI, conflicting a PR that was fine.
        if pr["mergeStateStatus"] == "DIRTY" and request_branch_regeneration(pr):
            return "dirty-regenerating"
        if pr["mergeStateStatus"] not in {"CLEAN", "HAS_HOOKS"}:
            return f"not-mergeable-{pr['mergeStateStatus'].lower()}"
        refusal = verify_pr(pr)
        if refusal:
            return f"unsafe-{refusal}"
        outcome = merge_pr(pr, debug=debug)
        if outcome == "not-rebasable" and request_branch_regeneration(pr):
            # Otherwise this PR is refused identically on every future run while
            # falling further behind -- the worst thing to accumulate unsupervised.
            return "not-rebasable-regenerating"
        if outcome != "stale-merge-ref" or attempt == 2:
            return outcome
        fresh = refresh_stale_merge_ref(pr, debug)
        if fresh is None:
            return outcome
        pr = fresh
        if not is_open(pr):
            return pr["state"].lower()
    return outcome


def process_repository(prs, debug=False):
    """Returns [(label, outcome, seconds)] for every PR in this repository."""
    results = []
    for pr in prs:
        started = time.monotonic()
        try:
            outcome = process_pr(get_pr(pr), debug=debug)
        except GhError as error:
            outcome = "error-gh"
            log(pr["label"], f"giving up on this PR: {error}")
        except Exception:  # noqa: BLE001 - the point is that nothing escapes
            outcome = "error-unexpected"
            log(pr["label"], f"unexpected error:\n{traceback.format_exc().strip()}",
                stream=sys.stderr)
        elapsed = round(time.monotonic() - started)
        log(pr["label"], f"outcome={outcome} in {elapsed}s")
        results.append((pr["label"], outcome, elapsed))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--count", type=int,
                        help="process at most N PRs in total, across the whole run")
    args = parser.parse_args()

    keep_awake()

    started = time.monotonic()
    processed = set()
    results = []
    rounds = 0
    with ThreadPoolExecutor() as executor:
        futures = {}          # future -> repository it is working
        active = set()        # repositories with a worker in flight
        searched = float("-inf")
        while True:
            # `wait` returns on the first repo to finish, so without this floor
            # a burst of short repos would search once per completion. Two
            # `gh search prs` calls each, against a 30/min search limit that
            # answers 403 -- which is not transient, so it would end the run.
            if futures and time.monotonic() - searched < SEARCH_INTERVAL:
                fresh = []
            else:
                fresh = [
                    pr for pr in get_all_prs(debug=args.debug, quiet=rounds > 0)
                    if (pr["repository"], pr["number"]) not in processed
                ]
                searched = time.monotonic()
            if args.count is not None:
                # Budget across the whole run, not per round. Sliced per round,
                # this capped the batch size and then looped until the org was
                # drained, so --count 20 was a throttle rather than a limit.
                # max(0, ...) matters: a negative slice end would silently
                # trim from the tail instead of yielding nothing.
                fresh = fresh[:max(0, args.count - len(processed))]

            prs_by_repo = {}
            for pr in fresh:
                # PRs within a repo must stay serial -- merging one puts the
                # next behind -- so a repo already working keeps its new PRs
                # until its worker finishes and a later round picks them up.
                if pr["repository"] not in active:
                    prs_by_repo.setdefault(pr["repository"], []).append(pr)

            if prs_by_repo:
                rounds += 1
                log("[run]", f"round {rounds}: {sum(map(len, prs_by_repo.values()))} "
                             f"PRs across {len(prs_by_repo)} repos, "
                             f"processing repos in parallel")
                for repository, prs in prs_by_repo.items():
                    processed.update((p["repository"], p["number"]) for p in prs)
                    active.add(repository)
                    futures[executor.submit(process_repository, prs, args.debug)] = \
                        repository

            if not futures:
                break

            # Waiting on all of them was the bug this replaces: one slow repo
            # held the search hostage for as long as it ran, so PRs opened
            # meanwhile sat untouched. drifty took ~4h while the rest idled.
            done, _ = wait(futures, return_when=FIRST_COMPLETED,
                           timeout=max(0, SEARCH_INTERVAL
                                          - (time.monotonic() - searched)))
            for future in done:
                results.extend(future.result())  # re-raise exceptions
                active.discard(futures.pop(future))

    # The per-PR lines are interleaved across threads, so end with a flat
    # account of what happened to everything.
    log("[run]", f"done: {len(results)} PRs in {rounds} round(s), "
                 f"{round(time.monotonic() - started)}s total")
    for outcome, count in Counter(o for _, o, _ in results).most_common():
        log("[run]", f"  {outcome}: {count}")
    unmerged = [(label, o, s) for label, o, s in results if o != "merged"]
    for label, outcome, elapsed in sorted(unmerged):
        log("[run]", f"  {label} {outcome} ({elapsed}s)")
    return any(outcome.startswith("error-") for _, outcome, _ in results)


if __name__ == "__main__":
    try:
        sys.exit(1 if main() else 0)
    except GhError:
        # Raised outside the worker threads (the PR search, gh_login). Already
        # logged; a traceback would add nothing.
        sys.exit(1)
    except KeyboardInterrupt:
        os._exit(130)
