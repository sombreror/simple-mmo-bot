import discord
from discord.ext import commands, tasks

import aiohttp
import asyncio
import os
import time
import json
import logging
from datetime import datetime, timezone
from collections import deque
from dotenv import load_dotenv

# ==========================================================
# CONFIGURATION
# ==========================================================

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_KEY = os.getenv("API_KEY")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
GUILD_ID_RAW = os.getenv("GUILD_ID")
RAID_ROLE_ID_RAW = os.getenv("RAID_ROLE_ID")  # optional: role to ping on raids

# Robust environment variable validation
errors = []

if DISCORD_TOKEN is None:
    errors.append("DISCORD_TOKEN missing in .env")

if API_KEY is None:
    errors.append("API_KEY missing in .env")
else:
    API_KEY = API_KEY.strip()  # remove accidental spaces/newlines

if CHANNEL_ID_RAW is None:
    errors.append("CHANNEL_ID missing in .env")
else:
    try:
        CHANNEL_ID = int(CHANNEL_ID_RAW)
    except ValueError:
        errors.append(f"CHANNEL_ID must be a valid integer, got: {CHANNEL_ID_RAW!r}")

if GUILD_ID_RAW is None:
    errors.append("GUILD_ID missing in .env")
else:
    try:
        GUILD_ID = int(GUILD_ID_RAW)
    except ValueError:
        errors.append(f"GUILD_ID must be a valid integer, got: {GUILD_ID_RAW!r}")

if errors:
    raise ValueError("\n".join(errors))

# RAID_ROLE_ID is optional: if missing or invalid, no role is pinged
RAID_ROLE_ID = None
if RAID_ROLE_ID_RAW:
    try:
        RAID_ROLE_ID = int(RAID_ROLE_ID_RAW)
    except ValueError:
        RAID_ROLE_ID = None


# ==========================================================
# LOGGING SETUP
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ==========================================================
# DISCORD SETUP
# ==========================================================

intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)


# ==========================================================
# STATE PERSISTENCE
# ==========================================================

STATE_FILE = "bot_state.json"


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


_state = load_state()

last_raid_started = _state.get("last_raid_started")
last_orphanage = _state.get("last_orphanage")
last_worldboss = _state.get(
    "last_worldboss", {}
)  # {boss_id: {"active": bool, "hp": int}}
last_guild_task_key = _state.get("last_guild_task_key")  # e.g. "travel:30000"
guild_task_completed_notified = _state.get("guild_task_completed_notified", False)

last_check_time = None
bot_start_time = time.time()
raid_reminder_sent = False  # avoids sending the expiry reminder more than once per raid
RAID_REMINDER_MINUTES_BEFORE = 10  # how long before expiry to warn
no_raid_logged = False  # avoids logging "no active raid" on every single check

# Tracks consecutive 401 (auth) failures so we can warn the channel once,
# instead of just filling up the logs silently.
consecutive_401_count = 0
auth_failure_notified = False
AUTH_FAILURE_THRESHOLD = 3


def persist_state():
    save_state(
        {
            "last_raid_started": last_raid_started,
            "last_orphanage": last_orphanage,
            "last_worldboss": last_worldboss,
            "last_guild_task_key": last_guild_task_key,
            "guild_task_completed_notified": guild_task_completed_notified,
        }
    )


# ==========================================================
# RATE LIMITER
# ==========================================================

# The SimpleMMO API has a real limit of 40 requests/minute
# (see the "x-ratelimit-limit: 40" header in responses).
# We keep a safety margin.
MAX_REQUESTS_PER_MINUTE = 35

request_times = deque()


def _prune_request_times():
    """Drop request timestamps older than 60s. Shared by the rate limiter
    and /status, so the reported count is always fresh."""
    now = time.time()
    while request_times and request_times[0] < now - 60:
        request_times.popleft()


async def rate_limit():
    _prune_request_times()

    if len(request_times) >= MAX_REQUESTS_PER_MINUTE:
        wait_time = 60 - (time.time() - request_times[0])
        logger.warning(f"Rate limit reached. Waiting {wait_time:.2f}s")
        await asyncio.sleep(wait_time)
        _prune_request_times()

    request_times.append(time.time())


