"""
Sync script (users repo): after problems sync, read sync-batch.json (one API
call), re-fetch the same sheet snapshot, look up url/index by sheet_key, and
append pointer records into users/<encoded-email>/data.json.

Runs AFTER the problems repo sync finishes (repository_dispatch).

Environment variables:
    SHEET_URLS          - comma-separated Apps Script GET endpoints
                          (must match problems repo, same order)
    STATUS_SHEET_URL    - Apps Script POST endpoint for the status logger
    PROBLEMS_REPO       - "owner/repo" (default: codecoffee-app/lc-problems-sub)
    PROBLEMS_REF        - git ref (default: master)
    PROBLEMS_READ_TOKEN - token that can read the problems repo
                          (falls back to GITHUB_TOKEN)
"""

import os
import sys
import json
import base64
import requests
from datetime import datetime, timezone
from collections import defaultdict

REPO_NAME = "users"
USERS_BASE_DIR = "users"
SYNC_BATCH_PATH = "sync-batch.json"


def email_to_folder_name(email):
    return (
        email
        .replace("_", "__UND__")
        .replace(".", "__DOT__")
        .replace("@", "__AT__")
        .replace("+", "__PLUS__")
        .replace("-", "__DASH__")
    )


def get_sheet_urls():
    raw = os.environ.get("SHEET_URLS", "")
    urls = [u.strip() for u in raw.split(",") if u.strip()]
    if not urls:
        print("ERROR: No sheet URLs found in SHEET_URLS env var.", file=sys.stderr)
        sys.exit(1)
    return urls


def fetch_sheet_rows(url):
    for attempt in (1, 2):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  Attempt {attempt} failed for {url}: {e}", file=sys.stderr)
            if attempt == 2:
                print("  Skipping this sheet after 2 failed attempts.", file=sys.stderr)
                return []


def extract_json_string(row):
    if isinstance(row, str):
        return row
    if isinstance(row, dict):
        for key in ("data", "value", "json", "submission"):
            if key in row:
                return row[key]
        return next(iter(row.values()), None)
    if isinstance(row, (list, tuple)) and row:
        return row[0]
    return None


def parse_submission(raw_json_string):
    if not raw_json_string:
        return None
    try:
        submission = json.loads(raw_json_string)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"  Skipping row - could not parse JSON: {e}", file=sys.stderr)
        return None

    required_fields = ("slug", "code", "timestamp", "email", "language", "status")
    missing = [f for f in required_fields if f not in submission]
    if missing:
        print(f"  Skipping row - missing fields {missing}", file=sys.stderr)
        return None

    return submission


