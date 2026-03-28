#!/usr/bin/env python3
"""
Processes all open pull requests in the arlol organization made by dependabot
or renovate, and tries to do the right thing to merge them.
"""

import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


def run_gh(args):
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout

def get_repositories(organization="arlol"):
    repositories_output = run_gh([
        "repo", "list", organization,
        "--no-archived",
        "--json", "nameWithOwner",
        "--limit", "1000",
    ])
    repositories = [r["nameWithOwner"] for r in json.loads(repositories_output)]
    return repositories


def get_prs(repository):
    prs = []
    for author in ["app/dependabot", "app/renovate"]:
        prs_output = run_gh([
            "pr", "list",
            "--repo", repository,
            "--author", author,
            "--state", "open",
            "--json", "number,labels,body,author",
            "--limit", "100",
        ])
        prs.extend(json.loads(prs_output))
    return prs


def get_pr_details(repository, number):
    return json.loads(run_gh([
        "pr", "view", str(number),
        "--repo", repository,
        "--json", "body,baseRefName,headRefOid,statusCheckRollup,comments,mergeStateStatus,state",
    ]))


def is_behind(repository, pr_details):
    behind_by = run_gh([
        "api", f"repos/{repository}/compare/{pr_details['baseRefName']}...{pr_details['headRefOid']}",
        "--jq", ".behind_by",
    ]).strip()
    return int(behind_by) > 0


def is_open(pr_details):
    return pr_details["state"] == "OPEN"


def get_head_commit_date(repository, head):
    return run_gh([
        "api", f"repos/{repository}/git/commits/{head}",
        "--jq", ".committer.date",
    ]).strip()


def is_currently_rebasing(pr_details):
    return (
        "Dependabot is rebasing this PR" in pr_details["body"]
        or "- [x] <!-- rebase-check -->" in pr_details["body"]
    )


def rebase_already_triggered(repository, author_login, pr_details):
    """Returns True if a rebase has already been requested and we should wait."""
    if "dependabot" in author_login:
        last_rebase_comment = next((
            comment for comment in reversed(pr_details["comments"])
            if "@dependabot rebase" in comment["body"]
        ), None)
        if not last_rebase_comment:
            return False
        head_commit_date = get_head_commit_date(repository, pr_details["headRefOid"])
        return head_commit_date <= last_rebase_comment["createdAt"]
    else:
        return "- [x] <!-- rebase-check -->" in pr_details["body"]


def trigger_rebase(repository, number, author_login, body):
    if "dependabot" in author_login:
        run_gh([
            "pr", "comment", str(number),
            "--repo", repository,
            "--body", "@dependabot rebase",
        ])
    else:
        new_body = body.replace(
            "- [ ] <!-- rebase-check -->",
            "- [x] <!-- rebase-check -->",
        )
        run_gh(["pr", "edit", str(number), "--repo", repository, "--body", new_body])


def merge_pr(repository, number):
    run_gh([
        "pr", "merge", str(number),
        "--repo", repository,
        "--rebase",
    ])


def check_ci_status(pr_details):
    """Returns (ci_running, ci_passing) tuple."""
    passing_conclusions = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    checks = pr_details["statusCheckRollup"]
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


def wait_for_rebase(repository, number, label, poll_interval=15):
    """Polls until PR is no longer being rebased and is up to date with base.
    Returns latest PR pr_details."""
    while True:
        pr_details = get_pr_details(repository, number)
        if not is_open(pr_details):
            print(f"[{label}] {pr_details['state']} while waiting for rebase")
            return pr_details
        if is_currently_rebasing(pr_details):
            print(f"[{label}] rebasing, waiting {poll_interval}s...")
            time.sleep(poll_interval)
            continue
        if is_behind(repository, pr_details):
            print(f"[{label}] still behind, waiting {poll_interval}s...")
            time.sleep(poll_interval)
            continue
        return pr_details


def wait_for_ci(repository, number, label, poll_interval=15):
    """Polls PR CI status until all checks complete. Returns latest pr_details."""
    while True:
        pr_details = get_pr_details(repository, number)
        if not is_open(pr_details):
            print(f"[{label}] {pr_details['state']} while waiting for CI")
            return pr_details
        ci_running, ci_passing = check_ci_status(pr_details)
        if not ci_running:
            return pr_details
        print(f"[{label}] CI still running, waiting {poll_interval}s...")
        time.sleep(poll_interval)



def process_pr(repository, pr, debug=False):
    label_names = {label["name"] for label in pr["labels"]}
    number = pr["number"]
    label = f"{repository}#{number}"
    author_login = pr["author"]["login"]

    pr_details = get_pr_details(repository, number)

    if debug:
        print(f"[{label}] mergeStateStatus={pr_details['mergeStateStatus']}")

    if is_currently_rebasing(pr_details):
        print(f"[{label}] rebasing, waiting...")
        pr_details = wait_for_rebase(repository, number, label)

    if not is_open(pr_details):
        return

    merge_state = pr_details["mergeStateStatus"]

    if merge_state in {"DIRTY", "DRAFT"}:
        print(f"[{label}] {merge_state}, skipping")
        return

    if label_names & {"github_actions", "github-actions"}:
        behind = is_behind(repository, pr_details)
        if debug:
            print(f"[{label}] is_behind={behind}")
        if behind:
            if rebase_already_triggered(repository, author_login, pr_details):
                print(f"[{label}] waiting for rebase")
            else:
                trigger_rebase(repository, number, author_login, pr_details["body"])
                print(f"[{label}] rebasing")
            pr_details = wait_for_rebase(repository, number, label)
            if not is_open(pr_details):
                return

    ci_running, ci_passing = check_ci_status(pr_details)
    if debug:
        print(f"[{label}] ci_running={ci_running} ci_passing={ci_passing}")
    if ci_running:
        pr_details = wait_for_ci(repository, number, label)
        if not is_open(pr_details):
            return
        _, ci_passing = check_ci_status(pr_details)

    if ci_passing:
        merge_pr(repository, number)
        print(f"[{label}] merged")
    else:
        print(f"[{label}] CI failed, skipping")


def process_repository(repository, debug=False, semaphore=None):
    for pr in get_prs(repository):
        if semaphore is not None and not semaphore.acquire(blocking=False):
            break
        process_pr(repository, pr, debug=debug)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--count", type=int)
    args = parser.parse_args()

    semaphore = threading.Semaphore(args.count) if args.count is not None else None

    repositories = get_repositories()

    with ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(process_repository, repository, args.debug, semaphore): repository
            for repository in repositories
        }
        for future in as_completed(futures):
            future.result()  # re-raise exceptions


if __name__ == "__main__":
    main()