# ==========================================================
# AIOHTTP SESSION (shared)
# ==========================================================

_session = None


async def get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


# ==========================================================
# SIMPLEMMO API
# ==========================================================
#
# The public SimpleMMO API (https://web.simple-mmo.com/p-api/home)
# requires the api_key as a QUERY PARAMETER in the URL, not as an
# Authorization header. Confirmed working example:
#
#   POST https://api.simple-mmo.com/v2/orphanage?api_key=XXXX
#
# (no Authorization/Bearer header, no request body needed)


async def smmo_request(endpoint, method="POST"):
    """Make a request to the SimpleMMO API using api_key as a query parameter."""

    await rate_limit()

    separator = "&" if "?" in endpoint else "?"
    url = f"https://api.simple-mmo.com{endpoint}{separator}api_key={API_KEY}"

    session = await get_session()

    try:
        if method == "POST":
            async with session.post(url) as response:
                return await _handle_response(response, endpoint)
        else:
            async with session.get(url) as response:
                return await _handle_response(response, endpoint)
    except aiohttp.ClientError as e:
        logger.error(f"Network error on {endpoint}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error on {endpoint}: {e}")
        return None


async def _handle_response(response, endpoint):
    global consecutive_401_count, auth_failure_notified

    if response.status == 429:
        logger.error(f"API rate limit hit (429) on {endpoint}")
        return None
    if response.status == 401:
        body_text = await response.text()
        logger.warning(f"Auth failed (401) on {endpoint} — body: {body_text}")
        consecutive_401_count += 1
        return None
    if response.status == 405:
        body_text = await response.text()
        logger.error(f"Method not allowed (405) on {endpoint} — body: {body_text}")
        return None
    if response.status != 200:
        body_text = await response.text()
        logger.error(f"API Error: {response.status} on {endpoint} — body: {body_text}")
        return None

    # Any successful response clears the auth-failure streak.
    consecutive_401_count = 0
    auth_failure_notified = False
    return await response.json()


async def get_raid():
    return await smmo_request(f"/v1/guilds/raid/{GUILD_ID}")


async def get_orphanage():
    return await smmo_request("/v2/orphanage")


async def get_worldboss():
    return await smmo_request("/v1/worldboss/all")


async def get_guild_task():
    return await smmo_request(f"/v1/guilds/task/{GUILD_ID}")


# ==========================================================
# RAID CHECK
# ==========================================================


def is_valid_raid(raid):
    """True only if the raid has real data: location(s) and expires_at present.
    The API still returns an object when no raid is active
    (started_at/locations/expires_at empty or null): this must be treated as
    'no raid', not as a new raid to notify about."""
    if raid is None:
        return False

    locations = raid.get("locations")
    expires = raid.get("expires_at")

    return bool(locations) and bool(expires)


def is_new_raid(raid):
    """Compares the current raid with the last one notified. Returns True if new."""
    global last_raid_started

    started = raid.get("started_at")

    if started != last_raid_started:
        last_raid_started = started
        persist_state()
        return True

    return False


def raid_expiring_soon(raid):
    """True if the raid is still active but expires within RAID_REMINDER_MINUTES_BEFORE minutes."""
    expires = raid.get("expires_at")
    if not expires:
        return False

    try:
        dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        return False

    remaining = (dt - datetime.now(timezone.utc)).total_seconds()
    return 0 < remaining <= RAID_REMINDER_MINUTES_BEFORE * 60


# ==========================================================
# ORPHANAGE CHECK
# ==========================================================


async def check_orphanage():
    global last_orphanage

    data = await get_orphanage()
    if data is None:
        return None

    active = None
    for tier in data:
        if tier.get("is_active"):
            active = tier
            break

    active_key = None
    if active:
        tier_name = active.get("tier", {}).get("name", "")
        percentage = active.get("percentage", 0)
        active_key = f"{tier_name}:{percentage}"

    if active_key != last_orphanage:
        last_orphanage = active_key
        persist_state()
        return active

    return None


