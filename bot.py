import discord
from discord.ext import commands, tasks

import aiohttp
import asyncio
import os
import re
import signal
import tempfile
import time
import json
import logging
import traceback
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


class JsonFormatter(logging.Formatter):
    """Renders each log record as a single JSON object per line, so logs
    can be ingested/searched by external tools instead of parsed as free text."""

    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))

        return json.dumps(payload, ensure_ascii=False)


_json_handler_file = logging.FileHandler("bot.log")
_json_handler_file.setFormatter(JsonFormatter())

_plain_handler_stream = logging.StreamHandler()
_plain_handler_stream.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)

logging.basicConfig(
    level=logging.INFO, handlers=[_json_handler_file, _plain_handler_stream]
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
    """Writes state atomically: write to a temp file in the same directory,
    then os.replace() it over the real file. This avoids leaving a
    truncated/corrupted bot_state.json behind if the process is killed or
    crashes mid-write."""
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".bot_state_", suffix=".tmp", dir=directory
        )
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except OSError as e:
        logger.error(f"Failed to persist state: {e}")
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


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

# Persisted now (item 9): these used to live only in memory and reset on
# every restart, which could cause duplicate notifications right after a
# restart that happened to land near an event.
raid_reminder_sent = _state.get("raid_reminder_sent", False)
RAID_REMINDER_MINUTES_BEFORE = 10  # how long before expiry to warn
no_raid_logged = _state.get(
    "no_raid_logged", False
)  # avoids logging "no active raid" on every single check

# World boss "incoming soon" reminder: warns once per boss cycle, this many
# minutes before its enable_time. Keyed by boss_id -> enable_time already
# notified for, so a new cycle (different enable_time) can be re-notified.
WORLDBOSS_REMINDER_MINUTES_BEFORE = 1
worldboss_reminder_notified_for = _state.get(
    "worldboss_reminder_notified_for", {}
)  # {boss_id: enable_time}

# Tracks consecutive 401 (auth) failures PER ENDPOINT, so one endpoint
# failing repeatedly can't be masked by another endpoint that's still
# succeeding (each endpoint clears only its own streak on success).
# This dict is ephemeral (not persisted): a fresh count on restart is fine,
# it just takes a few more ticks to re-detect an ongoing failure.
consecutive_401_counts = {}  # {endpoint: count}
# This one IS persisted, so a restart doesn't cause the same ongoing auth
# failure to be re-announced every time the bot restarts.
auth_failure_notified_endpoints = _state.get(
    "auth_failure_notified_endpoints", {}
)  # {endpoint: True}
AUTH_FAILURE_THRESHOLD = 3

# Guild sanctuary tracking:
# - last_sanctuary_active: the tier "key" (e.g. "tier_2") that currently has
#   is_active == true, or None if no tier is active. Used to detect when the
#   guild switches to a different active tier.
# - sanctuary_completed_tiers: list of tier "key"s whose goal has already
#   been reached (percentage >= 100) and already notified about, so we don't
#   re-notify every tick once a tier stays completed.
last_sanctuary_active = _state.get("last_sanctuary_active")
sanctuary_completed_tiers = _state.get("sanctuary_completed_tiers", [])


def persist_state():
    save_state(
        {
            "last_raid_started": last_raid_started,
            "last_orphanage": last_orphanage,
            "last_worldboss": last_worldboss,
            "last_guild_task_key": last_guild_task_key,
            "guild_task_completed_notified": guild_task_completed_notified,
            "raid_reminder_sent": raid_reminder_sent,
            "no_raid_logged": no_raid_logged,
            "worldboss_reminder_notified_for": worldboss_reminder_notified_for,
            "auth_failure_notified_endpoints": auth_failure_notified_endpoints,
            "last_sanctuary_active": last_sanctuary_active,
            "sanctuary_completed_tiers": sanctuary_completed_tiers,
        }
    )


# ==========================================================
# RATE LIMITER
# ==========================================================

# The SimpleMMO API has a real limit of 40 requests/minute
# (see the "x-ratelimit-limit: 40" header in responses).
# We keep a safety margin.
MAX_REQUESTS_PER_MINUTE = 35

