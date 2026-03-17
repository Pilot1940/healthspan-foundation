"""
Batch-update GitHub issues for Healthspan Foundation.

Usage:
    python scripts/github_issues.py
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("GITHUB_TOKEN", "")
REPO = os.getenv("GITHUB_REPO", "")

if not TOKEN or not REPO:
    print("ERROR: GITHUB_TOKEN and GITHUB_REPO must be set in .env")
    sys.exit(1)

API = f"https://api.github.com/repos/{REPO}"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def comment_and_close(issue_num: int, comment: str) -> None:
    """Add a comment and close an issue."""
    # Add comment
    r = requests.post(
        f"{API}/issues/{issue_num}/comments",
        headers=HEADERS,
        json={"body": comment},
    )
    if r.status_code == 201:
        print(f"  #{issue_num}: Comment added")
    else:
        print(f"  #{issue_num}: Comment FAILED ({r.status_code}: {r.json().get('message', '')})")
        return

    # Close issue
    r = requests.patch(
        f"{API}/issues/{issue_num}",
        headers=HEADERS,
        json={"state": "closed"},
    )
    if r.status_code == 200:
        print(f"  #{issue_num}: Closed")
    else:
        print(f"  #{issue_num}: Close FAILED ({r.status_code}: {r.json().get('message', '')})")


def comment_only(issue_num: int, comment: str) -> None:
    """Add a comment without closing."""
    r = requests.post(
        f"{API}/issues/{issue_num}/comments",
        headers=HEADERS,
        json={"body": comment},
    )
    if r.status_code == 201:
        print(f"  #{issue_num}: Comment added (left open)")
    else:
        print(f"  #{issue_num}: Comment FAILED ({r.status_code}: {r.json().get('message', '')})")


def create_issue(title: str, body: str, labels: list[str]) -> None:
    """Create a new issue."""
    # Ensure labels exist
    for label in labels:
        requests.post(
            f"{API}/labels",
            headers=HEADERS,
            json={"name": label},
        )

    r = requests.post(
        f"{API}/issues",
        headers=HEADERS,
        json={"title": title, "body": body, "labels": labels},
    )
    if r.status_code == 201:
        num = r.json()["number"]
        print(f"  Created issue #{num}: {title}")
    else:
        print(f"  Create FAILED ({r.status_code}: {r.json().get('message', '')})")


if __name__ == "__main__":
    print(f"Target repo: {REPO}\n")

    # 1. Close #1
    print("Closing #1 (Supabase project):")
    comment_and_close(1,
        "DONE: Supabase project created in ap-southeast-1 (Singapore). "
        "Status: Healthy. Project ref: dsnydskkjwziynwmzfkh"
    )

    # 2. Close #3
    print("\nClosing #3 (36 tables migration):")
    comment_and_close(3,
        "DONE: All 36 tables created. 149/149 migration statements passed, "
        "zero failures. RLS enabled on all tables. Partial unique indexes "
        "applied for user_goals, user_locations, user_telegram_links."
    )

    # 3. Close #4
    print("\nClosing #4 (RLS policies):")
    comment_and_close(4,
        "DONE: RLS policies applied in migration 001. User isolation on all "
        "28 user tables. Admin shadow read on documents only."
    )

    # 4. Comment on #2 (leave open)
    print("\nCommenting on #2 (Auth — partial):")
    comment_only(2,
        "PARTIAL: Supabase Auth enabled. Email confirmation on. "
        "2FA and custom SMTP still pending — Day 2 task."
    )

    # 5. Create new issue
    print("\nCreating new issue (seed system_config):")
    create_issue(
        "[F-009] Seed system_config with all threshold values",
        "Seed system_config table with all default thresholds from "
        "build plan Section 4.30: alert_confidence_threshold 0.80, "
        "macro_uncertainty_threshold 0.70, alias_auto_learn_threshold 0.90, "
        "geocode_confidence_threshold 0.70, brain_max_concurrent_threads 5, "
        "invite_expiry_days 7, food_rule_expiry_job_enabled true, "
        "brain_thread_summary_after 5",
        ["type:schema", "priority:high"],
    )

    print("\nDone.")