# ==========================================================
# WORLD BOSS CHECK
# ==========================================================


def is_boss_active(boss, now=None):
    """True if the boss is active right now: enable_time already passed and HP > 0."""
    if now is None:
        now = time.time()

    enable_time = boss.get("enable_time") or 0
    current_hp = boss.get("current_hp") or 0

    return enable_time <= now and current_hp > 0


async def check_worldboss():
    """Returns (activated, killed): lists of bosses whose state changed since the last check."""
    global last_worldboss

    bosses = await get_worldboss()
    if not bosses:
        return [], []

    now = time.time()
    new_state = {}
    activated = []
    killed = []

    for boss in bosses:
        boss_id = str(boss.get("id"))
        if boss_id == "None":
            continue

        active_now = is_boss_active(boss, now)
        current_hp = boss.get("current_hp") or 0

        prev = last_worldboss.get(boss_id, {})
        was_active = prev.get("active", False)

        if active_now and not was_active:
            activated.append(boss)
        elif was_active and current_hp <= 0:
            killed.append(boss)

        new_state[boss_id] = {"active": active_now, "hp": current_hp}

    if new_state != last_worldboss:
        last_worldboss = new_state
        persist_state()

    return activated, killed


# ==========================================================
# GUILD TASK CHECK
# ==========================================================


def is_valid_guild_task(task):
    """True only if the task has real data (type and target_amount present/valid)."""
    if not task:
        return False

    return bool(task.get("type")) and bool(task.get("target_amount"))


async def check_guild_task():
    """Returns ('new', task) if it's a new task, ('completed', task) if the
    current one was just completed, otherwise None."""
    global last_guild_task_key, guild_task_completed_notified

    task = await get_guild_task()
    if not is_valid_guild_task(task):
        return None

    task_type = task.get("type")
    target = task.get("target_amount")
    current = task.get("current_amount", 0)
    key = f"{task_type}:{target}"

    if key != last_guild_task_key:
        last_guild_task_key = key
        guild_task_completed_notified = False
        persist_state()
        return ("new", task)

    if current >= target and not guild_task_completed_notified:
        guild_task_completed_notified = True
        persist_state()
        return ("completed", task)

    return None


# ==========================================================
# DISCORD EMBEDS
# ==========================================================


def parse_timestamp(ts_str):
    if not ts_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        unix_ts = int(dt.timestamp())
        return f"<t:{unix_ts}:R>"
    except ValueError:
        return ts_str


def create_raid_embed(raid):
    embed = discord.Embed(
        title="⚔️ Raid Started!",
        description="A new guild raid has started!",
        color=0xFF4444,
    )

    locations = raid.get("locations", [])
    embed.add_field(
        name="📍 Locations",
        value="\n".join(locations) if locations else "Unknown",
        inline=False,
    )

    expires = raid.get("expires_at")
    embed.add_field(name="⏰ Expires", value=parse_timestamp(expires), inline=False)

    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def create_raid_reminder_embed(raid):
    embed = discord.Embed(
        title="⏰ Raid Expiring Soon!",
        description="The current raid is about to expire — get in before it's gone!",
        color=0xFFA500,
    )
    embed.add_field(
        name="Expires", value=parse_timestamp(raid.get("expires_at")), inline=False
    )
    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def create_orphanage_embed(orphanage):
    tier_name = orphanage.get("tier", {}).get("name", "Unknown Tier")

    embed = discord.Embed(
        title="🏠 Orphanage Active",
        description=f"**{tier_name}** is now active!",
        color=0x44FF44,
    )

    effects = orphanage.get("effects", [])
    embed.add_field(
        name="✨ Effects", value="\n".join(effects) if effects else "None", inline=False
    )

    percentage = orphanage.get("percentage", 0)
    embed.add_field(name="📊 Progress", value=f"{percentage}%", inline=False)

    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def format_number(n):
    """Abbreviates large numbers for readability while keeping the exact
    value visible, e.g. 1,234,567 -> '1.2M (1,234,567)'."""
    n = n or 0
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B ({n:,})"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M ({n:,})"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K ({n:,})"
    return f"{n:,}"


