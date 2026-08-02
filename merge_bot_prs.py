#!/usr/bin/env python3
"""
Processes all open pull requests in the arlol organization made by dependabot
or renovate, and tries to do the right thing to merge them.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
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


def run_gh(args, label="", check=True):
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,     # the caller's `check` decides, via the branch below
    )
    if check and result.returncode != 0:
        log(label, f"Error: {result.stderr.strip()}", stream=sys.stderr)
        sys.exit(1)
    return result.stdout

def get_all_prs(organization="arlol", debug=False):
    prs = []
    for author in ["app/dependabot", "app/renovate"]:
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
        "--json", "author,baseRefName,body,headRefOid,labels,mergeStateStatus,number,state,statusCheckRollup",
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
    return "dependabot" in pr["author"]["login"]


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


def merge_pr(pr, poll_interval=15, max_attempts=20, debug=False):
    """Attempt merge, retrying while GitHub re-evaluates rules asynchronously.

    Returns True on merge, False if rule evaluation never settles in time.
    """
    for attempt in range(1, max_attempts + 1):
        result = subprocess.run(
            ["gh", "pr", "merge", str(pr["number"]), "--repo", pr["repository"], "--rebase"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            if attempt > 1:
                log(pr["label"], f"merge succeeded on attempt {attempt}")
            return True
        # Both errors mean GitHub is still evaluating async rules (e.g. code
        # scanning) that briefly reported the PR as CLEAN. Keep polling.
        retryable = (
            "Repository rule violations found" in result.stderr
            or "the base branch policy prohibits the merge" in result.stderr
        )
        if not retryable:
            log(pr["label"], f"Error: {result.stderr.strip()}", stream=sys.stderr)
            if debug:
                print_merge_diagnostics(pr)
            sys.exit(1)
        log(pr["label"], f"rule evaluation pending, waiting {poll_interval}s "
                         f"(attempt {attempt}/{max_attempts}): {result.stderr.strip()}")
        time.sleep(poll_interval)
    return False


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


def wait_for_clean(pr, poll_interval=15, max_attempts=20, stale_codeql_seconds=120):
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
            and codeql_settled(pr, stale_codeql_seconds)
        ):
            run_ids = codeql_run_ids(pr)
            log(pr["label"], f"BLOCKED with CodeQL green >{stale_codeql_seconds}s, "
                             f"re-running CodeQL runs {sorted(run_ids)}")
            rerun_codeql(pr, run_ids)
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
    if merge_pr(pr, debug=debug):
        return "merged"
    return "rule-eval-timeout"


def process_repository(prs, debug=False):
    """Returns [(label, outcome, seconds)] for every PR in this repository."""
    results = []
    for pr in prs:
        started = time.monotonic()
        outcome = process_pr(get_pr(pr), debug=debug)
        elapsed = round(time.monotonic() - started)
        log(pr["label"], f"outcome={outcome} in {elapsed}s")
        results.append((pr["label"], outcome, elapsed))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--count", type=int)
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
            all_prs = all_prs[:args.count]

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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        os._exit(130)
