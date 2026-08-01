<div align="center">

# ⚔️ SimpleMMO Discord Monitor Bot

**Real-time raid, orphanage, world boss, and guild task alerts — straight into your Discord server.**

![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?style=for-the-badge&logo=discord&logoColor=white)
![aiohttp](https://img.shields.io/badge/async-aiohttp-2C5BB4?style=for-the-badge&logo=aiohttp&logoColor=white)
![Status](https://img.shields.io/badge/status-active-brightgreen?style=for-the-badge)
![Rate Limited](https://img.shields.io/badge/API-rate--limited-yellow?style=for-the-badge)

</div>

---

A Discord bot that monitors [SimpleMMO](https://web.simple-mmo.com/) guild events —
raids, orphanage progress, world bosses, and guild tasks — and posts notifications
to a Discord channel in real time. It also exposes slash commands to check the
current status on demand, and is built to run unattended for long stretches
(persisted state, rate limiting, graceful shutdown, structured logs).

## 📑 Table of Contents

- [Features](#-features)
- [What it looks like](#-what-it-looks-like)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the bot](#running-the-bot)
- [Slash commands](#-slash-commands)
- [How it works](#how-it-works)
- [Tuning](#tuning)
- [Logging](#logging)
- [Known limitations / roadmap](#known-limitations--roadmap)

## ✨ Features

| | |
|---|---|
| ⚔️ **Raid alerts** | Notifies the channel when a new guild raid starts (with optional role ping), and sends a one-time reminder shortly before it expires. |
| 🏠 **Orphanage tracking** | Notifies when a new orphanage tier becomes active. |
| 🔥 **World boss alerts** | Notifies when a boss becomes active and when it's defeated (with live HP) — *and* warns the channel ahead of time when a boss is about to spawn. |
| 📋 **Guild task tracking** | Notifies when a new guild task appears and when the current one is completed, with a progress bar. |
| 🎮 **Slash commands** | Check the current status of anything above, on demand, with a short per-user cooldown to prevent spam. |
| 💾 **State persistence** | Remembers what's already been notified across restarts (`bot_state.json`), including in-progress reminders — no duplicate pings just because the bot restarted. Writes are atomic, so a crash mid-write can't corrupt the file. |
| 🐢 **Rate limiting** | Stays under SimpleMMO's 40 req/min API limit with a safety margin, and every call has a timeout so a stalled response can't hang the monitor loop. |
| 🛡️ **Resilience** | The monitor loop won't crash on unexpected errors; auth failures are tracked *per endpoint* and trigger a one-time channel alert instead of failing silently. |
| 🧹 **Graceful shutdown** | Responds to Ctrl+C and to `docker stop` / `systemctl stop` by finishing cleanly: stopping the monitor, saving state, and closing the HTTP session. |
| 📝 **Structured logs** | Human-readable in the console, JSON lines in `bot.log`. API keys are never written to disk, even inside error messages. |

## 📸 What it looks like

Every embed the bot sends uses a color to signal what kind of event it is:

| Color | Meaning |
|:---:|---|
| 🟥 Red | New event / alert (raid started, auth failure) |
| 🟧 Orange | Active now, or a heads-up before something happens |
| 🟩 Green | Something completed successfully (orphanage active, task done) |
| 🟦 Blue | Informational status (task info, bot status) |
| ⬜ Grey | Something ended (world boss defeated) |

The mockups below match the bot's actual embeds field-for-field — this is what you'll really see in the channel.

<details open>
<summary><strong>🔔 Automatic notifications</strong></summary>

<br>

**New raid detected** — posted the moment a raid starts, pings `RAID_ROLE_ID` if configured:

> 🟥 **⚔️ Raid Started!**
> A new guild raid has started!
>
> **📍 Locations**
> Frozen Peaks
> Dragon's Lair
>
> **⏰ Expires**
> in 2 hours
>
> <sub>SimpleMMO Monitor • Today at 14:32</sub>

**Raid about to expire** — sent once, `RAID_REMINDER_MINUTES_BEFORE` (default 10) minutes out:

> 🟧 **⏰ Raid Expiring Soon!**
> The current raid is about to expire — get in before it's gone!
>
> **Expires**
> in 8 minutes
>
> <sub>SimpleMMO Monitor • Today at 16:22</sub>

**Orphanage tier activated:**

> 🟩 **🏠 Orphanage Active**
> **Gold Tier** is now active!
>
> **✨ Effects**
> +10% EXP gain
> +5% Gold drop
>
> **📊 Progress**
> 78%
>
> <sub>SimpleMMO Monitor • Today at 09:14</sub>

**World boss becomes active:**

> 🟧 **🔥 World Boss Active!**
> **Evil Knight** (Lv. 240) is now available!
>
> **❤️ HP**
> 2.4M / 2.4M (100%)
>
> <sub>SimpleMMO Monitor • Today at 18:00</sub>

**World boss defeated:**

> ⬜ **💀 World Boss Defeated!**
> **Evil Knight** (Lv. 240) has been taken down!
>
> <sub>SimpleMMO Monitor • Today at 18:47</sub>

**World boss about to spawn** — sent once, `WORLDBOSS_REMINDER_MINUTES_BEFORE` (default 60) minutes out:

> 🟧 **⏳ World Boss Incoming!**
> **Bees** (Lv. 951) will spawn soon!
>
> **🕐 Spawns**
> in 42 minutes
>
> <sub>SimpleMMO Monitor • Today at 20:18</sub>

**New guild task:**

> 🟦 **📋 New Guild Task!**
> Type: **Travel**
>
> **🎯 Target**
> 30,000
>
> **📊 Progress**
> 4,200 / 30,000 (14%)
> `██▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒`
>
> **🎁 Reward**
> 1.2K EXP + 500 Power Points
>
> <sub>SimpleMMO Monitor • Today at 06:00</sub>

**Guild task completed:**

> 🟩 **✅ Guild Task Completed!**
> The **Travel** task has been completed!
>
> **🎁 Reward**
> 1.2K EXP + 500 Power Points
>
> <sub>SimpleMMO Monitor • Today at 11:35</sub>

**Repeated auth failure on a specific endpoint:**

> 🟥 **🚨 API Authentication Failing**
> The bot has failed to authenticate with the SimpleMMO API on `/v1/guilds/raid/1234` 3 times in a row. The API key may be invalid or expired — please check the `.env` configuration.
>
> <sub>SimpleMMO Monitor • Today at 03:02</sub>

</details>

<details open>
<summary><strong>🎮 Slash command replies</strong></summary>

<br>

**`/raid`** — mirrors the "Raid Started" embed above when one is active, or:
> ℹ️ No active guild raid right now. This is the real data from the API — it's just empty.

**`/orphanage`** — mirrors the "Orphanage Active" embed above, or, if nothing is active:
> ℹ️ No orphanage tier is currently active. Closest is **Silver Tier** at 63% progress.

**`/task`:**

> 🟦 **📋 Guild Task Status**
>
> **Type**
> Travel
>
> **Progress**
> 4,200 / 30,000 (14%)
>
> **Bar**
> `██▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒`
>
> **🎁 Reward**
> 1.2K EXP + 500 Power Points
>
> <sub>SimpleMMO Monitor • Today at 12:04</sub>

**`/worldboss`** — active bosses plus the next spawn, all in one embed:

> 🟧 **🔥 Active World Bosses**
>
> **One Above All (Lv. 396)**
> HP: 3.8M / 3.8M (100%)
>
> **Bees (Lv. 951)**
> HP: 8.4M / 8.4M (100%)
>
> **⏳ Next World Boss**
> Soon Boss (Lv. 50) — spawns in 30 minutes
>
> <sub>Today at 13:00</sub>

**`/nextbosses`** (no `count`, or `count: 1`) — single boss, reuses the "World Boss Incoming" embed. With `count > 1`:

> 🟧 **⏳ Upcoming World Bosses (3)**
>
> **Soon Boss (Lv. 50)**
> Spawns in 30 minutes
>
> **Future Boss (Lv. 100)**
> Spawns in 1 hour
>
> **Later Boss (Lv. 20)**
> Spawns in 2 hours
>
> <sub>Today at 13:00</sub>

**`/status`:**

> 🟦 **🤖 SimpleMMO Bot Status**
>
> **Status**
> 🟢 Online
>
> **Guild ID**
> 1234
>
> **API Requests (last minute)**
> 12/35
>
> **Last Check**
> 8 seconds ago
>
> <sub>Today at 13:00</sub>

**`/uptime`:**

> 🟢 Bot online since 3 days ago (Mon, 28 Jul 2026 09:14)

**Command on cooldown / error handling:**

> ⏳ This command is on cooldown, try again in 6.2s.

</details>

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
|-----------------|:--------:|-------------------------------------------------------------------------------|
| `DISCORD_TOKEN` | ✅ Yes   | Your Discord bot's token.                                                    |
| `API_KEY`       | ✅ Yes   | Your SimpleMMO API key.                                                     |
| `CHANNEL_ID`    | ✅ Yes   | ID of the **text channel** where notifications will be posted.              |
| `GUILD_ID`      | ✅ Yes   | Your in-game SimpleMMO guild ID (used for raid/task endpoints).             |
| `RAID_ROLE_ID`  | ⬜ No    | ID of a role to ping when a new raid starts. If unset, no role is pinged.   |

The bot validates these on startup and will refuse to start with a clear
error message if anything required is missing or malformed.

### Discord bot setup notes

- The bot only uses slash commands, so the **message content intent is not
  required**. You can ignore the related startup warning.
- Voice support is not used, so `PyNaCl`/`davey` warnings on startup can be ignored.
- Make sure the bot has permission to **send messages and embeds** in the
  configured channel, and that `CHANNEL_ID` actually points to a text channel
  (the bot checks this and logs an error otherwise instead of crashing).

## Running the bot

```bash
python bot.py
```

On first connect, slash commands are synced automatically (only once per
process, not on every reconnect).

To stop the bot cleanly, use Ctrl+C or send it SIGTERM (e.g. `docker stop`,
`systemctl stop`) — it finishes the current tick, saves state, and closes
its connections before exiting instead of dropping everything mid-write.

## 🎮 Slash commands

| Command                | Description                                                  |
|-------------------------|---------------------------------------------------------------|
| `/raid`                 | Show the current guild raid status.                          |
| `/orphanage`             | Show the current orphanage status.                            |
| `/task`                  | Show the current guild task status with progress.             |
| `/worldboss`             | Show currently active world bosses, plus the next spawn.      |
| `/nextbosses [count]`    | Show the next world boss to spawn, or the next `count` (up to 15) if given. |
| `/status`                | Show bot health: uptime info, guild ID, API usage.             |
| `/uptime`                | Show how long the bot has been running.                        |

API-backed commands (`/raid`, `/orphanage`, `/task`, `/worldboss`,
`/nextbosses`) have a 15-second per-user cooldown to keep any one person
from spamming SimpleMMO API calls through the bot.

## How it works

A background loop (`monitor()`) runs every minute and:

1. Polls the SimpleMMO API for raid, orphanage, world boss, and guild task data.
2. Compares the results against the last known state (persisted in
   `bot_state.json`) to detect what's actually new — including raids/bosses
   about to expire or spawn, not just brand-new events.
3. Sends a Discord embed notification for any new event.

State is persisted to disk (via an atomic write, so a crash can't leave a
corrupted file) so restarts don't cause duplicate notifications. The monitor
loop is wrapped so an unexpected error on a single tick is logged (with full
traceback) but doesn't stop future checks, and repeated authentication
failures on a specific SimpleMMO endpoint trigger a one-time channel alert
naming that endpoint.

## Tuning

A few constants near the top of `bot.py` can be adjusted if needed:

| Constant | Default | What it controls |
|---|:---:|---|
| `MAX_REQUESTS_PER_MINUTE` | 35 | Safety margin under SimpleMMO's 40 req/min limit. |
| `HTTP_TIMEOUT_SECONDS` | 15 | How long to wait for a single API call before giving up. |
| `RAID_REMINDER_MINUTES_BEFORE` | 10 | How long before a raid expires to send the reminder. |
| `WORLDBOSS_REMINDER_MINUTES_BEFORE` | 60 | How long before a world boss spawns to send the "incoming" reminder. |
| `AUTH_FAILURE_THRESHOLD` | 3 | Consecutive 401s on the same endpoint before the bot warns the channel. |

The per-user command cooldown (15 seconds) is set directly on each slash
command's decorator in `bot.py` rather than as a top-level constant.

## Logging

The console prints plain, human-readable lines (as before). `bot.log`
now writes one JSON object per line — `timestamp`, `level`, `logger`,
`message`, and `exception` (with traceback) when relevant — so logs can be
piped into external tooling without extra parsing. In both, the SimpleMMO
API key is stripped from any request URL that ends up in an error message,
so it's never written to disk.

## Known limitations / roadmap

- No thumbnails/icons on embeds yet (pending confirmation the SimpleMMO API
  exposes icon URLs for bosses/tiers).
- No direct links to raid locations.
- No alerting on prolonged network/API outages beyond per-endpoint
  authentication (401) failures — a persistently unreachable API (timeouts,
  5xx errors) is logged every tick but doesn't yet trigger a channel alert.
- `/nextbosses` and the "boss incoming" reminder only know about bosses the
  SimpleMMO API currently reports an `enable_time` for; already-defeated
  bosses without a known next spawn time aren't shown.