def create_worldboss_embed(boss, killed=False):
    name = boss.get("name", "Unknown")
    level = boss.get("level", "?")

    if killed:
        embed = discord.Embed(
            title="💀 World Boss Defeated!",
            description=f"**{name}** (Lv. {level}) has been taken down!",
            color=0x888888,
        )
    else:
        embed = discord.Embed(
            title="🔥 World Boss Active!",
            description=f"**{name}** (Lv. {level}) is now available!",
            color=0xFF8800,
        )
        hp = boss.get("current_hp", 0)
        max_hp = boss.get("max_hp", 0)
        pct = int(hp / max_hp * 100) if max_hp else 0
        embed.add_field(
            name="❤️ HP",
            value=f"{format_number(hp)} / {format_number(max_hp)} ({pct}%)",
            inline=False,
        )

    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def _progress_bar(current, target, length=20):
    pct = 0 if not target else min(100, int(current / target * 100))
    filled = int(length * pct / 100)
    bar = "█" * filled + "░" * (length - filled)
    return bar, pct


def create_guild_task_embed(task, completed=False):
    task_type = str(task.get("type", "Unknown")).capitalize()
    exp_reward = task.get("exp_reward", 0)
    pp_reward = task.get("power_point_reward", 0)

    if completed:
        embed = discord.Embed(
            title="✅ Guild Task Completed!",
            description=f"The **{task_type}** task has been completed!",
            color=0x44FF44,
        )
    else:
        embed = discord.Embed(
            title="📋 New Guild Task!",
            description=f"Type: **{task_type}**",
            color=0x4488FF,
        )
        current = task.get("current_amount", 0)
        target = task.get("target_amount", 0)
        bar, pct = _progress_bar(current, target)
        embed.add_field(name="🎯 Target", value=f"{target:,}", inline=False)
        embed.add_field(
            name="📊 Progress",
            value=f"{current:,} / {target:,} ({pct}%)\n{bar}",
            inline=False,
        )

    embed.add_field(
        name="🎁 Reward",
        value=f"{format_number(exp_reward)} EXP + {format_number(pp_reward)} Power Points",
        inline=False,
    )

    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def create_guild_task_status_embed(task):
    task_type = str(task.get("type", "Unknown")).capitalize()
    current = task.get("current_amount", 0)
    target = task.get("target_amount", 0)
    bar, pct = _progress_bar(current, target)

    embed = discord.Embed(title="📋 Guild Task Status", color=0x4488FF)
    embed.add_field(name="Type", value=task_type, inline=False)
    embed.add_field(
        name="Progress", value=f"{current:,} / {target:,} ({pct}%)", inline=False
    )
    embed.add_field(name="Bar", value=bar, inline=False)
    embed.add_field(
        name="🎁 Reward",
        value=f"{format_number(task.get('exp_reward', 0))} EXP + {format_number(task.get('power_point_reward', 0))} Power Points",
        inline=False,
    )

    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def create_auth_failure_embed(count):
    embed = discord.Embed(
        title="🚨 API Authentication Failing",
        description=(
            f"The bot has failed to authenticate with the SimpleMMO API "
            f"{count} times in a row. The API key may be invalid or expired — "
            f"please check the `.env` configuration."
        ),
        color=0xFF0000,
    )
    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


# ==========================================================
# BACKGROUND MONITOR
# ==========================================================


@tasks.loop(minutes=1)
async def monitor():
    try:
        await _monitor_tick()
    except Exception:
        # Never let an unexpected error silently kill the loop — log it
        # with the full traceback and try again on the next tick.
        logger.exception("Unexpected error during monitor tick")


