#!/usr/bin/env python3
"""
Clears the GitHub notification inbox of things that never need a human: release
notifications from the organization's own repositories, and pull requests
opened by dependabot or renovate that have already been merged.
"""

import argparse
import json
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from merge_bot_prs import BOT_COMMIT_LOGINS, GhError, log, paginated, run_gh

# The PR lookups are one API call each and the inbox runs to hundreds of
# threads, so they are spread over a pool. Everything else here is serial.
LOOKUP_WORKERS = 8


def inbox(everything=False, label=""):
    """Notifications to consider: unread by default, the whole list with
    `everything`.

    Unread is the default because it is the only state the script can see.
    REST models `unread` and nothing else -- a thread marked done is
    indistinguishable from one nobody has touched, and `?all=true` goes on
    returning done threads forever. Marking done also clears unread, so
    scoping to unread is what keeps a second run from redoing the first.
    `--all` exists for the initial catch-up sweep, and re-clears everything it
    already cleared each time it is used.
    """
    query = "all=true&per_page=100" if everything else "per_page=100"
    return paginated(f"/notifications?{query}", label=label)


def is_merged_bot_pr(notification, label=""):
    """Whether the PR behind this notification was opened by a bot and merged.

    The notification payload carries neither the author nor the merge state, so
    the PR itself has to be fetched. `subject.url` is already its API URL.
    """
    url = notification["subject"].get("url")
    if not url:
        return False
    pr = json.loads(run_gh(["api", url], label=label))
    return pr["user"]["login"] in BOT_COMMIT_LOGINS and pr["merged"]


def mark_done(notification, label=""):
    """Mark a thread done -- PATCH would only mark it read, which keeps it in
    the inbox. `?all=true` goes on listing done threads afterwards, so the
    unread count is the only way to see this worked."""
    run_gh(["api", "--silent", "--method", "DELETE",
            f"/notifications/threads/{notification['id']}"], label=label)


def classify(notification, organization, label=""):
    repo = notification["repository"]["full_name"]
    subject = notification["subject"]["type"]
    if subject == "Release":
        # Releases are cleared only for the org's own repositories: a release
        # from someone else's project is something the user chose to watch.
        if repo.split("/")[0].lower() == organization.lower():
            return "release"
        return None
    if subject == "PullRequest" and is_merged_bot_pr(notification, label):
        # Not restricted to the org -- a merged bot PR is finished business
        # wherever it lives.
        return "merged-bot-pr"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="arlol",
                        help="organization whose releases are cleared")
    parser.add_argument("--all", action="store_true",
                        help="sweep read notifications too, not just unread")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be cleared, change nothing")
    args = parser.parse_args()

    started = time.monotonic()
    notifications = inbox(args.all, "[inbox]")
    log("[inbox]", f"{len(notifications)} notifications")

    outcomes = Counter()

    def decide(notification):
        repo = notification["repository"]["full_name"]
        label = f"[{repo}]"
        try:
            return notification, classify(notification, args.org, label), label
        except GhError as error:
            log(label, f"giving up on this notification: {error}")
            return notification, "error", label
        except Exception:  # noqa: BLE001 - the point is that nothing escapes
            log(label, f"unexpected error:\n{traceback.format_exc().strip()}",
                stream=sys.stderr)
            return notification, "error", label

    with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
        decided = list(pool.map(decide, notifications))

    for notification, reason, label in decided:
        if reason is None:
            outcomes["kept"] += 1
            continue
        if reason == "error":
            outcomes["error"] += 1
            continue
        title = notification["subject"]["title"]
        if args.dry_run:
            log(label, f"would clear ({reason}): {title}")
            outcomes[f"would-clear-{reason}"] += 1
            continue
        try:
            mark_done(notification, label)
            log(label, f"cleared ({reason}): {title}")
            outcomes[reason] += 1
        except GhError as error:
            log(label, f"could not clear: {error}")
            outcomes["error"] += 1

    log("[run]", f"done in {round(time.monotonic() - started)}s")
    for outcome, count in outcomes.most_common():
        log("[run]", f"  {outcome}: {count}")
    unread = len(paginated("/notifications?per_page=100", label="[inbox]"))
    log("[run]", f"  unread remaining: {unread}")
    return outcomes["error"] > 0


if __name__ == "__main__":
    try:
        sys.exit(1 if main() else 0)
    except GhError:
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
