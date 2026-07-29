# SimpleMMO Discord Monitor Bot

A Discord bot that monitors [SimpleMMO](https://web.simple-mmo.com/) guild events —
raids, orphanage progress, world bosses, and guild tasks — and posts notifications
to a Discord channel in real time. It also exposes slash commands to check the
current status on demand.

## Features

- **Raid alerts** — notifies the channel when a new guild raid starts (with
  optional role ping), and sends a reminder shortly before it expires.
- **Orphanage tracking** — notifies when a new orphanage tier becomes active.
- **World boss alerts** — notifies when a world boss becomes active and when
  it's defeated, including live HP.
- **Guild task tracking** — notifies when a new guild task appears and when
  the current one is completed, with a progress bar.
- **Slash commands** to check the current status of any of the above at any time.
- **State persistence** — remembers what's already been notified across
  restarts (`bot_state.json`), so you won't get duplicate notifications.
- **Rate limiting** — stays under SimpleMMO's API limit (40 requests/minute)
  with a safety margin.
- **Resilience** — the background monitor loop won't crash on unexpected
  errors; API auth failures (invalid/expired key) trigger a one-time alert
  in the channel instead of failing silently.

## Requirements

- Python 3.12+
- A Discord bot application ([Discord Developer Portal](https://discord.com/developers/applications))
- A SimpleMMO account with API access ([API docs](https://web.simple-mmo.com/p-api/home))

## Installation

```bash
git clone <this-repo-url>
cd simple-mmo-bot
python3 -m venv venv
source venv/bin/activate  # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

If you don't have a `requirements.txt` yet, the bot depends on:

```
discord.py
aiohttp
python-dotenv
```

## Configuration

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_discord_bot_token
API_KEY=your_simplemmo_api_key
CHANNEL_ID=123456789012345678
GUILD_ID=1234
RAID_ROLE_ID=123456789012345678
```

| Variable        | Required | Description                                                                 |
|-----------------|----------|-------------------------------------------------------------------------------|
| `DISCORD_TOKEN` | Yes      | Your Discord bot's token.                                                    |
| `API_KEY`       | Yes      | Your SimpleMMO API key.                                                     |
| `CHANNEL_ID`    | Yes      | ID of the **text channel** where notifications will be posted.              |
| `GUILD_ID`      | Yes      | Your in-game SimpleMMO guild ID (used for raid/task endpoints).             |
| `RAID_ROLE_ID`  | No       | ID of a role to ping when a new raid starts. If unset, no role is pinged.   |

The bot validates these on startup and will refuse to start with a clear
error message if anything required is missing or malformed.

### Discord bot setup notes

- The bot only uses slash commands, so the **message content intent is not
  required**. You can ignore the related startup warning.
- Voice support is not used, so `PyNaCl` warnings on startup can be ignored.
- Make sure the bot has permission to **send messages and embeds** in the
  configured channel.

## Running the bot

```bash
python bot.py
```

On first connect, slash commands are synced automatically (only once per
process, not on every reconnect).

## Slash commands

| Command       | Description                                      |
|---------------|---------------------------------------------------|
| `/raid`       | Show the current guild raid status.               |
| `/orphanage`  | Show the current orphanage status.                |
| `/task`       | Show the current guild task status with progress. |
| `/worldboss`  | Show currently active world bosses.               |
| `/status`     | Show bot health: uptime info, guild ID, API usage. |
| `/uptime`     | Show how long the bot has been running.            |

## How it works

A background loop (`monitor()`) runs every minute and:

1. Polls the SimpleMMO API for raid, orphanage, world boss, and guild task data.
2. Compares the results against the last known state (persisted in
   `bot_state.json`) to detect what's actually new.
3. Sends a Discord embed notification for any new event.

State is persisted to disk so restarts don't cause duplicate notifications,
and the monitor loop is wrapped so that an unexpected error on a single tick
is logged (with full traceback) but doesn't stop future checks.

## Tuning

A few constants near the top of `bot.py` can be adjusted if needed:

- `MAX_REQUESTS_PER_MINUTE` — safety margin under SimpleMMO's 40 req/min limit.
- `RAID_REMINDER_MINUTES_BEFORE` — how long before a raid expires to send the reminder.
- `AUTH_FAILURE_THRESHOLD` — how many consecutive 401 errors before the bot
  warns the channel that the API key may be invalid.

## Logging

Logs are written both to the console and to `bot.log`, in English, at INFO
level by default (errors and warnings are logged with more detail,
including full tracebacks for unexpected exceptions).

## Known limitations / roadmap

- No thumbnails/icons on embeds yet (pending confirmation the SimpleMMO API
  exposes icon URLs for bosses/tiers).
- No direct links to raid locations.
- No alerting on prolonged network/API outages beyond auth (401) failures.
