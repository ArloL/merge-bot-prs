#!/usr/bin/env python3
"""
Lists all open pull requests in the arlol organization made by dependabot,
and comments @dependabot rebase on any with the github_actions label that
are behind main.
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
        "--json", "headRefOid,baseRefName,statusCheckRollup,comments",
    ]))


def get_behind_by(repo, base, head):
    compare = json.loads(run_gh([
        "api", f"repos/{repo}/compare/{base}...{head}",
        "--jq", "{behind_by: .behind_by}",
    ]))
    return compare["behind_by"]


def get_head_commit_date(repo, head):
    return run_gh([
        "api", f"repos/{repo}/git/commits/{head}",
        "--jq", ".committer.date",
    ]).strip()


def comment_rebase(repo, number):
    run_gh([
        "pr", "comment", str(number),
        "--repo", repo,
        "--body", "@dependabot rebase",
    ])


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


def wait_for_ci(repo, number, label, poll_interval=30):
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

    if "Dependabot is rebasing this PR" in pr["body"]:
        return f"[{label}] dependabot is rebasing, skipping"

    behind_by = 0
    if "github_actions" in label_names:
        behind_by = get_behind_by(repo, details["baseRefName"], details["headRefOid"])

    ci_running, ci_passing = check_ci_status(details)

    if behind_by > 0:
        last_rebase_comment = next((
            comment for comment in reversed(details["comments"])
            if "@dependabot rebase" in comment["body"]
        ), None)
        needs_comment = True
        if last_rebase_comment:
            head_commit_date = get_head_commit_date(repo, details["headRefOid"])
            needs_comment = (head_commit_date > last_rebase_comment["createdAt"])
        if needs_comment:
            comment_rebase(repo, number)
            return f"[{label}] behind by {behind_by}, rebasing"
        else:
            return f"[{label}] behind by {behind_by}, waiting for dependabot"

    if ci_running:
        ci_passing = wait_for_ci(repo, number, label)

    if ci_passing:
        merge_pr(repo, number)
        return f"[{label}] CI done, merging"
    else:
        return f"[{label}] CI failed, skipping"


def process_repo(repo, prs, executor):
    """Process all PRs for a single repo.
    - non-github_actions PRs: submitted to thread pool in parallel
    - github_actions PRs: processed one at a time (serial)
    """
    github_actions_prs = []
    other_prs = []
    for pr in prs:
        label_names = {label["name"] for label in pr["labels"]}
        if "github_actions" in label_names:
            github_actions_prs.append((pr, label_names))
        else:
            other_prs.append((pr, label_names))

    # Submit non-github_actions PRs in parallel
    futures = []
    for pr, label_names in other_prs:
        details = get_pr_details(repo, pr["number"])
        future = executor.submit(process_pr, repo, pr, label_names, details)
        futures.append(future)

    # Process github_actions PRs serially
    for pr, label_names in github_actions_prs:
        details = get_pr_details(repo, pr["number"])
        result = process_pr(repo, pr, label_names, details)
        print(result)

    # Collect parallel results
    for f in as_completed(futures):
        print(f.result())


def get_dependabot_prs_for_repo(repo):
    """Returns list of open dependabot PRs for a single repo."""
    output = run_gh([
        "pr", "list",
        "--repo", repo,
        "--author", "app/dependabot",
        "--state", "open",
        "--json", "title,url,number,createdAt,labels,body",
        "--limit", "100",
    ])
    prs = json.loads(output)
    for pr in prs:
        pr["repository"] = {"nameWithOwner": repo}
    return prs


def main():
    repos_output = run_gh([
        "repo", "list", "arlol",
        "--no-archived",
        "--json", "nameWithOwner",
        "--limit", "1000",
    ])
    repos = [r["nameWithOwner"] for r in json.loads(repos_output)]

    with ThreadPoolExecutor() as executor:
        # Fetch PRs for all repos in parallel
        pr_futures = {
            executor.submit(get_dependabot_prs_for_repo, repo): repo
            for repo in repos
        }
        by_repo = {}
        for f in as_completed(pr_futures):
            prs = f.result()
            if prs:
                repo = prs[0]["repository"]["nameWithOwner"]
                by_repo[repo] = prs

        if not by_repo:
            print("No open pull requests found.")
            return

        total = sum(len(prs) for prs in by_repo.values())
        print(f"Found {total} open pull request(s):\n")
        for repo, prs in sorted(by_repo.items()):
            for pr in prs:
                print(f"  #{pr['number']} [{repo}] {pr['title']}")
                print(f"    {pr['url']}  (created: {pr['createdAt'][:10]})")

        print()

        # Process each repo in parallel
        repo_futures = {
            executor.submit(process_repo, repo, repo_prs, executor): repo
            for repo, repo_prs in by_repo.items()
        }
        for f in as_completed(repo_futures):
            f.result()  # re-raise exceptions


if __name__ == "__main__":
    main()