# How long to wait for a single SimpleMMO API call before giving up. Without
# this, a stalled/hanging response could block the monitor tick indefinitely.
HTTP_TIMEOUT_SECONDS = 15

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
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        _session = aiohttp.ClientSession(timeout=timeout)
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


def _mask_secret(text):
    """Strips the api_key value out of any string before it's logged. aiohttp
    exceptions often embed the full request URL in their string
    representation, which would otherwise leak the key into bot.log."""
    return re.sub(r"(api_key=)[^&\s'\")]+", r"\1***", str(text))


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
        logger.error(f"Network error on {endpoint}: {_mask_secret(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error on {endpoint}: {_mask_secret(e)}")
        return None


async def _handle_response(response, endpoint):
    global auth_failure_notified_endpoints

    if response.status == 429:
        logger.error(f"API rate limit hit (429) on {endpoint}")
        return None
    if response.status == 401:
        body_text = await response.text()
        logger.warning(f"Auth failed (401) on {endpoint} — body: {body_text}")
        consecutive_401_counts[endpoint] = consecutive_401_counts.get(endpoint, 0) + 1
        return None
    if response.status == 405:
        body_text = await response.text()
        logger.error(f"Method not allowed (405) on {endpoint} — body: {body_text}")
        return None
    if response.status != 200:
        body_text = await response.text()
        logger.error(f"API Error: {response.status} on {endpoint} — body: {body_text}")
        return None

    # A successful response clears only THIS endpoint's auth-failure streak,
    # so a healthy endpoint can't mask another endpoint that keeps failing.
    consecutive_401_counts.pop(endpoint, None)
    if endpoint in auth_failure_notified_endpoints:
        del auth_failure_notified_endpoints[endpoint]
        persist_state()
    return await response.json()


async def get_raid():
    return await smmo_request(f"/v1/guilds/raid/{GUILD_ID}")


async def get_orphanage():
    return await smmo_request("/v2/orphanage")


async def get_worldboss():
    return await smmo_request("/v1/worldboss/all")


async def get_guild_task():
    return await smmo_request(f"/v1/guilds/task/{GUILD_ID}")


async def get_sanctuary():
    return await smmo_request(f"/v1/guilds/sanctuary/{GUILD_ID}")


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


def raid_is_new(raid, last_started):
    """Pure comparison: True if this raid's started_at differs from
    last_started. Does not mutate any state — the caller decides whether and
    when to call commit_raid_seen() to actually record it."""
    return raid.get("started_at") != last_started


def commit_raid_seen(raid):
    """Records this raid's started_at as the last one seen/notified, and persists it."""
    global last_raid_started
    last_raid_started = raid.get("started_at")
    persist_state()


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


def get_upcoming_worldbosses(bosses, now=None, limit=1):
    """Returns up to `limit` bosses that haven't spawned yet, sorted by
    soonest enable_time first (i.e. the next bosses to spawn). enable_time
    in the SimpleMMO API is an absolute Unix timestamp, not a relative
    countdown, so 'upcoming' means enable_time > now."""
    if now is None:
        now = time.time()

    upcoming = [b for b in bosses if (b.get("enable_time") or 0) > now]
    upcoming.sort(key=lambda b: b.get("enable_time"))

    return upcoming[:limit]


def get_upcoming_worldboss(bosses, now=None):
    """Returns just the single next boss to spawn, or None. Thin wrapper
    around get_upcoming_worldbosses() for callers that only need one."""
    upcoming = get_upcoming_worldbosses(bosses, now, limit=1)
    return upcoming[0] if upcoming else None


def worldboss_incoming_soon(boss, now=None):
    """True if the given (not yet active) boss's enable_time falls within
    WORLDBOSS_REMINDER_MINUTES_BEFORE minutes from now."""
    if now is None:
        now = time.time()

    enable_time = boss.get("enable_time") or 0
    remaining = enable_time - now
    return 0 < remaining <= WORLDBOSS_REMINDER_MINUTES_BEFORE * 60


