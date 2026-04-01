#!/usr/bin/env python3
"""
Processes all open pull requests in the arlol organization made by dependabot
or renovate, and tries to do the right thing to merge them.
"""

import argparse
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
    ]))
    result["repository"] = pr["repository"]
    result["label"] = pr["label"]
    return result


def is_behind(pr):
    behind_by = run_gh([
        "api", f"repos/{pr["repository"]}/compare/{pr['baseRefName']}...{pr['headRefOid']}",
        "--jq", ".behind_by",
    ]).strip()
    return int(behind_by) > 0


def is_open(pr):
    return pr["state"] == "OPEN"


def get_head_commit_date(pr):
    return run_gh([
        "api", f"repos/{pr["repository"]}/git/commits/{pr["headRefOid"]}",
        "--jq", ".committer.date",
    ]).strip()


def is_currently_rebasing(pr):
    return (
        "Dependabot is rebasing this PR" in pr["body"]
        or "- [x] <!-- rebase-check -->" in pr["body"]
    )


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
        ])
    else:
        new_body = pr["body"].replace(
            "- [ ] <!-- rebase-check -->",
            "- [x] <!-- rebase-check -->",
        )
        run_gh(["pr", "edit", str(pr["number"]), "--repo", pr["repository"], "--body", new_body])


def merge_pr(pr):
    run_gh([
        "pr", "merge", str(pr["number"]),
        "--repo", pr["repository"],
        "--rebase",
    ])


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
            return

        ci_running, ci_passing = check_ci_status(pr)
        if ci_running:
            pr = wait_for_ci(pr)
            if not is_open(pr):
                return

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
        merge_pr(pr)
        print(f"{pr["label"]} merged")


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
    main()
