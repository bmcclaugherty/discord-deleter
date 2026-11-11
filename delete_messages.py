#!/usr/bin/env python3
"""Delete your own Discord messages from a DM or group DM.

Uses your own user token against Discord's private API (the same endpoints the
web client uses). You can only delete your own messages in DMs / group DMs --
Discord has no "manage messages" permission there.

Usage examples:
    # Dry-run: list matching messages without deleting anything
    python3 delete_messages.py --token TOKEN --channel-id 123... \
        --start 2024-01-01 --end 2024-06-30 --dry-run

    # Delete for real (paced to avoid rate limits)
    python3 delete_messages.py --channel-id 123... --start 2024-01-01 --end 2024-06-30

The token can also be passed via the DISCORD_TOKEN environment variable, or
left out entirely to be prompted for it.
"""

import argparse
import getpass
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DISCORD_EPOCH_MS = 1420070400000  # Discord snowflake epoch (2015-01-01T00:00:00Z)
DEFAULT_BASE = "https://discord.com/api/v9"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Message types that cannot be deleted (system messages).
UNDELETABLE_TYPES = {1, 2, 3, 4, 5, 21}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    pass


def api_request(method, url, token, timeout=30):
    """Perform a request and return (status, parsed_json_or_None)."""
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", token)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "*/*")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read()
    except urllib.error.URLError as e:
        raise ApiError(f"network error: {e.reason}") from e

    data = None
    if body:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
    return status, data


def get_self_user(token, base):
    status, data = api_request("GET", f"{base}/users/@me", token)
    if status == 401:
        raise ApiError("token rejected (401). It may be invalid or expired.")
    if status != 200 or not data:
        raise ApiError(f"could not resolve your user id (HTTP {status}).")
    return data["id"], data.get("username")


# --------------------------------------------------------------------------- #
# Snowflake / date helpers
# --------------------------------------------------------------------------- #
def date_to_snowflake(ms):
    return (int(ms) - DISCORD_EPOCH_MS) << 22


def snowflake_to_ms(sf):
    return (int(sf) >> 22) + DISCORD_EPOCH_MS


def parse_date(value):
    """Parse YYYY-MM-DD or YYYY-MM-DD HH:MM[+/-offset] as UTC ms."""
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def fmt_snowflake_ts(sf):
    return datetime.fromtimestamp(snowflake_to_ms(sf) / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# --------------------------------------------------------------------------- #
# Message discovery
# --------------------------------------------------------------------------- #
def message_label(msg):
    content = (msg.get("content") or "").replace("\n", " ").strip()
    if not content:
        if msg.get("attachments"):
            content = "[attachment]"
        elif msg.get("embeds"):
            content = "[embed]"
        else:
            content = "[no text]"
    if len(content) > 80:
        content = content[:77] + "..."
    return content


def iter_search_messages(token, base, channel_id, author_id, min_id, max_id,
                         search_delay, log):
    """Yield message dicts from the DM search endpoint, oldest first."""
    url = f"{base}/channels/{channel_id}/messages/search"
    cursor_min_id = min_id
    page = 0

    while True:
        params = {
            "author_id": author_id,
            "sort_by": "timestamp",
            "sort_order": "asc",
            "limit": "25",
        }
        if cursor_min_id:
            params["min_id"] = str(cursor_min_id)
        if max_id:
            params["max_id"] = str(max_id)
        query = urllib.parse.urlencode(params)

        status, data = api_request("GET", f"{url}?{query}", token)

        # Index still building: wait and retry the same page.
        if status == 202 and data and data.get("code") == 110000:
            wait = float(data.get("retry_after") or 2)
            log(f"[WARN] search index not ready, waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        if status == 401:
            raise ApiError("token rejected (401) during search.")
        if status == 403:
            raise ApiError("forbidden (403) searching channel -- check the channel id.")
        if status == 404:
            raise ApiError("channel not found (404) -- check the channel id.")
        if status != 200 or not data:
            raise ApiError(f"search failed (HTTP {status}).")

        groups = data.get("messages") or []
        messages = [g[0] for g in groups if g]
        if not messages:
            return

        page += 1
        log(f"[INFO] search page {page}: {len(messages)} message(s) "
            f"({fmt_snowflake_ts(messages[0]['id'])} -> "
            f"{fmt_snowflake_ts(messages[-1]['id'])})")

        last_id = messages[-1]["id"]
        for msg in messages:
            if msg.get("type") in UNDELETABLE_TYPES:
                continue
            yield msg

        # Advance the cursor past the last message we just read.
        cursor_min_id = last_id
        time.sleep(search_delay)


def iter_scan_messages(token, base, channel_id, author_id, min_id, max_id, log):
    """Yield own message dicts by walking channel history backwards (newest first)."""
    url = f"{base}/channels/{channel_id}/messages"
    cursor = max_id  # 'before' this id
    page = 0

    while True:
        params = {"limit": "100"}
        if cursor:
            params["before"] = str(cursor)
        query = urllib.parse.urlencode(params)

        status, data = api_request("GET", f"{url}?{query}", token)
        if status == 401:
            raise ApiError("token rejected (401) during scan.")
        if status == 403:
            raise ApiError("forbidden (403) scanning channel -- check the channel id.")
        if status == 404:
            raise ApiError("channel not found (404) -- check the channel id.")
        if status != 200:
            raise ApiError(f"history scan failed (HTTP {status}).")
        if not isinstance(data, list) or not data:
            return

        page += 1
        oldest_ts = snowflake_to_ms(data[-1]["id"])
        log(f"[INFO] scan page {page}: {len(data)} message(s), oldest "
            f"{fmt_snowflake_ts(data[-1]['id'])}")

        # Messages arrive newest-first; keep only ours within the date window.
        ours = [
            m for m in data
            if str(m.get("author", {}).get("id")) == author_id
            and m.get("type") not in UNDELETABLE_TYPES
            and (min_id is None or int(m["id"]) >= int(min_id))
            and (max_id is None or int(m["id"]) <= int(max_id))
        ]
        yield from ours

        # Stop once the whole page is older than our start bound.
        if min_id is not None and int(data[-1]["id"]) < int(min_id):
            return
        cursor = data[-1]["id"]


# --------------------------------------------------------------------------- #
# Deletion
# --------------------------------------------------------------------------- #
def pacing_delay(delay, jitter):
    factor = 1 + random.uniform(-jitter, jitter)
    return max(0.5, delay * factor)


def delete_messages(messages, token, base, channel_id, args, log, resume_ids):
    deleted = 0
    already_gone = 0
    skipped = []
    consecutive_failures = 0

    for i, msg in enumerate(messages, 1):
        if args.max and deleted >= args.max:
            log(f"[INFO] reached --max {args.max}, stopping.")
            break

        msg_id = msg["id"]
        if resume_ids and msg_id in resume_ids:
            continue

        # Pace between deletes (skip the pause before the very first one).
        if i > 1:
            wait = pacing_delay(args.delay, args.jitter)
            if (i - 1) % 50 == 0:
                wait += 3
                log("[INFO] breather: extra 3s pause to avoid rate limits")
            time.sleep(wait)

        status = None
        data = None
        for attempt in range(1, args.max_retries + 1):
            status, data = api_request(
                "DELETE", f"{base}/channels/{channel_id}/messages/{msg_id}", token
            )
            if status == 204:
                break
            if status == 429:
                wait = float((data or {}).get("retry_after") or 1) + 1
                if (data or {}).get("global"):
                    wait = max(wait, 300)
                log(f"[WARN] rate limited (429), waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            if status in (500, 502, 503, 504):
                backoff = 2 ** attempt
                log(f"[WARN] HTTP {status} on {msg_id}, retrying in {backoff}s "
                    f"({attempt}/{args.max_retries})")
                time.sleep(backoff)
                continue
            break  # 4xx (other than 429) is final

        if status == 204:
            deleted += 1
            consecutive_failures = 0
            if resume_ids is not None:
                resume_ids.add(msg_id)
                append_resume(msg_id)
            log(f"[OK] deleted {msg_id} ({deleted} so far)")
        elif status == 404 and (data or {}).get("code") == 10008:
            already_gone += 1
            consecutive_failures = 0
            log(f"[OK] {msg_id} already gone")
        elif status == 401:
            log("[ERR] token rejected (401). Aborting.")
            raise ApiError("token rejected (401) during delete.")
        else:
            consecutive_failures += 1
            skipped.append((msg_id, status, (data or {}).get("message")))
            log(f"[WARN] skipped {msg_id} (HTTP {status})")

        if consecutive_failures >= 5:
            log("[ERR] 5 consecutive failures; stopping to avoid hammering the API.")
            break

    return {"deleted": deleted, "already_gone": already_gone, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Resume support
# --------------------------------------------------------------------------- #
RESUME_FILE = "deleted_ids.txt"


def load_resume():
    if not os.path.exists(RESUME_FILE):
        return set()
    with open(RESUME_FILE) as f:
        return {line.strip() for line in f if line.strip()}


def append_resume(msg_id):
    with open(RESUME_FILE, "a") as f:
        f.write(msg_id + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args():
    p = argparse.ArgumentParser(
        description="Delete your own Discord messages from a DM or group DM.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--token", help="Discord user token (or DISCORD_TOKEN env var).")
    p.add_argument("--channel-id", required=True, help="DM / group DM channel id.")
    p.add_argument("--start", help="Begin date (YYYY-MM-DD or YYYY-MM-DD HH:MM, UTC).")
    p.add_argument("--end", help="End date (YYYY-MM-DD or YYYY-MM-DD HH:MM, UTC).")
    p.add_argument("--base", default=DEFAULT_BASE, help="API base URL.")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Seconds between deletes.")
    p.add_argument("--jitter", type=float, default=0.3,
                   help="Randomize delay by +/- this fraction.")
    p.add_argument("--search-delay", type=float, default=10.0,
                   help="Seconds between search pages.")
    p.add_argument("--max-retries", type=int, default=3,
                   help="Retries per message on 5xx/429.")
    p.add_argument("--max", type=int, default=None,
                   help="Delete at most this many messages.")
    p.add_argument("--scan", action="store_true",
                   help="Use history scan instead of search (slower, but finds "
                        "messages the search index misses).")
    p.add_argument("--no-resume", action="store_true",
                   help="Ignore/do not write the deleted_ids.txt resume file.")
    p.add_argument("--dry-run", action="store_true",
                   help="List matching messages and exit without deleting.")
    return p.parse_args()


def main():
    args = parse_args()

    token = args.token or os.environ.get("DISCORD_TOKEN")
    if not token:
        try:
            token = getpass.getpass("Discord token: ")
        except (EOFError, KeyboardInterrupt):
            print("error: no token provided (use --token or DISCORD_TOKEN).",
                  file=sys.stderr)
            return 1
    token = token.strip()
    if not token:
        print("error: no token provided", file=sys.stderr)
        return 1

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    try:
        user_id, username = get_self_user(token, args.base)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    log(f"[INFO] operating as {username} ({user_id})")

    min_id = date_to_snowflake(parse_date(args.start)) if args.start else None
    max_id = date_to_snowflake(parse_date(args.end)) if args.end else None
    if args.start:
        log(f"[INFO] start: {args.start} -> min_id {min_id}")
    if args.end:
        log(f"[INFO] end:   {args.end} -> max_id {max_id}")

    resume_ids = None if args.no_resume else load_resume()

    def make_iterator():
        if args.scan:
            return iter_scan_messages(token, args.base, args.channel_id, user_id,
                                     min_id, max_id, log)
        return iter_search_messages(token, args.base, args.channel_id, user_id,
                                    min_id, max_id, args.search_delay, log)

    if args.dry_run:
        count = 0
        print(f"Messages that would be deleted from channel {args.channel_id}:")
        for msg in make_iterator():
            if args.max and count >= args.max:
                break
            print(f"  {msg['id']}  {msg.get('timestamp', '').replace('T', ' ')[:19]}"
                  f"  {message_label(msg)}")
            count += 1
        print(f"\nTotal: {count} message(s) match.")
        return 0

    log(f"[INFO] deleting messages (mode: {'scan' if args.scan else 'search'})")
    start_time = time.time()

    try:
        summary = delete_messages(make_iterator(), token, args.base,
                                  args.channel_id, args, log, resume_ids)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    elapsed = time.time() - start_time
    print("\n=== Summary ===")
    print(f"Deleted:       {summary['deleted']}")
    print(f"Already gone:  {summary['already_gone']}")
    print(f"Skipped:       {len(summary['skipped'])}")
    if summary["skipped"]:
        for msg_id, status, reason in summary["skipped"]:
            print(f"  - {msg_id}: HTTP {status} {reason or ''}")
    print(f"Elapsed:       {elapsed:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
