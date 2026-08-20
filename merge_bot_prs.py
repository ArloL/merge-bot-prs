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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

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


def get_all_prs(organization="arlol", debug=False):
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
        log("[search]", f"{author}: {len(found)} open PRs (archived repos excluded)")
        prs.extend(found)
    result = [{
        "number": pr["number"],
        "repository": pr["repository"]["nameWithOwner"],
        "label": f"[{pr['repository']['nameWithOwner']}#{pr['number']}]",
    } for pr in prs]
    if debug:
        by_repo = Counter(pr["repository"] for pr in result)
        for repository, count in sorted(by_repo.items()):
            log("[search]", f"  {repository}: {count}")
    return result


def get_pr(pr):
    result = json.loads(run_gh([
        "pr", "view", str(pr["number"]),
        "--repo", pr["repository"],
        "--json", ("author,baseRefName,body,headRefName,headRefOid,headRepositoryOwner,"
                   "isCrossRepository,labels,mergeStateStatus,number,reviewDecision,state,"
                   "statusCheckRollup"),
    ], label=pr["label"]))
    result["repository"] = pr["repository"]
    result["label"] = pr["label"]
    return result


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


def _is_codeql_check(check):
    text = ((check.get("workflowName") or "") + " " + (check.get("name") or "")).lower()
    return "codeql" in text


def codeql_settled(pr, min_age_seconds=120):
    """True if every CodeQL-related check completed successfully at least
    min_age_seconds ago — the fingerprint of GitHub's stale code-scanning bug,
    distinct from a CodeQL run that is still legitimately in progress."""
    codeql_checks = [c for c in pr["statusCheckRollup"] if _is_codeql_check(c)]
    if not codeql_checks:
        return False
    now = datetime.now(UTC)
    passing = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    for c in codeql_checks:
        if c.get("status") != "COMPLETED" or c.get("conclusion") not in passing:
            return False
        completed_at = c.get("completedAt")
        if not completed_at:
            return False
        age = (now - datetime.fromisoformat(completed_at)).total_seconds()
        if age < min_age_seconds:
            return False
    return True


def codeql_run_ids(pr):
    """Workflow run ids backing the CodeQL Analysis checks on this PR."""
    ids = set()
    for c in pr["statusCheckRollup"]:
        if not _is_codeql_check(c):
            continue
        match = re.search(r"/actions/runs/(\d+)", c.get("detailsUrl") or "")
        if match:
            ids.add(match.group(1))
    return ids


def rerun_codeql(pr, run_ids):
    for run_id in run_ids:
        # check=False: a stale/expired run that can't be re-run must not kill
        # the whole process; we fall through and skip the PR instead.
        run_gh(["run", "rerun", run_id, "--repo", pr["repository"]],
               label=pr["label"], check=False)


def rerun_stale_codeql(pr, min_age_seconds):
    """Re-run CodeQL if every CodeQL check has been green for min_age_seconds.
    Returns True if a re-run was kicked off."""
    if not codeql_settled(pr, min_age_seconds):
        return False
    run_ids = codeql_run_ids(pr)
    log(pr["label"], f"CodeQL green >{min_age_seconds}s but code scanning still "
                     f"blocking, re-running runs {sorted(run_ids)}")
    rerun_codeql(pr, run_ids)
    return True


