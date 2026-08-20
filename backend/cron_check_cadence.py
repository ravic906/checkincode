"""
Runs weekly as a Render Cron Job (see render.yaml). Cron can only express
calendar schedules, not "every 45 days", so this script does the interval
math itself: check how long it's been since the last problem batch, and
only actually trigger a new one once >=45 days have passed. A human still
reviews/approves every draft before it goes live -- see
main.py's /api/admin/problems/* endpoints.
"""

import os
import sys
from datetime import datetime, timezone

import requests

BACKEND_URL = os.environ.get("BACKEND_URL", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
CADENCE_DAYS = 45


def main():
    if not BACKEND_URL or not ADMIN_TOKEN:
        print("BACKEND_URL / ADMIN_TOKEN not set -- nothing to do.")
        sys.exit(1)

    headers = {"X-Admin-Token": ADMIN_TOKEN}

    resp = requests.get(f"{BACKEND_URL}/api/admin/cadence", headers=headers, timeout=30)
    resp.raise_for_status()
    last_generated = resp.json().get("last_batch_generated_at")

    if last_generated:
        last_dt = datetime.fromisoformat(last_generated.replace("Z", "+00:00"))
        days_since = (datetime.now(timezone.utc) - last_dt).days
        if days_since < CADENCE_DAYS:
            print(f"Last batch was {days_since} day(s) ago (< {CADENCE_DAYS}) -- skipping.")
            return

    print("Cadence elapsed (or no prior batch) -- generating a new problem batch.")
    resp = requests.post(
        f"{BACKEND_URL}/api/admin/problems/generate-batch",
        headers=headers,
        json={"count": 5},
        timeout=120,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Inserted {len(result['inserted'])} pending draft(s), skipped {len(result['skipped'])}.")
    for s in result["skipped"]:
        print(f"  skipped: {s['title']} -- {s['reason']}")


if __name__ == "__main__":
    main()
