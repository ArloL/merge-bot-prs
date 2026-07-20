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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone


def run_gh(args, label="", check=True):
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        print(f"{label} Error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout

def get_all_prs(organization="arlol"):
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
        prs.extend(json.loads(prs_output))
    return [{
        "number": pr["number"],
        "repository": pr["repository"]["nameWithOwner"],
        "label": f"[{pr['repository']['nameWithOwner']}#{pr['number']}]",
    } for pr in prs]


def get_pr(pr):
    result = json.loads(run_gh([
        "pr", "view", str(pr["number"]),
        "--repo", pr["repository"],
        "--json", "author,baseRefName,body,comments,headRefOid,labels,mergeStateStatus,number,state,statusCheckRollup",
    ], label=pr["label"]))
    result["repository"] = pr["repository"]
    result["label"] = pr["label"]
    return result


def is_behind(pr):
    behind_by = run_gh([
        "api", f"repos/{pr["repository"]}/compare/{pr['baseRefName']}...{pr['headRefOid']}",
        "--jq", ".behind_by",
    ], label=pr["label"]).strip()
    return int(behind_by) > 0


def is_open(pr):
    return pr["state"] == "OPEN"


def get_head_commit_date(pr):
    return run_gh([
        "api", f"repos/{pr["repository"]}/git/commits/{pr["headRefOid"]}",
        "--jq", ".committer.date",
    ], label=pr["label"]).strip()


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
    now = datetime.now(timezone.utc)
    passing = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    for c in codeql_checks:
        if c.get("status") != "COMPLETED" or c.get("conclusion") not in passing:
            return False
        completed_at = c.get("completedAt")
        if not completed_at:
            return False
        age = (now - datetime.fromisoformat(completed_at.replace("Z", "+00:00"))).total_seconds()
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


def rebase_already_triggered(pr):
    """Returns True if a rebase has already been requested and we should wait."""
    if "dependabot" in pr["author"]["login"]:
        last_rebase_comment = next((
            comment for comment in reversed(pr["comments"])
            if "@dependabot rebase" in comment["body"]
        ), None)
        if not last_rebase_comment:
            return False
        head_commit_date = get_head_commit_date(pr)
        return head_commit_date <= last_rebase_comment["createdAt"]
    else:
        return "- [x] <!-- rebase-check -->" in pr["body"]


def trigger_rebase(pr):
    if "dependabot" in pr["author"]["login"]:
        run_gh([
            "pr", "comment", str(pr["number"]),
            "--repo", pr["repository"],
            "--body", "@dependabot rebase",
        ], label=pr["label"])
    else:
        new_body = pr["body"].replace(
            "- [ ] <!-- rebase-check -->",
            "- [x] <!-- rebase-check -->",
        )
        run_gh(["pr", "edit", str(pr["number"]), "--repo", pr["repository"], "--body", new_body], label=pr["label"])


def print_merge_diagnostics(pr):
    """Print why GitHub is refusing to merge: fresh merge state, required
    checks, and the branch protection / ruleset that governs the base branch."""
    fresh = get_pr(pr)
    print(f"{pr['label']} diagnostics: mergeStateStatus={fresh['mergeStateStatus']} state={fresh['state']}")
    for check in fresh["statusCheckRollup"]:
        name = check.get("name") or check.get("context") or check.get("__typename", "?")
        status = check.get("status") or check.get("state")
        conclusion = check.get("conclusion")
        required = check.get("isRequired")
        print(f"{pr['label']} diagnostics: check {name}: status={status} conclusion={conclusion} required={required}")

    # Classic branch protection (404 when only rulesets are configured).
    protection = run_gh([
        "api", f"repos/{pr['repository']}/branches/{fresh['baseRefName']}/protection",
    ], label=pr["label"], check=False).strip()
    if protection:
        print(f"{pr['label']} diagnostics: branch protection: {protection}")

    # Rulesets that apply to the base branch (the modern equivalent).
    rules = run_gh([
        "api", f"repos/{pr['repository']}/rules/branches/{fresh['baseRefName']}",
    ], label=pr["label"], check=False).strip()
    if rules:
        print(f"{pr['label']} diagnostics: active rules: {rules}")


def merge_pr(pr, poll_interval=15, max_attempts=20, debug=False):
    """Attempt merge, retrying while GitHub re-evaluates rules asynchronously.

    Returns True on merge, False if rule evaluation never settles in time.
    """
    for _ in range(max_attempts):
        result = subprocess.run(
            ["gh", "pr", "merge", str(pr["number"]), "--repo", pr["repository"], "--rebase"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        # Both errors mean GitHub is still evaluating async rules (e.g. code
        # scanning) that briefly reported the PR as CLEAN. Keep polling.
        retryable = (
            "Repository rule violations found" in result.stderr
            or "the base branch policy prohibits the merge" in result.stderr
        )
        if not retryable:
            print(f"{pr['label']} Error: {result.stderr.strip()}", file=sys.stderr)
            if debug:
                print_merge_diagnostics(pr)
            sys.exit(1)
        print(f"{pr['label']} rule evaluation pending, waiting {poll_interval}s...")
        time.sleep(poll_interval)
    return False


def check_ci_status(pr):
    """Returns (ci_running, ci_passing) tuple."""
    passing_conclusions = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    checks = pr["statusCheckRollup"]
    ci_running = any(
        check.get("status", "COMPLETED") != "COMPLETED"
        if "status" in check
        else check.get("state") in {"EXPECTED", "PENDING"}
        for check in checks
    )
    ci_passing = all(
        check["conclusion"] in passing_conclusions
        if "status" in check
        else check.get("state") == "SUCCESS"
        for check in checks
    )
    return ci_running, ci_passing


def wait_for_rebase(pr, poll_interval=15):
    while True:
        pr = get_pr(pr)
        if not is_open(pr):
            print(f"{pr["label"]} {pr['state']} while waiting for rebase")
            return pr
        if is_currently_rebasing(pr):
            print(f"{pr["label"]} rebasing, waiting {poll_interval}s...")
            time.sleep(poll_interval)
            continue
        if is_behind(pr):
            print(f"{pr["label"]} still behind, waiting {poll_interval}s...")
            time.sleep(poll_interval)
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
            print(f"{pr['label']} {merge_state} while waiting for clean, skipping")
            return pr
        if (
            merge_state == "BLOCKED"
            and not reran_codeql
            and codeql_settled(pr, stale_codeql_seconds)
        ):
            run_ids = codeql_run_ids(pr)
            print(f"{pr['label']} BLOCKED with CodeQL green >{stale_codeql_seconds}s, "
                  f"re-running CodeQL runs {sorted(run_ids)}")
            rerun_codeql(pr, run_ids)
            reran_codeql = True
            time.sleep(poll_interval)     # let GitHub re-queue the jobs
            pr = wait_for_ci(pr)          # wait out the fresh CodeQL run
            if not is_open(pr):
                return pr
            attempts = 0                  # fresh budget for re-registration
            continue
        print(f"{pr['label']} {merge_state}, waiting {poll_interval}s...")
        time.sleep(poll_interval)
        attempts += 1
    return pr


def wait_for_ci(pr, poll_interval=15):
    while True:
        pr = get_pr(pr)
        if not is_open(pr):
            print(f"{pr["label"]} {pr['state']} while waiting for CI")
            return pr
        ci_running, _ = check_ci_status(pr)
        if ci_running:
            print(f"{pr["label"]} CI still running, waiting {poll_interval}s...")
            time.sleep(poll_interval)
            continue
        return pr


def rebase_when_behind(pr, debug=False):
    behind = is_behind(pr)
    if debug:
        print(f"{pr["label"]} is_behind={behind}")
    if behind:
        if rebase_already_triggered(pr):
            print(f"{pr["label"]} waiting for rebase")
        else:
            trigger_rebase(pr)
            print(f"{pr["label"]} rebasing")

        pr = wait_for_rebase(pr)

        if not is_open(pr):
            return pr

        ci_running, ci_passing = check_ci_status(pr)
        if ci_running:
            pr = wait_for_ci(pr)
            if not is_open(pr):
                return pr

    return pr


def process_pr(pr, debug=False):
    if debug:
        print(f"{pr["label"]} mergeStateStatus={pr['mergeStateStatus']}")

    if is_currently_rebasing(pr):
        print(f"{pr["label"]} rebasing, waiting...")
        pr = wait_for_rebase(pr)
        if not is_open(pr):
            return

    merge_state = pr["mergeStateStatus"]
    if merge_state in {"DIRTY", "DRAFT"}:
        print(f"{pr["label"]} {merge_state}, skipping")
        return

    label_names = {label["name"] for label in pr["labels"]}
    if label_names & {"github_actions", "github-actions"}:
        pr = rebase_when_behind(pr, debug)
        if not is_open(pr):
            return

    ci_running, ci_passing = check_ci_status(pr)
    if debug:
        print(f"{pr["label"]} ci_running={ci_running} ci_passing={ci_passing}")
    if ci_running:
        pr = wait_for_ci(pr)
        if not is_open(pr):
            return
        _, ci_passing = check_ci_status(pr)

    if not ci_passing:
        pr = rebase_when_behind(pr, debug)
        if not is_open(pr):
            return
        _, ci_passing = check_ci_status(pr)

    if ci_passing:
        pr = wait_for_clean(pr)
        if not is_open(pr):
            return
        if pr["mergeStateStatus"] not in {"CLEAN", "HAS_HOOKS"}:
            print(f"{pr['label']} {pr['mergeStateStatus']}, skipping")
            return
        if merge_pr(pr, debug=debug):
            print(f"{pr['label']} merged")
        else:
            print(f"{pr['label']} rule evaluation never settled, skipping")


def process_repository(prs, debug=False):
    for pr in prs:
        process_pr(get_pr(pr), debug=debug)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--count", type=int)
    args = parser.parse_args()

    processed = set()
    while True:
        all_prs = [
            pr for pr in get_all_prs()
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

        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(process_repository, prs, args.debug): repository
                for repository, prs in prs_by_repo.items()
            }
            for future in as_completed(futures):
                future.result()  # re-raise exceptions


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        os._exit(130)
