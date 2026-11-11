# discord-deleter

Delete your **own** Discord messages from a DM or group DM, filtered by date
range, using Discord's private user API (the same endpoints the web client
uses).

> **⚠️ Important limitations**
>
> - In DMs and group DMs you can **only delete your own messages**. Discord has
>   no "manage messages" permission in private chats, so there is no way to
>   delete *another* user's messages there.
> - This uses your personal user token against Discord's private API. That is
>   against Discord's Terms of Service (the same trade-off made by tools like
>   Discrub and psibean's `discord-message-deleter`). Use at your own risk.
> - Deletion is **irreversible**. Use `--dry-run` first.

## Requirements

- Python 3.8+ (standard library only — no `pip install` needed).

## Getting your token

1. Open [discord.com/app](https://discord.com/app) in your browser and log in.
2. Press `F12` to open DevTools and go to the **Network** tab.
3. Click any channel or search so Discord makes a request, then pick a request
   to `discord.com/api`.
4. Under **Request Headers**, copy the value of `authorization` (a long string,
   no `Bot ` prefix).

Alternatively, in the browser console: `JSON.parse(localStorage.token)`.

Treat the token like a password — it grants full access to your account. It
never leaves your machine, and this script sends requests only to Discord.

## Finding your channel ID

Enable Developer Mode (Discord Settings → Advanced), then right-click the DM or
group DM in the sidebar → **Copy Channel ID**. Note: this is the **channel**
ID, not the other user's ID.

## Usage

```bash
# Preview what would be deleted (no changes made):
python3 delete_messages.py --channel-id 123456789 --start 2024-01-01 --end 2024-06-30 --dry-run

# Delete for real:
python3 delete_messages.py --channel-id 123456789 --start 2024-01-01 --end 2024-06-30

# No date bounds = everything of yours in the chat:
python3 delete_messages.py --channel-id 123456789

# Cap how many get deleted (safety):
python3 delete_messages.py --channel-id 123456789 --max 50
```

The token can be passed with `--token`, via the `DISCORD_TOKEN` environment
variable, or you'll be prompted for it.

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--channel-id` | *(required)* | DM / group DM channel id |
| `--start` / `--end` | none | Date window (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM`, UTC) |
| `--token` | env/prompt | Discord user token |
| `--base` | `https://discord.com/api/v9` | API base (try `https://canary.discord.com/api/v9` if needed) |
| `--delay` | `2.0` | Seconds between deletes |
| `--jitter` | `0.3` | Randomize the delete delay by ±30% |
| `--search-delay` | `10.0` | Seconds between search pages |
| `--max` | none | Delete at most this many messages |
| `--scan` | off | Walk channel history instead of search (slower, finds what the search index misses) |
| `--dry-run` | off | List matches and exit |
| `--no-resume` | off | Don't use `deleted_ids.txt` |

## Rate limiting

User tokens don't return reliable `X-RateLimit-*` headers, so the tool **paces
itself** rather than trusting headers:

- ~2s (jittered) between deletes, with a +3s breather every 50 deletes.
- ~10s between search pages.
- On `429`, it honors `retry_after` (≥5 min for global limits) and retries.
- On `5xx`, it retries with exponential backoff (3 attempts).
- 5 consecutive failures stops the run so it doesn't hammer the API.

Defaults mirror the pacing used by Discrub/psibean. If you're doing a very
large range, consider raising `--delay` toward 3–5s to be extra safe.

## Resume

Deleted message IDs are appended to `deleted_ids.txt` in the working
directory, so a re-run skips messages it already deleted. Use `--no-resume` to
disable this.

## How it works

1. `GET /users/@me` → your user ID (the `author_id` for every search).
2. `--start` / `--end` are converted to Discord snowflake IDs (`min_id`/`max_id`).
3. Search (`GET /channels/{id}/messages/search`) returns your messages in the
   date window, paginated by advancing `min_id`. System messages (types
   1,2,3,4,5,21) are skipped.
4. Each message is deleted with `DELETE /channels/{id}/messages/{id}`.
# discord-deleter
# discord-deleter
