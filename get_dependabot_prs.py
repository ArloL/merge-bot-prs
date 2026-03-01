#!/usr/bin/env python3
"""
Lists open pull requests in the arlol organization made by dependabot
that update calver-tag-action, and comments @dependabot rebase on any
with the github_actions label that are behind main.
"""

import json
import subprocess
import sys


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


def enable_automerge(repo, number):
    run_gh([
        "pr", "merge", str(number),
        "--repo", repo,
        "--auto",
        "--rebase",
    ])


def merge_pr(repo, number):
    run_gh([
        "pr", "merge", str(number),
        "--repo", repo,
        "--rebase",
    ])


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <search-term>", file=sys.stderr)
        sys.exit(1)
    search_term = sys.argv[1]

    output = run_gh([
        "search", "prs",
        search_term,
        "--owner", "arlol",
        "--author", "app/dependabot",
        "--state", "open",
        "--archived=false",
        "--json", "title,url,repository,number,createdAt,labels,body",
        "--limit", "100",
    ])
    prs = json.loads(output)

    if not prs:
        print("No open pull requests found.")
        return

    print(f"Found {len(prs)} open pull request(s):\n")
    for pr in prs:
        repo = pr["repository"]["nameWithOwner"]
        number = pr["number"]
        print(f"  #{number} [{repo}] {pr['title']}")
        print(f"    {pr['url']}  (created: {pr['createdAt'][:10]})")
        if "Dependabot is rebasing this PR" in pr["body"]:
            print("    dependabot is rebasing, skipping")
            continue
        details = get_pr_details(repo, number)
        label_names = {label["name"] for label in pr["labels"]}
        behind_by = 0
        if "github_actions" in label_names:
            behind_by = get_behind_by(
                repo, details["baseRefName"], details["headRefOid"]
            )
        passing_conclusions = {"SUCCESS", "NEUTRAL", "SKIPPED"}
        ci_running = any(
            check["status"] != "COMPLETED"
            for check in details["statusCheckRollup"]
        )
        ci_passing = all(
            check["conclusion"] in passing_conclusions
            for check in details["statusCheckRollup"]
        )
        if behind_by > 0:
            last_rebase_comment = next((
                comment for comment in reversed(details["comments"])
                if "@dependabot rebase" in comment["body"]
            ), None)
            needs_comment = True
            if last_rebase_comment:
                head_commit_date = get_head_commit_date(
                    repo, details["headRefOid"]
                )
                needs_comment = (
                    head_commit_date > last_rebase_comment["createdAt"]
                )
            if needs_comment:
                print(f"    behind by {behind_by}, rebasing")
                comment_rebase(repo, number)
            else:
                print(f"    behind by {behind_by}, waiting for dependabot")
        elif ci_running:
            print("    CI running, enabling automerge")
            enable_automerge(repo, number)
        elif ci_passing:
            print("    CI done, merging")
            merge_pr(repo, number)
        else:
            print("    CI failed, skipping")


if __name__ == "__main__":
    main()