async def _monitor_tick():
    global last_check_time
    last_check_time = time.time()

    channel = bot.get_channel(CHANNEL_ID)

    if channel is None:
        logger.warning(f"Channel {CHANNEL_ID} not found")
        return

    if not isinstance(channel, discord.TextChannel):
        logger.error(
            f"Channel {CHANNEL_ID} is a {type(channel).__name__}, "
            f"not a TextChannel. Please set a text channel ID in .env"
        )
        return

    global raid_reminder_sent, auth_failure_notified, no_raid_logged

    raid = await get_raid()
    if raid is not None and not is_valid_raid(raid):
        if not no_raid_logged:
            logger.info("No active raid (empty/placeholder data from API)")
            no_raid_logged = True
        raid = None

    if raid is not None:
        no_raid_logged = False
        if is_new_raid(raid):
            logger.info("New raid detected, sending notification")
            ping_content = f"<@&{RAID_ROLE_ID}>" if RAID_ROLE_ID else None
            await channel.send(content=ping_content, embed=create_raid_embed(raid))
            raid_reminder_sent = False
        elif not raid_reminder_sent and raid_expiring_soon(raid):
            logger.info("Raid expiring soon, sending reminder")
            await channel.send(embed=create_raid_reminder_embed(raid))
            raid_reminder_sent = True

    orphanage = await check_orphanage()
    if orphanage:
        logger.info("New orphanage event detected, sending notification")
        await channel.send(embed=create_orphanage_embed(orphanage))

    boss_activated, boss_killed = await check_worldboss()
    for boss in boss_activated:
        logger.info(f"World boss activated: {boss.get('name')}")
        await channel.send(embed=create_worldboss_embed(boss, killed=False))
    for boss in boss_killed:
        logger.info(f"World boss killed: {boss.get('name')}")
        await channel.send(embed=create_worldboss_embed(boss, killed=True))

    task_event = await check_guild_task()
    if task_event:
        event_type, task = task_event
        if event_type == "new":
            logger.info("New guild task detected, sending notification")
            await channel.send(embed=create_guild_task_embed(task, completed=False))
        elif event_type == "completed":
            logger.info("Guild task completed, sending notification")
            await channel.send(embed=create_guild_task_embed(task, completed=True))

    # Warn once if the API key seems to be failing repeatedly.
    if consecutive_401_count >= AUTH_FAILURE_THRESHOLD and not auth_failure_notified:
        logger.error(
            f"{consecutive_401_count} consecutive auth failures, notifying channel"
        )
        await channel.send(embed=create_auth_failure_embed(consecutive_401_count))
        auth_failure_notified = True


# ==========================================================
# EVENTS
# ==========================================================

_synced = False


@bot.event
async def on_ready():
    global _synced

    logger.info(f"Bot connected as {bot.user}")
    logger.info(f"Guild ID: {GUILD_ID}")

    if not _synced:
        await bot.tree.sync()
        _synced = True
        logger.info("Slash commands synced")
    else:
        logger.info("Slash commands already synced, skipping")

    if not monitor.is_running():
        monitor.start()
        logger.info("Monitoring started")
    else:
        logger.info("Monitor already running")

    logger.info("Bot is ready")


@bot.event
async def on_disconnect():
    await close_session()
    logger.info("Session closed on disconnect")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: discord.app_commands.AppCommandError
):
    """Catches errors from slash commands so users get a clear message
    instead of Discord's generic 'Interaction failed'."""

    if isinstance(error, discord.app_commands.CommandOnCooldown):
        message = (
            f"⏳ This command is on cooldown, try again in {error.retry_after:.1f}s."
        )
    elif isinstance(error, discord.app_commands.MissingPermissions):
        message = "🚫 You don't have permission to use this command."
    else:
        logger.exception(
            f"Unhandled error in command '{interaction.command.name if interaction.command else '?'}'",
            exc_info=error,
        )
        message = (
            "⚠️ Something went wrong while running this command. It's been logged."
        )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        # Interaction already expired/invalid — nothing more we can do.
        pass


# ==========================================================
# COMMANDS
# ==========================================================


