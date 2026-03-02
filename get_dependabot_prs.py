#!/usr/bin/env python3
"""
Lists all open pull requests in the arlol organization made by dependabot
or renovate, and triggers a rebase on any with the github_actions/github-actions label
that are behind main.
"""

import json
import subprocess
import sys
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


def get_pr_details(repo, number):
    return json.loads(run_gh([
        "pr", "view", str(number),
        "--repo", repo,
        "--json", "body,headRefOid,statusCheckRollup,comments,mergeStateStatus",
    ]))


def get_head_commit_date(repo, head):
    return run_gh([
        "api", f"repos/{repo}/git/commits/{head}",
        "--jq", ".committer.date",
    ]).strip()


def is_currently_rebasing(body):
    return (
        "Dependabot is rebasing this PR" in body
        or "- [x] <!-- rebase-check -->" in body
    )


def rebase_already_triggered(repo, author_login, details):
    """Returns True if a rebase has already been requested and we should wait."""
    if "dependabot" in author_login:
        last_rebase_comment = next((
            comment for comment in reversed(details["comments"])
            if "@dependabot rebase" in comment["body"]
        ), None)
        if not last_rebase_comment:
            return False
        head_commit_date = get_head_commit_date(repo, details["headRefOid"])
        return head_commit_date <= last_rebase_comment["createdAt"]
    else:
        return "- [x] <!-- rebase-check -->" in details["body"]


def trigger_rebase(repo, number, author_login, body):
    if "dependabot" in author_login:
        run_gh([
            "pr", "comment", str(number),
            "--repo", repo,
            "--body", "@dependabot rebase",
        ])
    else:
        new_body = body.replace(
            "- [ ] <!-- rebase-check -->",
            "- [x] <!-- rebase-check -->",
        )
        run_gh(["pr", "edit", str(number), "--repo", repo, "--body", new_body])


def merge_pr(repo, number):
    run_gh([
        "pr", "merge", str(number),
        "--repo", repo,
        "--rebase",
    ])


def check_ci_status(details):
    """Returns (ci_running, ci_passing) tuple."""
    passing_conclusions = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    checks = details["statusCheckRollup"]
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


def wait_for_rebase(repo, number, label, poll_interval=15):
    """Polls until PR is no longer being rebased and is up to date with base.
    Returns latest PR details."""
    while True:
        details = get_pr_details(repo, number)
        if is_currently_rebasing(details["body"]):
            print(f"  [{label}] rebasing, waiting {poll_interval}s...")
            time.sleep(poll_interval)
            continue
        if details["mergeStateStatus"] == "BEHIND":
            print(f"  [{label}] still behind, waiting {poll_interval}s...")
            time.sleep(poll_interval)
            continue
        return details


def wait_for_ci(repo, number, label, poll_interval=15):
    """Polls PR CI status until all checks complete. Returns ci_passing bool."""
    while True:
        details = get_pr_details(repo, number)
        ci_running, ci_passing = check_ci_status(details)
        if not ci_running:
            return ci_passing
        print(f"  [{label}] CI still running, waiting {poll_interval}s...")
        time.sleep(poll_interval)


def process_pr(repo, pr, label_names, details):
    """Process a single PR. Returns a status string for logging."""
    label = f"{repo}#{pr['number']}"
    number = pr["number"]

    author_login = pr["author"]["login"]

    if is_currently_rebasing(details["body"]):
        print(f"  [{label}] rebasing, waiting...")
        details = wait_for_rebase(repo, number, label)
    elif label_names & {"github_actions", "github-actions"}:
        if details["mergeStateStatus"] == "BEHIND":
            if rebase_already_triggered(repo, author_login, details):
                print(f"  [{label}] behind, waiting for rebase")
            else:
                trigger_rebase(repo, number, author_login, details["body"])
                print(f"  [{label}] behind, rebasing")
            details = wait_for_rebase(repo, number, label)

    ci_running, ci_passing = check_ci_status(details)
    if ci_running:
        ci_passing = wait_for_ci(repo, number, label)

    if ci_passing:
        merge_pr(repo, number)
        return f"[{label}] merged"
    else:
        return f"[{label}] CI failed, skipping"


def process_repo(repo):
    """Fetch and process all dependabot/renovate PRs for a single repo serially."""
    prs = []
    for author in ["app/dependabot", "app/renovate"]:
        output = run_gh([
            "pr", "list",
            "--repo", repo,
            "--author", author,
            "--state", "open",
            "--json", "number,labels,body,author",
            "--limit", "100",
        ])
        prs.extend(json.loads(output))
    for pr in prs:
        label_names = {label["name"] for label in pr["labels"]}
        details = get_pr_details(repo, pr["number"])
        print(process_pr(repo, pr, label_names, details))


def main():
    repos_output = run_gh([
        "repo", "list", "arlol",
        "--no-archived",
        "--json", "nameWithOwner",
        "--limit", "1000",
    ])
    repos = [r["nameWithOwner"] for r in json.loads(repos_output)]

    with ThreadPoolExecutor() as executor:
        repo_futures = {
            executor.submit(process_repo, repo): repo
            for repo in repos
        }
        for f in as_completed(repo_futures):
            f.result()  # re-raise exceptions


if __name__ == "__main__":
    main()