def update_branch(pr):
    """Rebase the branch onto its base via GitHub, bypassing the bot entirely.

    Returns True if the branch was moved.
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
        return False
    old_head = pr["headRefOid"]
    new_head = get_pr(pr)["headRefOid"]
    log(pr["label"], f"branch updated {old_head[:8]} -> {new_head[:8]}")
    return True


def rebase_already_triggered(pr):
    """Returns True if renovate has already been asked to rebase and we should wait."""
    return "- [x] <!-- rebase-check -->" in pr["body"]


def trigger_rebase(pr):
    new_body = pr["body"].replace(
        "- [ ] <!-- rebase-check -->",
        "- [x] <!-- rebase-check -->",
    )
    run_gh(["pr", "edit", str(pr["number"]), "--repo", pr["repository"], "--body", new_body], label=pr["label"])


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


def merge_pr(pr, poll_interval=15, max_attempts=20, debug=False,
             stale_codeql_seconds=60):
    """Attempt merge, retrying while GitHub re-evaluates rules asynchronously.

    Returns the outcome: "merged", "head-changed" if something landed on the
    branch after verify_pr vouched for it, or "rule-eval-timeout" if rule
    evaluation never settles in time.
    """
    reran_codeql = False
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
        log(pr["label"], f"rule evaluation pending, waiting {poll_interval}s "
                         f"(attempt {attempt}/{max_attempts}): {result.stderr.strip()}")
        # This, not BLOCKED in wait_for_clean, is where the stale code-scanning
        # bug actually surfaces: mergeStateStatus reads CLEAN and only the merge
        # call reports that code scanning is still waiting on a CodeQL run that
        # finished long ago. Without a re-run here the loop just burns its whole
        # budget and reports rule-eval-timeout.
        if not reran_codeql:
            fresh = get_pr(pr)
            if not is_open(fresh):
                return fresh["state"].lower()
            if fresh["headRefOid"] != pr["headRefOid"]:
                log(pr["label"], f"head moved away from {pr['headRefOid'][:8]} while "
                                 f"merging, refusing")
                return "head-changed"
            if rerun_stale_codeql(fresh, stale_codeql_seconds):
                reran_codeql = True
                time.sleep(poll_interval)     # let GitHub re-queue the jobs
                fresh = wait_for_ci(fresh)
                if not is_open(fresh):
                    return fresh["state"].lower()
                attempt = 0                   # fresh budget for re-registration
                continue
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


def wait_for_clean(pr, poll_interval=15, max_attempts=20, stale_codeql_seconds=60):
    reran_codeql = False
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
        if (
            merge_state == "BLOCKED"
            and not reran_codeql
            and rerun_stale_codeql(pr, stale_codeql_seconds)
        ):
            reran_codeql = True
            time.sleep(poll_interval)     # let GitHub re-queue the jobs
            pr = wait_for_ci(pr)          # wait out the fresh CodeQL run
            if not is_open(pr):
                return pr
            attempts = 0                  # fresh budget for re-registration
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
        if not update_branch(pr):
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

    merge_state = pr["mergeStateStatus"]
    if merge_state in {"DIRTY", "DRAFT"}:
        return merge_state.lower()

    label_names = {label["name"] for label in pr["labels"]}
    if label_names & {"github_actions", "github-actions"}:
        pr = rebase_when_behind(pr, debug)
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
        pr = rebase_when_behind(pr, debug)
        if not is_open(pr):
            return pr["state"].lower()
        _, ci_passing = check_ci_status(pr)

    if not ci_passing:
        # Previously this fell off the end of the function in silence, so a PR
        # with genuinely red CI looked identical to one that was never seen.
        log(pr["label"], f"CI still failing after rebase check: "
                         f"{', '.join(failing_checks(pr)) or 'unknown'}")
        return "ci-failed"

    pr = wait_for_clean(pr)
    if not is_open(pr):
        return pr["state"].lower()
    if pr["mergeStateStatus"] not in {"CLEAN", "HAS_HOOKS"}:
        return f"not-mergeable-{pr['mergeStateStatus'].lower()}"
    refusal = verify_pr(pr)
    if refusal:
        return f"unsafe-{refusal}"
    return merge_pr(pr, debug=debug)


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
                        help="process at most N PRs in total, across all passes")
    args = parser.parse_args()

    started = time.monotonic()
    processed = set()
    results = []
    passes = 0
    while True:
        all_prs = [
            pr for pr in get_all_prs(debug=args.debug)
            if (pr["repository"], pr["number"]) not in processed
        ]
        if args.count is not None:
            # Budget across the whole run, not per pass. Sliced per pass, this
            # capped the batch size and then looped until the org was drained,
            # so --count 20 was a throttle rather than a limit.
            # max(0, ...) matters: a negative slice end would silently
            # trim from the tail instead of yielding nothing.
            all_prs = all_prs[:max(0, args.count - len(processed))]

        if not all_prs:
            break

        processed.update((pr["repository"], pr["number"]) for pr in all_prs)

        prs_by_repo = {}
        for pr in all_prs:
            prs_by_repo.setdefault(pr["repository"], []).append(pr)

        passes += 1
        log("[run]", f"pass {passes}: {len(all_prs)} PRs across "
                     f"{len(prs_by_repo)} repos, processing repos in parallel")

        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(process_repository, prs, args.debug): repository
                for repository, prs in prs_by_repo.items()
            }
            for future in as_completed(futures):
                results.extend(future.result())  # re-raise exceptions

    # The per-PR lines are interleaved across threads, so end with a flat
    # account of what happened to everything.
    log("[run]", f"done: {len(results)} PRs in {passes} pass(es), "
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