def problems_repo_config():
    repo = os.environ.get("PROBLEMS_REPO") or "codecoffee-app/lc-problems-sub"
    ref = os.environ.get("PROBLEMS_REF") or "master"
    token = (
        os.environ.get("PROBLEMS_READ_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    )
    return repo, ref, token


def fetch_sync_batch():
    """
    One GitHub Contents API call for sync-batch.json written by problems sync.
    """
    repo, ref, token = problems_repo_config()
    url = f"https://api.github.com/repos/{repo}/contents/{SYNC_BATCH_PATH}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(url, headers=headers, params={"ref": ref}, timeout=30)
    except requests.RequestException as e:
        print(f"ERROR: failed to fetch {SYNC_BATCH_PATH}: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code == 404:
        print(
            f"ERROR: {SYNC_BATCH_PATH} not found in {repo}@{ref}. "
            "Did problems sync run and push?",
            file=sys.stderr,
        )
        sys.exit(1)

    if resp.status_code != 200:
        print(
            f"ERROR: GitHub API {resp.status_code} for {SYNC_BATCH_PATH}: "
            f"{resp.text[:200]}",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = resp.json()
    if payload.get("encoding") != "base64" or "content" not in payload:
        print(f"ERROR: unexpected GitHub contents response for {SYNC_BATCH_PATH}",
              file=sys.stderr)
        sys.exit(1)

    raw = base64.b64decode(payload["content"]).decode("utf-8")
    batch = json.loads(raw)
    if not isinstance(batch, dict):
        print("ERROR: sync-batch.json must be a JSON object", file=sys.stderr)
        sys.exit(1)
    return batch


def collect_user_records(sheet_urls, batch):
    """
    Re-read sheets in the same URL order as problems. For each response index,
    look up sheet_key in batch. Missing key = skipped by problems (invalid) or
    not in today's written set.
    """
    records = []
    matched = 0
    skipped_no_batch = 0
    skipped_invalid = 0

    for sheet_num, url in enumerate(sheet_urls, start=1):
        print(f"Fetching sheet {sheet_num}: {url[:40]}...")
        rows = fetch_sheet_rows(url)
        if not isinstance(rows, list):
            print("  Unexpected response type, skipping sheet.", file=sys.stderr)
            continue
        print(f"  Got {len(rows)} raw rows")

        for row_index, row in enumerate(rows):
            sheet_key = f"{sheet_num}-{row_index}"
            pointer = batch.get(sheet_key)
            if not pointer:
                skipped_no_batch += 1
                continue

            submission = parse_submission(extract_json_string(row))
            if not submission:
                skipped_invalid += 1
                continue

            if "url" not in pointer or "index" not in pointer:
                print(
                    f"  Skipping {sheet_key} - batch entry missing url/index",
                    file=sys.stderr,
                )
                skipped_invalid += 1
                continue

            records.append({
                "email": submission["email"],
                "slug": submission["slug"],
                "status": submission["status"],
                "timestamp": int(submission["timestamp"]),
                "language": submission["language"],
                "url": pointer["url"],
                "index": pointer["index"],
            })
            matched += 1

    print(
        f"\nBatch keys: {len(batch)}, matched: {matched}, "
        f"no-batch-key: {skipped_no_batch}, invalid: {skipped_invalid}"
    )
    if matched != len(batch):
        print(
            f"WARNING: matched {matched} rows but batch has {len(batch)} keys. "
            "Some sheet_keys may not have been found in today's sheet responses.",
            file=sys.stderr,
        )
    return records


def load_user_data(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        if "submissions" not in data or not isinstance(data["submissions"], dict):
            data["submissions"] = {}
        return data
    return {"submissions": {}}


def save_user_data(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def to_user_record(record):
    return {
        "url": record["url"],
        "status": record["status"],
        "timestamp": record["timestamp"],
        "index": record["index"],
        "language": record["language"],
    }


def write_all_user_submissions(records):
    by_email = defaultdict(list)
    for record in records:
        by_email[record["email"]].append(record)

    for email, subs in by_email.items():
        subs.sort(key=lambda s: s["timestamp"])

        folder = email_to_folder_name(email)
        data_path = os.path.join(USERS_BASE_DIR, folder, "data.json")
        data = load_user_data(data_path)

        for s in subs:
            data["submissions"].setdefault(s["slug"], []).append(to_user_record(s))

        save_user_data(data_path, data)
        print(f"  [{folder}] appended {len(subs)} submission(s)")


def get_today_date_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def log_sync_status():
    status_url = os.environ.get("STATUS_SHEET_URL")
    if not status_url:
        print("STATUS_SHEET_URL not set - skipping status log.", file=sys.stderr)
        return

    payload = {"date": get_today_date_str(), "repo": REPO_NAME}
    try:
        resp = requests.post(status_url, json=payload, timeout=30)
        resp.raise_for_status()
        print(f"Logged sync status: repo={REPO_NAME}, date={payload['date']}")
    except requests.RequestException as e:
        print(f"WARNING: failed to log sync status: {e}", file=sys.stderr)


def main():
    sheet_urls = get_sheet_urls()
    print(f"Found {len(sheet_urls)} sheet URL(s) to sync.\n")

    print(f"Fetching {SYNC_BATCH_PATH} from problems repo...")
    batch = fetch_sync_batch()
    print(f"  Got {len(batch)} batch entr(ies)\n")

    if not batch:
        print("Empty batch - nothing to write for users.")
        log_sync_status()
        return

    records = collect_user_records(sheet_urls, batch)

    print("\nWriting user data.json files...")
    write_all_user_submissions(records)

    log_sync_status()


if __name__ == "__main__":
    main()