async def check_worldboss():
    """Returns (activated, killed, incoming):
    - activated: bosses whose state changed from inactive to active since the last check.
      NOTE: this is still tracked/returned for internal state purposes (it's
      needed to correctly detect the later "killed" transition), but the
      caller intentionally does NOT send a Discord notification for it — the
      only "boss is starting" notification is the 1-minute-before "incoming" one.
    - killed: bosses whose state changed from active to dead (hp <= 0) since the last check
    - incoming: the next upcoming boss if it's about to spawn within
      WORLDBOSS_REMINDER_MINUTES_BEFORE minutes and hasn't been notified yet
      for this specific enable_time, otherwise None
    """
    global last_worldboss, worldboss_reminder_notified_for

    bosses = await get_worldboss()
    if not bosses:
        return [], [], None

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

    # Check whether the next upcoming (not yet active) boss should trigger a
    # "spawning soon" reminder. Tracked per boss_id + enable_time so a new
    # spawn cycle for the same boss can be notified again.
    incoming = None
    next_boss = get_upcoming_worldboss(bosses, now)
    if next_boss is not None:
        boss_id = str(next_boss.get("id"))
        enable_time = next_boss.get("enable_time")
        already_notified_for = worldboss_reminder_notified_for.get(boss_id)

        if (
            worldboss_incoming_soon(next_boss, now)
            and already_notified_for != enable_time
        ):
            incoming = next_boss
            worldboss_reminder_notified_for[boss_id] = enable_time

    return activated, killed, incoming


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
# GUILD SANCTUARY CHECK
# ==========================================================


def _sanctuary_tier_key(tier):
    return tier.get("tier", {}).get("key")


async def check_sanctuary():
    """Returns (newly_active, newly_completed):
    - newly_active: the tier dict that just became the active one
      (is_active flipped to true on a different tier than before), or None
      if the active tier didn't change.
    - newly_completed: list of tier dicts whose goal was just reached
      (percentage >= 100) for the first time, i.e. not already notified.
    """
    global last_sanctuary_active, sanctuary_completed_tiers

    tiers = await get_sanctuary()
    if not tiers:
        return None, []

    active = None
    for tier in tiers:
        if tier.get("is_active"):
            active = tier
            break

    active_key = _sanctuary_tier_key(active) if active else None

    newly_active = None
    if active_key != last_sanctuary_active:
        last_sanctuary_active = active_key
        persist_state()
        if active:
            newly_active = active

    newly_completed = []
    for tier in tiers:
        key = _sanctuary_tier_key(tier)
        if (
            key
            and tier.get("percentage", 0) >= 100
            and key not in sanctuary_completed_tiers
        ):
            sanctuary_completed_tiers.append(key)
            newly_completed.append(tier)

    if newly_completed:
        persist_state()

    return newly_active, newly_completed


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


# Discord hard limits for embeds (https://discord.com/developers/docs/resources/message#embed-object-embed-limits).
# Exceeding these raises an HTTPException when sending the message.
DISCORD_MAX_EMBED_FIELDS = 25
DISCORD_MAX_FIELD_VALUE_LENGTH = 1024


def _safe_field_value(text, limit=DISCORD_MAX_FIELD_VALUE_LENGTH):
    """Truncates a field value so it never exceeds Discord's per-field
    character limit, instead of letting embed.add_field() raise later."""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_unix_relative(unix_ts):
    """Formats a raw Unix timestamp (int/float) as a Discord relative
    timestamp, e.g. 'in 3 hours'. Used for world boss enable_time, which is
    already a Unix timestamp (unlike the ISO strings used elsewhere)."""
    if not unix_ts:
        return "Unknown"
    return f"<t:{int(unix_ts)}:R>"