@bot.tree.command(name="raid", description="Show the current guild raid status")
async def raid_command(interaction: discord.Interaction):
    await interaction.response.defer()

    raid = await get_raid()

    if raid is None:
        await interaction.followup.send(
            "⚠️ Couldn't fetch raid data from the API right now. Check the logs for details."
        )
        return

    if not is_valid_raid(raid):
        await interaction.followup.send(
            "ℹ️ No active guild raid right now. This is the real data from the API — it's just empty."
        )
        return

    await interaction.followup.send(embed=create_raid_embed(raid))


@bot.tree.command(name="orphanage", description="Show the current orphanage status")
async def orphanage_command(interaction: discord.Interaction):
    await interaction.response.defer()

    data = await get_orphanage()

    if data is None:
        await interaction.followup.send(
            "⚠️ Couldn't fetch orphanage data from the API right now. Check the logs for details."
        )
        return

    active = None
    for tier in data:
        if tier.get("is_active"):
            active = tier
            break

    if active is None:
        # No active tier: show the one with the most progress anyway, for context.
        closest = max(data, key=lambda t: t.get("percentage", 0), default=None)
        if closest:
            pct = closest.get("percentage", 0)
            tier_name = closest.get("tier", {}).get("name", "Unknown")
            await interaction.followup.send(
                f"ℹ️ No orphanage tier is currently active. Closest is **{tier_name}** at {pct}% progress."
            )
        else:
            await interaction.followup.send("ℹ️ No orphanage data available right now.")
        return

    await interaction.followup.send(embed=create_orphanage_embed(active))


@bot.tree.command(name="task", description="Show the current guild task status")
async def task_command(interaction: discord.Interaction):
    await interaction.response.defer()

    task = await get_guild_task()

    if task is None:
        await interaction.followup.send(
            "⚠️ Couldn't fetch guild task data from the API right now. Check the logs for details."
        )
        return

    if not is_valid_guild_task(task):
        await interaction.followup.send("ℹ️ No active guild task right now.")
        return

    await interaction.followup.send(embed=create_guild_task_status_embed(task))


@bot.tree.command(name="worldboss", description="Show active world bosses")
async def worldboss_command(interaction: discord.Interaction):
    await interaction.response.defer()

    bosses = await get_worldboss()

    if bosses is None:
        await interaction.followup.send(
            "⚠️ Couldn't fetch world boss data from the API right now. Check the logs for details."
        )
        return

    now = time.time()
    active_bosses = [b for b in bosses if is_boss_active(b, now)]

    if not active_bosses:
        await interaction.followup.send("ℹ️ No world boss is currently active.")
        return

    embed = discord.Embed(title="🔥 Active World Bosses", color=0xFF8800)
    for boss in active_bosses:
        hp = boss.get("current_hp", 0)
        max_hp = boss.get("max_hp", 1)
        pct = int(hp / max_hp * 100) if max_hp else 0
        embed.add_field(
            name=f"{boss.get('name', 'Unknown')} (Lv. {boss.get('level', '?')})",
            value=f"HP: {format_number(hp)} / {format_number(max_hp)} ({pct}%)",
            inline=False,
        )
    embed.timestamp = datetime.now(timezone.utc)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="status", description="Show bot status")
async def status(interaction: discord.Interaction):
    _prune_request_times()
    requests_count = len(request_times)

    embed = discord.Embed(title="🤖 SimpleMMO Bot Status", color=0x4444FF)

    embed.add_field(name="Status", value="🟢 Online", inline=False)
    embed.add_field(name="Guild ID", value=str(GUILD_ID), inline=False)
    embed.add_field(
        name="API Requests (last minute)",
        value=f"{requests_count}/{MAX_REQUESTS_PER_MINUTE}",
        inline=False,
    )

    if last_check_time:
        embed.add_field(
            name="Last Check", value=f"<t:{int(last_check_time)}:R>", inline=False
        )

    embed.timestamp = datetime.now(timezone.utc)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="uptime", description="Show how long the bot has been running")
async def uptime(interaction: discord.Interaction):
    started_ts = int(bot_start_time)
    await interaction.response.send_message(
        f"🟢 Bot online since <t:{started_ts}:R> (<t:{started_ts}:f>)"
    )


# ==========================================================
# START
# ==========================================================

if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        asyncio.run(close_session())
