"""
Users-repo sync: pull today's sheet batch, compute url/index from the
problems repo (already updated), and append pointer records into each
user's data.json.

Runs AFTER the problems repo sync finishes (repository_dispatch).

Environment variables:
    SHEET_URLS          - comma-separated Apps Script GET endpoints
    STATUS_SHEET_URL    - Apps Script POST endpoint for the status logger
    PROBLEMS_REPO       - "owner/repo" for lc-problems-sub
                          (default: codecoffee-app/lc-problems-sub)
    PROBLEMS_REF        - git ref to read (default: master)
    PROBLEMS_READ_TOKEN - token that can read the problems repo
                          (falls back to GITHUB_TOKEN)
    DEFAULT_LIMIT       - fallback limit if config.json is missing (default 100)
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


def email_to_folder_name(email):
    """Map an email to a filesystem-safe folder name (same rules as the app)."""
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
    """Each Apps Script row is a JSON string from column A."""
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
        print(f"  Skipping row - missing fields {missing}: {submission}", file=sys.stderr)
        return None

    return submission


def fetch_all_submissions(sheet_urls):
    all_submissions = []
    for url in sheet_urls:
        print(f"Fetching sheet: {url[:40]}...")
        rows = fetch_sheet_rows(url)
        print(f"  Got {len(rows)} raw rows")
        for row in rows:
            submission = parse_submission(extract_json_string(row))
            if submission:
                all_submissions.append(submission)
    return all_submissions


def group_by_problem(submissions):
    grouped = defaultdict(list)
    for submission in submissions:
        grouped[submission["slug"]].append(submission)

    for slug, subs in grouped.items():
        subs.sort(key=lambda s: int(s["timestamp"]))

    return grouped


def is_accepted(submission):
    return str(submission.get("status", "")).strip().lower() == "accepted"


def split_accepted_errors(submissions):
    accepted, errors = [], []
    for s in submissions:
        (accepted if is_accepted(s) else errors).append(s)
    return accepted, errors


def get_default_limit():
    return int(os.environ.get("DEFAULT_LIMIT", "100"))


# ---------------------------------------------------------------------------
# Problems-repo file reads (GitHub API — only the files we need, no full clone)
# ---------------------------------------------------------------------------

def problems_repo_config():
    # Empty strings from unset GitHub secrets must not override defaults.
    repo = os.environ.get("PROBLEMS_REPO") or "codecoffee-app/lc-problems-sub"
    ref = os.environ.get("PROBLEMS_REF") or "master"
    token = (
        os.environ.get("PROBLEMS_READ_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    )
    return repo, ref, token


def fetch_problems_json(path):
    """
    Fetch a JSON file from the problems repo via the GitHub Contents API.
    Returns the parsed JSON, or None if the file does not exist (404).
    """
    repo, ref, token = problems_repo_config()
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(url, headers=headers, params={"ref": ref}, timeout=30)
    except requests.RequestException as e:
        print(f"ERROR: failed to fetch {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code == 404:
        return None

    if resp.status_code != 200:
        print(
            f"ERROR: GitHub API {resp.status_code} for {path}: {resp.text[:200]}",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = resp.json()
    if payload.get("encoding") != "base64" or "content" not in payload:
        print(f"ERROR: unexpected GitHub contents response for {path}", file=sys.stderr)
        sys.exit(1)

    raw = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(raw)


def load_bucket_cursor(slug, outcome):
    """
    Read post-sync cursor for problems/{outcome}/{slug}/.
    outcome is "accepted" or "errors".

    Returns (current, limit, len_current_file).
    Missing config/file → fresh defaults (current=1, limit=DEFAULT, K=0).
    """
    config_path = f"problems/{outcome}/{slug}/config.json"
    config = fetch_problems_json(config_path)

    if config is None:
        return 1, get_default_limit(), 0

    current = int(config.get("current", 1))
    limit = int(config.get("limit", get_default_limit()))

    data_path = f"problems/{outcome}/{slug}/data/{current}.json"
    data = fetch_problems_json(data_path)
    k = len(data) if isinstance(data, list) else 0
    return current, limit, k


def assign_url_and_index(submissions, current, limit, k):
    """
    After-sync formula. problems has already appended these N submissions.

        old_total = (current - 1) * limit + k - N
        for i-th new row (oldest first):
            global = old_total + i
            url    = f"{global // limit + 1}.json"
            index  = global % limit

    Mutates each submission dict in place with 'url' and 'index'.
    """
    n = len(submissions)
    if n == 0:
        return

    old_total = (current - 1) * limit + k - n
    if old_total < 0:
        print(
            f"ERROR: computed old_total={old_total} "
            f"(current={current}, limit={limit}, k={k}, n={n}). "
            "Problems repo state does not match this batch.",
            file=sys.stderr,
        )
        sys.exit(1)

    for i, submission in enumerate(submissions):
        global_pos = old_total + i
        file_num = global_pos // limit + 1
        submission["url"] = f"{file_num}.json"
        submission["index"] = global_pos % limit


def enrich_with_pointers(grouped):
    """
    For each problem bucket (accepted / errors), read the problems cursor
    and attach url + index to every submission in that bucket.
    """
    for slug, submissions in grouped.items():
        accepted, errors = split_accepted_errors(submissions)

        if accepted:
            current, limit, k = load_bucket_cursor(slug, "accepted")
            print(
                f"  [{slug}/accepted] N={len(accepted)} "
                f"cursor current={current} limit={limit} k={k}"
            )
            assign_url_and_index(accepted, current, limit, k)

        if errors:
            current, limit, k = load_bucket_cursor(slug, "errors")
            print(
                f"  [{slug}/errors] N={len(errors)} "
                f"cursor current={current} limit={limit} k={k}"
            )
            assign_url_and_index(errors, current, limit, k)


# ---------------------------------------------------------------------------
# Write users/<encoded-email>/data.json
# ---------------------------------------------------------------------------

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


def to_user_record(submission):
    return {
        "url": submission["url"],
        "status": submission["status"],
        "timestamp": int(submission["timestamp"]),
        "index": submission["index"],
        "language": submission["language"],
    }


def write_all_user_submissions(grouped):
    """
    Flatten all enriched submissions, group by email, append into each
    user's data.json under submissions[slug].
    """
    by_email = defaultdict(list)
    for slug, submissions in grouped.items():
        for s in submissions:
            by_email[s["email"]].append(s)

    for email, subs in by_email.items():
        # Keep per-user append order stable: by timestamp
        subs.sort(key=lambda s: int(s["timestamp"]))

        folder = email_to_folder_name(email)
        user_dir = os.path.join(USERS_BASE_DIR, folder)
        data_path = os.path.join(user_dir, "data.json")
        data = load_user_data(data_path)

        for s in subs:
            slug = s["slug"]
            data["submissions"].setdefault(slug, []).append(to_user_record(s))

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

    submissions = fetch_all_submissions(sheet_urls)
    print(f"\nTotal valid submissions collected: {len(submissions)}")

    grouped = group_by_problem(submissions)
    print(f"Grouped into {len(grouped)} distinct problem(s)")

    print("\nResolving url/index from problems repo...")
    enrich_with_pointers(grouped)

    print("\nWriting user data.json files...")
    write_all_user_submissions(grouped)

    log_sync_status()


if __name__ == "__main__":
    main()