def create_raid_embed(raid):
    embed = discord.Embed(
        title="⚔️ Raid Started!",
        description="A new guild raid has started!",
        color=0xFF4444,
    )

    locations = raid.get("locations", [])
    embed.add_field(
        name="📍 Locations",
        value=_safe_field_value("\n".join(locations)) if locations else "Unknown",
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
        name="✨ Effects",
        value=_safe_field_value("\n".join(effects)) if effects else "None",
        inline=False,
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


def create_worldboss_incoming_embed(boss):
    """Embed for the 'next world boss is about to spawn' reminder."""
    name = boss.get("name", "Unknown")
    level = boss.get("level", "?")
    enable_time = boss.get("enable_time")

    embed = discord.Embed(
        title="⏳ World Boss Incoming!",
        description=f"**{name}** (Lv. {level}) will spawn soon!",
        color=0xFFA500,
    )
    embed.add_field(
        name="🕐 Spawns", value=format_unix_relative(enable_time), inline=False
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


def create_sanctuary_active_embed(tier):
    tier_name = tier.get("tier", {}).get("name", "Unknown Tier")

    embed = discord.Embed(
        title="🏛️ Sanctuary Tier Active!",
        description=f"**{tier_name}** is now the active guild sanctuary tier!",
        color=0x44FF44,
    )

    effects = tier.get("effects", [])
    embed.add_field(
        name="✨ Effects",
        value=_safe_field_value("\n".join(effects)) if effects else "None",
        inline=False,
    )

    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def create_sanctuary_completed_embed(tier):
    tier_name = tier.get("tier", {}).get("name", "Unknown Tier")
    current = tier.get("current_value", 0)
    target = tier.get("target_value", 0)

    embed = discord.Embed(
        title="🏆 Sanctuary Tier Completed!",
        description=f"**{tier_name}** has reached its goal!",
        color=0xFFD700,
    )

    embed.add_field(name="🎯 Target", value=f"{target:,}", inline=False)
    embed.add_field(name="📊 Reached", value=f"{current:,} / {target:,}", inline=False)

    effects = tier.get("effects", [])
    embed.add_field(
        name="✨ Effects Unlocked",
        value=_safe_field_value("\n".join(effects)) if effects else "None",
        inline=False,
    )

    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def create_sanctuary_status_embed(tiers):
    embed = discord.Embed(title="🏛️ Guild Sanctuary Status", color=0x4488FF)

    for tier in tiers:
        tier_name = tier.get("tier", {}).get("name", "Unknown Tier")
        current = tier.get("current_value", 0)
        target = tier.get("target_value", 0)
        pct = tier.get("percentage", 0)
        bar, _ = _progress_bar(current, target)

        status_bits = []
        if tier.get("is_active"):
            status_bits.append("🟢 Active")
        if tier.get("has_expired"):
            status_bits.append("⌛ Expired")
        elif tier.get("in_progress"):
            status_bits.append("🔨 In progress")
        elif tier.get("goal_reached_at"):
            status_bits.append("✅ Goal reached")
        status = " · ".join(status_bits) if status_bits else "—"

        value_lines = [
            status,
            f"{current:,} / {target:,} ({pct}%)",
            bar,
        ]

        embed.add_field(
            name=tier_name,
            value=_safe_field_value("\n".join(value_lines)),
            inline=False,
        )

    embed.set_footer(text="SimpleMMO Monitor")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


def create_auth_failure_embed(count, endpoint):
    embed = discord.Embed(
        title="🚨 API Authentication Failing",
        description=(
            f"The bot has failed to authenticate with the SimpleMMO API "
            f"on `{endpoint}` {count} times in a row. The API key may be "
            f"invalid or expired — please check the `.env` configuration."
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
        # The gateway cache may not have the channel yet (e.g. right after
        # startup), so fall back to an explicit API fetch before giving up.
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.warning(f"Channel {CHANNEL_ID} not found via cache or fetch: {e}")
            return

    if not isinstance(channel, discord.TextChannel):
        logger.error(
            f"Channel {CHANNEL_ID} is a {type(channel).__name__}, "
            f"not a TextChannel. Please set a text channel ID in .env"
        )
        return

    global raid_reminder_sent, no_raid_logged

    raid = await get_raid()
    if raid is not None and not is_valid_raid(raid):
        if not no_raid_logged:
            logger.info("No active raid (empty/placeholder data from API)")
            no_raid_logged = True
            persist_state()
        raid = None

    if raid is not None:
        if no_raid_logged:
            no_raid_logged = False
            persist_state()

        if raid_is_new(raid, last_raid_started):
            logger.info("New raid detected, sending notification")
            commit_raid_seen(raid)
            ping_content = f"<@&{RAID_ROLE_ID}>" if RAID_ROLE_ID else None
            await channel.send(content=ping_content, embed=create_raid_embed(raid))
            raid_reminder_sent = False
            persist_state()
        elif not raid_reminder_sent and raid_expiring_soon(raid):
            logger.info("Raid expiring soon, sending reminder")
            await channel.send(embed=create_raid_reminder_embed(raid))
            raid_reminder_sent = True
            persist_state()

    orphanage = await check_orphanage()
    if orphanage:
        logger.info("New orphanage event detected, sending notification")
        await channel.send(embed=create_orphanage_embed(orphanage))

    # NOTE: `boss_activated` is intentionally NOT used to send a "World Boss
    # Active" notification anymore. The only "boss is starting" notification
    # is the `boss_incoming` one below, sent WORLDBOSS_REMINDER_MINUTES_BEFORE
    # minutes before the boss actually spawns. `boss_activated` is still
    # returned by check_worldboss() because it's needed internally to detect
    # the "killed" transition correctly.
    _boss_activated, boss_killed, boss_incoming = await check_worldboss()
    for boss in boss_killed:
        logger.info(f"World boss killed: {boss.get('name')}")
        await channel.send(embed=create_worldboss_embed(boss, killed=True))
    if boss_incoming:
        logger.info(f"World boss incoming soon: {boss_incoming.get('name')}")
        await channel.send(embed=create_worldboss_incoming_embed(boss_incoming))

    task_event = await check_guild_task()
    if task_event:
        event_type, task = task_event
        if event_type == "new":
            logger.info("New guild task detected, sending notification")
            await channel.send(embed=create_guild_task_embed(task, completed=False))
        elif event_type == "completed":
            logger.info("Guild task completed, sending notification")
            await channel.send(embed=create_guild_task_embed(task, completed=True))

    sanctuary_active, sanctuary_completed = await check_sanctuary()
    if sanctuary_active:
        logger.info(
            f"Sanctuary tier became active: {sanctuary_active.get('tier', {}).get('name')}"
        )
        await channel.send(embed=create_sanctuary_active_embed(sanctuary_active))
    for tier in sanctuary_completed:
        logger.info(f"Sanctuary tier goal reached: {tier.get('tier', {}).get('name')}")
        await channel.send(embed=create_sanctuary_completed_embed(tier))

    # Warn once per endpoint if the API key seems to be failing repeatedly
    # on that specific endpoint (see _handle_response for how counts accrue).
    for endpoint, count in list(consecutive_401_counts.items()):
        if (
            count >= AUTH_FAILURE_THRESHOLD
            and endpoint not in auth_failure_notified_endpoints
        ):
            logger.error(
                f"{count} consecutive auth failures on {endpoint}, notifying channel"
            )
            await channel.send(embed=create_auth_failure_embed(count, endpoint))
            auth_failure_notified_endpoints[endpoint] = True
            persist_state()


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
@discord.app_commands.checks.cooldown(1, 15.0)
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
@discord.app_commands.checks.cooldown(1, 15.0)
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


@bot.tree.command(
    name="sanctuary", description="Show the current guild sanctuary status"
)
@discord.app_commands.checks.cooldown(1, 15.0)
async def sanctuary_command(interaction: discord.Interaction):
    await interaction.response.defer()

    tiers = await get_sanctuary()

    if not tiers:
        await interaction.followup.send(
            "⚠️ Couldn't fetch sanctuary data from the API right now. Check the logs for details."
        )
        return

    await interaction.followup.send(embed=create_sanctuary_status_embed(tiers))


@bot.tree.command(name="task", description="Show the current guild task status")
@discord.app_commands.checks.cooldown(1, 15.0)
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
@discord.app_commands.checks.cooldown(1, 15.0)
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
        embed = discord.Embed(title="🔥 World Bosses", color=0xFF8800)
        embed.add_field(
            name="Status", value="ℹ️ No world boss is currently active.", inline=False
        )
    else:
        embed = discord.Embed(title="🔥 Active World Bosses", color=0xFF8800)
        # Leave room for the "Next World Boss" field below, and stay safely
        # under Discord's 25-field-per-embed hard limit.
        max_active_fields = DISCORD_MAX_EMBED_FIELDS - 2
        for boss in active_bosses[:max_active_fields]:
            hp = boss.get("current_hp", 0)
            max_hp = boss.get("max_hp", 1)
            pct = int(hp / max_hp * 100) if max_hp else 0
            embed.add_field(
                name=f"{boss.get('name', 'Unknown')} (Lv. {boss.get('level', '?')})",
                value=_safe_field_value(
                    f"HP: {format_number(hp)} / {format_number(max_hp)} ({pct}%)"
                ),
                inline=False,
            )
        remaining = len(active_bosses) - max_active_fields
        if remaining > 0:
            embed.add_field(
                name="…", value=f"and {remaining} more active", inline=False
            )

    # Always show the next upcoming boss countdown too, if there is one.
    next_boss = get_upcoming_worldboss(bosses, now)
    if next_boss:
        embed.add_field(
            name="⏳ Next World Boss",
            value=_safe_field_value(
                f"{next_boss.get('name', 'Unknown')} (Lv. {next_boss.get('level', '?')}) "
                f"— spawns {format_unix_relative(next_boss.get('enable_time'))}"
            ),
            inline=False,
        )

    embed.timestamp = datetime.now(timezone.utc)

    await interaction.followup.send(embed=embed)


@bot.tree.command(
    name="nextbosses",
    description="Show the next world boss(es) about to spawn",
)
@discord.app_commands.describe(
    count="How many upcoming bosses to show (default 1, max 15)"
)
@discord.app_commands.checks.cooldown(1, 15.0)
async def next_bosses_command(
    interaction: discord.Interaction,
    count: discord.app_commands.Range[int, 1, 15] = 1,
):
    await interaction.response.defer()

    bosses = await get_worldboss()

    if bosses is None:
        await interaction.followup.send(
            "⚠️ Couldn't fetch world boss data from the API right now. Check the logs for details."
        )
        return

    now = time.time()
    upcoming = get_upcoming_worldbosses(bosses, now, limit=count)

    if not upcoming:
        await interaction.followup.send(
            "ℹ️ No upcoming world boss found in the current API data "
            "(it may already be active, or the API isn't exposing a next spawn right now)."
        )
        return

    if len(upcoming) == 1:
        await interaction.followup.send(
            embed=create_worldboss_incoming_embed(upcoming[0])
        )
        return

    embed = discord.Embed(
        title=f"⏳ Upcoming World Bosses ({len(upcoming)})", color=0xFFA500
    )
    for boss in upcoming:
        embed.add_field(
            name=f"{boss.get('name', 'Unknown')} (Lv. {boss.get('level', '?')})",
            value=_safe_field_value(
                f"Spawns {format_unix_relative(boss.get('enable_time'))}"
            ),
            inline=False,
        )
    embed.set_footer(text="SimpleMMO Monitor")
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


async def _graceful_shutdown(sig=None):
    """Runs on SIGINT/SIGTERM (e.g. Ctrl+C, or `docker stop` / systemd
    `systemctl stop`) so the bot exits cleanly instead of dropping the
    aiohttp session and any in-flight state mid-write."""
    if sig is not None:
        logger.info(f"Received {sig.name}, shutting down gracefully")

    if monitor.is_running():
        monitor.cancel()

    persist_state()
    await close_session()

    if not bot.is_closed():
        await bot.close()


async def main():
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    shutdown_signal = {}

    def _handle_signal(received_sig):
        shutdown_signal["sig"] = received_sig
        shutdown_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows; Ctrl+C still
            # raises KeyboardInterrupt, which is handled below instead.
            pass

    async def _run_bot():
        async with bot:
            await bot.start(DISCORD_TOKEN)

    bot_task = asyncio.create_task(_run_bot())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        {bot_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
    )

    if shutdown_task in done:
        await _graceful_shutdown(shutdown_signal.get("sig"))
        bot_task.cancel()
    else:
        # The bot task ended on its own (e.g. login failure) — clean up too.
        await _graceful_shutdown()

    for task in pending:
        task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Fallback for platforms where add_signal_handler isn't supported.
        asyncio.run(_graceful_shutdown())
