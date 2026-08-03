# ==========================================================
# LANGUAGE POLICY
# ==========================================================
# All code, comments, docstrings, log messages, and embed/UI text in this
# file must always be written in English, regardless of the language used
# in the conversation that produced or modified this file.
# ==========================================================

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


def _parse_optional_channel_id(env_var_name):
    """Reads an optional channel-id env var. Returns the int ID, or None if
    unset/blank/invalid (logged as a warning if it was set but not a valid
    integer, so a typo in .env doesn't fail silently)."""
    raw = os.getenv(env_var_name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"WARNING: {env_var_name} is set but not a valid integer: {raw!r} — ignoring it")
        return None


# CHANNEL_ID is now OPTIONAL: it used to be the single mandatory channel,
# but now it only acts as a shared fallback for any per-feature channel
# below that you haven't split out yet. What's actually required is that
# EVERY feature ends up with *some* valid channel — either its own specific
# *_CHANNEL_ID, or this fallback — checked explicitly further down.
CHANNEL_ID = _parse_optional_channel_id("CHANNEL_ID")

# Per-feature notification channels. Each one falls back to CHANNEL_ID if
# not set individually, so you can split them out one at a time.
RAID_CHANNEL_ID = _parse_optional_channel_id("RAID_CHANNEL_ID") or CHANNEL_ID
WORLDBOSS_CHANNEL_ID = _parse_optional_channel_id("WORLDBOSS_CHANNEL_ID") or CHANNEL_ID
ORPHANAGE_CHANNEL_ID = _parse_optional_channel_id("ORPHANAGE_CHANNEL_ID") or CHANNEL_ID
GUILD_TASK_CHANNEL_ID = _parse_optional_channel_id("GUILD_TASK_CHANNEL_ID") or CHANNEL_ID
SANCTUARY_CHANNEL_ID = _parse_optional_channel_id("SANCTUARY_CHANNEL_ID") or CHANNEL_ID
ERROR_ALERT_CHANNEL_ID = _parse_optional_channel_id("ERROR_ALERT_CHANNEL_ID") or CHANNEL_ID

# Unlike the others, COMMANDS_CHANNEL_ID has NO fallback: None means "no
# restriction", i.e. slash commands can be used anywhere the bot has
# access, which is the same behavior as before this feature existed.
COMMANDS_CHANNEL_ID = _parse_optional_channel_id("COMMANDS_CHANNEL_ID")

# Now that every per-feature channel has had a chance to fall back to
# CHANNEL_ID, make sure each one actually ended up with SOMETHING. A
# feature left with None here means neither its own specific env var nor
# the general CHANNEL_ID fallback was set — that's a real misconfiguration,
# reported with the exact variable name so it's obvious what to add.
_required_channels = {
    "RAID_CHANNEL_ID": RAID_CHANNEL_ID,
    "WORLDBOSS_CHANNEL_ID": WORLDBOSS_CHANNEL_ID,
    "ORPHANAGE_CHANNEL_ID": ORPHANAGE_CHANNEL_ID,
    "GUILD_TASK_CHANNEL_ID": GUILD_TASK_CHANNEL_ID,
    "SANCTUARY_CHANNEL_ID": SANCTUARY_CHANNEL_ID,
    "ERROR_ALERT_CHANNEL_ID": ERROR_ALERT_CHANNEL_ID,
}
_channel_errors = [
    f"{name} missing: set {name} specifically, or set the general CHANNEL_ID as a fallback"
    for name, value in _required_channels.items()
    if value is None
]
if _channel_errors:
    raise ValueError("\n".join(_channel_errors))


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
            payload["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            )

        return json.dumps(payload, ensure_ascii=False)


_json_handler_file = logging.FileHandler("bot.log")
_json_handler_file.setFormatter(JsonFormatter())

_plain_handler_stream = logging.StreamHandler()
_plain_handler_stream.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)

logging.basicConfig(level=logging.INFO, handlers=[_json_handler_file, _plain_handler_stream])
logger = logging.getLogger(__name__)


# ==========================================================
# DISCORD SETUP
# ==========================================================

intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)


async def _resolve_text_channel(channel_id):
    """Resolves a single channel ID to a discord.TextChannel, falling back
    from the gateway cache to an explicit API fetch (same pattern as the
    old single-channel lookup), and validating it's actually a text
    channel. Returns None (and logs why) if it can't be resolved."""
    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.warning(f"Channel {channel_id} not found via cache or fetch: {e}")
            return None

    if not isinstance(channel, discord.TextChannel):
        logger.error(
            f"Channel {channel_id} is a {type(channel).__name__}, not a TextChannel."
        )
        return None

    return channel


async def resolve_notification_channels(channel_ids):
    """Resolves several channel IDs at once (duplicates are only fetched
    once). Returns a dict {channel_id: TextChannel} containing only the
    ones that resolved successfully — callers should treat a missing key as
    'skip sending to this channel this tick' rather than fail the whole
    tick, since other channels may still be perfectly fine."""
    resolved = {}
    for cid in {cid for cid in channel_ids if cid}:
        channel = await _resolve_text_channel(cid)
        if channel is not None:
            resolved[cid] = channel
    return resolved


async def send_notification(channels, channel_id, expire_at=None, expire_seconds=None, **kwargs):
    """Sends a one-off event notification (content/embed via **kwargs, same
    as channel.send) to a pre-resolved channel from the `channels` dict,
    silently skipping if that channel wasn't available this tick (already
    logged by resolve_notification_channels). If `expire_at` (unix
    timestamp) or `expire_seconds` (relative to now) is given, the message
    is automatically deleted once that time passes (checked once per
    monitor tick — see cleanup_expired_notifications). Neither given means
    it's never auto-deleted (used for things like the auth-failure alert,
    which should stick around until a human deals with it).

    Returns the sent discord.Message (or None if it wasn't sent), so a
    caller that needs to react to a LATER precise event — e.g. deleting a
    "boss active" notification exactly when that boss is killed, rather
    than waiting for a flat timer — can hang onto its channel/message ID."""
    channel = channels.get(channel_id)
    if channel is None:
        return None

    try:
        message = await channel.send(**kwargs)
    except discord.HTTPException as e:
        logger.warning(f"Failed to send notification to channel {channel_id}: {e}")
        return None

    if expire_at is None and expire_seconds is not None:
        expire_at = time.time() + expire_seconds

    if expire_at is not None:
        global pending_notification_deletions
        pending_notification_deletions.append(
            {"channel_id": channel_id, "message_id": message.id, "expires_at": expire_at}
        )
        persist_state()

    return message


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

# How long the "raid started" ping notification stays visible before being
# auto-deleted. Deliberately short — it's just a quick heads-up that
# something happened; the persistent "Raid Status" message (edited in
# place, see create_raid_status_embed) already shows the full up-to-date
# details for as long as the raid stays active, so this one doesn't need
# to linger for hours like it used to. Note actual deletion only happens
# on the next monitor tick after this many seconds pass (see
# cleanup_expired_notifications), so with the default 60s monitor
# interval it'll realistically vanish within roughly a minute, not
# instantly.
RAID_START_NOTIFICATION_LIFETIME_SECONDS = 30
no_raid_logged = _state.get("no_raid_logged", False)  # avoids logging "no active raid" on every single check

# How often the background monitor polls the API. Also used below to size
# WORLDBOSS_REMINDER_SECONDS_BEFORE with a safety margin (see its comment).
MONITOR_INTERVAL_SECONDS = 60

# World boss "incoming soon" reminder: warns once per boss cycle, this many
# seconds before its enable_time. Keyed by boss_id -> enable_time already
# notified for, so a new cycle (different enable_time) can be re-notified.
#
# Kept in SECONDS (not minutes) and set to 2x MONITOR_INTERVAL_SECONDS
# rather than a value close to the polling interval, on purpose: if this
# reminder window were only as wide as the polling interval, a single
# delayed tick (e.g. rate_limit() briefly throttling, or Discord API
# latency) could skip the window entirely and the reminder would silently
# never fire for that boss cycle. A 2x margin is enough here because the
# bot's own request usage (5 endpoints/tick) sits at only ~5-10 req/min
# against the API's 35 req/min cap, so rate_limit() throttling our own
# traffic is very unlikely; this margin mainly guards against ordinary
# network/Discord latency, not rate-limit stalls. The embed still shows the
# exact spawn time via Discord's own live-updating <t:...:R> timestamp, so
# the message stays accurate to the second regardless of exactly when
# within this window it was sent.
WORLDBOSS_REMINDER_SECONDS_BEFORE = MONITOR_INTERVAL_SECONDS * 2  # 120s
worldboss_reminder_notified_for = _state.get("worldboss_reminder_notified_for", {})  # {boss_id: enable_time}

# Tracks consecutive 401 (auth) failures PER ENDPOINT, so one endpoint
# failing repeatedly can't be masked by another endpoint that's still
# succeeding (each endpoint clears only its own streak on success).
# This dict is ephemeral (not persisted): a fresh count on restart is fine,
# it just takes a few more ticks to re-detect an ongoing failure.
consecutive_401_counts = {}  # {endpoint: count}
# This one IS persisted, so a restart doesn't cause the same ongoing auth
# failure to be re-announced every time the bot restarts.
auth_failure_notified_endpoints = _state.get("auth_failure_notified_endpoints", {})  # {endpoint: True}
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

# Persistent "status" message tracking: one message per feature, edited in
# place every tick instead of spamming a new message each time. Keyed by a
# short feature name -> {"channel_id": ..., "message_id": ...}. Persisted so
# a restart keeps editing the SAME message instead of creating a new one.
status_message_ids = _state.get("status_message_ids", {})

# Transient event notifications (new raid, tier activated, boss killed...)
# that should be auto-deleted once they're no longer relevant, instead of
# accumulating forever. Each entry: {"channel_id", "message_id", "expires_at"}.
# Checked/cleaned up once per monitor tick. Persisted so scheduled
# deletions survive a bot restart instead of being forgotten.
pending_notification_deletions = _state.get("pending_notification_deletions", [])

# Tracks the "🔥 World Boss Active!" notification message for each currently
# active boss, keyed by boss_id -> {"channel_id", "message_id"}. Unlike
# most notifications (which expire after a flat time window), this one is
# deleted PRECISELY the moment that same boss is confirmed killed — see the
# boss_killed handling in _monitor_tick — since we don't know in advance
# how long a fight will last, but we do know exactly when it ends.
worldboss_active_notification_ids = _state.get("worldboss_active_notification_ids", {})

# The single boss currently shown by the worldboss carousel (status message
# + /worldboss command), as a boss_id string, or None if there's nothing to
# show. Shared/global on purpose: there's only one carousel "position" for
# the whole bot, so the persistent status message and any /worldboss replies
# always agree on what's being displayed. Persisted so a restart doesn't
# reset the browsing position back to the first boss.
worldboss_carousel_boss_id = _state.get("worldboss_carousel_boss_id")

# Default lifetime for notifications that have no natural "this is no
# longer relevant" moment to key off of (e.g. "tier activated" — unlike a
# raid's expires_at or a boss's enable_time, there's no API field telling
# us when that announcement stops being useful). Chosen long enough that
# people reading the channel occasionally will still see it, short enough
# that channels don't fill up with month-old announcements.
DEFAULT_NOTIFICATION_LIFETIME_SECONDS = 6 * 60 * 60  # 6 hours

# Tracks the current guild task cycle's last-known current_amount, so a new
# cycle that happens to share the exact same type+target as the previous
# one (see check_guild_task) can still be detected — see FIX in
# check_guild_task for details.
last_guild_task_current = _state.get("last_guild_task_current")


def persist_state():
    save_state(
        {
            "last_raid_started": last_raid_started,
            "last_orphanage": last_orphanage,
            "last_worldboss": last_worldboss,
            "last_guild_task_key": last_guild_task_key,
            "guild_task_completed_notified": guild_task_completed_notified,
            "last_guild_task_current": last_guild_task_current,
            "raid_reminder_sent": raid_reminder_sent,
            "no_raid_logged": no_raid_logged,
            "worldboss_reminder_notified_for": worldboss_reminder_notified_for,
            "auth_failure_notified_endpoints": auth_failure_notified_endpoints,
            "last_sanctuary_active": last_sanctuary_active,
            "sanctuary_completed_tiers": sanctuary_completed_tiers,
            "status_message_ids": status_message_ids,
            "pending_notification_deletions": pending_notification_deletions,
            "worldboss_active_notification_ids": worldboss_active_notification_ids,
            "worldboss_carousel_boss_id": worldboss_carousel_boss_id,
        }
    )


# ==========================================================
# STATUS MESSAGES & NOTIFICATION LIFECYCLE
# ==========================================================
#
# Two distinct kinds of channel message, used together for every feature:
#
# 1. STATUS message: exactly one per feature per channel, edited in place
#    every tick to always reflect the current full state (all tiers, all
#    active bosses, etc.) — never re-sent, so it doesn't spam the channel.
# 2. NOTIFICATION message: sent once when something actually happens (new
#    raid, tier activated, boss killed...). These are meant to be seen and
#    then fade away, so they're auto-deleted once they stop being relevant
#    (see send_notification's expire_at/expire_seconds).


async def upsert_status_message(channels, channel_id, feature_key, embed, view=None):
    """Sends the persistent status embed for `feature_key` the first time,
    then edits that SAME message on every subsequent call. If the stored
    message was deleted out-of-band (manually, channel purge, etc.) or its
    channel changed, transparently sends a fresh one instead.

    `view` is optional (most status embeds don't need one); when given, it's
    attached/kept on both the initial send and every later edit — used by
    the worldboss carousel to keep its ◀ 🔄 ▶ buttons on the message."""
    channel = channels.get(channel_id)
    if channel is None:
        return

    global status_message_ids
    info = status_message_ids.get(feature_key)

    if info and info.get("channel_id") == channel_id:
        try:
            message = await channel.fetch_message(info["message_id"])
            await message.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            pass  # message is gone — fall through and recreate it below
        except discord.HTTPException as e:
            logger.warning(f"Failed to edit '{feature_key}' status message: {e}")
            return

    try:
        message = await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        logger.warning(f"Failed to send '{feature_key}' status message: {e}")
        return

    status_message_ids[feature_key] = {"channel_id": channel_id, "message_id": message.id}
    persist_state()


async def cleanup_expired_notifications(channels):
    """Deletes any notification messages whose expiry has passed. Entries
    for channels that aren't resolvable this tick, or where deletion fails
    for a transient reason, are kept and retried on the next tick instead
    of being dropped."""
    global pending_notification_deletions
    if not pending_notification_deletions:
        return

    now = time.time()
    remaining = []

    for entry in pending_notification_deletions:
        if entry["expires_at"] > now:
            remaining.append(entry)
            continue

        channel = channels.get(entry["channel_id"])
        if channel is None:
            remaining.append(entry)  # retry next tick
            continue

        try:
            message = await channel.fetch_message(entry["message_id"])
            await message.delete()
        except discord.NotFound:
            pass  # already gone (deleted manually, or channel purged) — fine
        except discord.HTTPException as e:
            logger.warning(f"Failed to delete expired notification: {e}")
            remaining.append(entry)  # retry later

    if remaining != pending_notification_deletions:
        pending_notification_deletions = remaining
        persist_state()


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
    """Returns (newly_active, all_tiers):
    - newly_active: the tier dict that just became the active one (its
      identity changed since last check — NOT re-triggered just because its
      percentage moved, that was a bug: notifying on every progress tick),
      or None if the active tier didn't change this time.
    - all_tiers: the full raw tier list from the API (for building the
      always-up-to-date status embed), or None if the API call failed.
    """
    global last_orphanage

    data = await get_orphanage()
    if data is None:
        return None, None

    active = None
    for tier in data:
        if tier.get("is_active"):
            active = tier
            break

    # Keyed ONLY by tier identity, not percentage — percentage changes
    # constantly as the guild progresses and must not re-trigger this.
    active_key = active.get("tier", {}).get("name") if active else None

    newly_active = None
    if active_key != last_orphanage:
        last_orphanage = active_key
        persist_state()
        newly_active = active

    return newly_active, data


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
    WORLDBOSS_REMINDER_SECONDS_BEFORE seconds from now."""
    if now is None:
        now = time.time()

    enable_time = boss.get("enable_time") or 0
    remaining = enable_time - now
    return 0 < remaining <= WORLDBOSS_REMINDER_SECONDS_BEFORE


async def check_worldboss():
    """Returns (activated, killed, incoming, bosses):
    - activated: bosses whose state changed from inactive to active since
      the last check — the caller sends a "World Boss Active!" notification
      for each of these, in addition to the earlier "incoming" heads-up.
    - killed: bosses whose state changed from active to dead (hp <= 0) since the last check
    - incoming: the next upcoming boss if it's about to spawn within
      WORLDBOSS_REMINDER_SECONDS_BEFORE seconds and hasn't been notified yet
      for this specific enable_time, otherwise None
    - bosses: the full raw boss list from the API (for the always-up-to-date
      status embed) — an empty list [] is valid ("no bosses right now"),
      only None means the API call itself failed this tick.
    """
    global last_worldboss, worldboss_reminder_notified_for

    bosses = await get_worldboss()
    if bosses is None:
        # Only a real API failure short-circuits here. An empty list is a
        # perfectly valid response (no bosses active or scheduled right
        # now) and must fall through so the status embed still gets
        # updated to reflect that — not be treated the same as a failure.
        return [], [], None, None

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

        if worldboss_incoming_soon(next_boss, now) and already_notified_for != enable_time:
            incoming = next_boss
            worldboss_reminder_notified_for[boss_id] = enable_time

    return activated, killed, incoming, bosses


# ==========================================================
# GUILD TASK CHECK
# ==========================================================


def is_valid_guild_task(task):
    """True only if the task has real data (type and target_amount present/valid)."""
    if not task:
        return False

    return bool(task.get("type")) and bool(task.get("target_amount"))


async def check_guild_task():
    """Returns (event, task):
    - event: ('new', task) if it's a new task, ('completed', task) if the
      current one was just completed, otherwise None.
    - task: the raw task dict from the API (even if it's not a "valid"
      task, e.g. an empty placeholder — the status embed still wants to
      show "no active task" for that), or None only if the API call itself
      failed this tick (so the caller can skip updating the status embed
      and leave the last known one intact instead of showing an error).
    """
    global last_guild_task_key, guild_task_completed_notified, last_guild_task_current

    task = await get_guild_task()
    if task is None:
        return None, None

    if not is_valid_guild_task(task):
        return None, task

    task_type = task.get("type")
    target = task.get("target_amount")
    current = task.get("current_amount", 0)
    key = f"{task_type}:{target}"

    # FIX: the API gives us no id/started_at for guild tasks — only
    # type+target+current_amount — so a brand new cycle that happens to
    # share the exact same type AND target_amount as the previous one
    # (e.g. "travel:30000" recurring weeks later) produces the same `key`
    # and would otherwise be silently missed as "not new", with
    # guild_task_completed_notified staying True from last time so even
    # the completion notification would never fire again for it.
    # Detect this by noticing current_amount going DOWN: within a single
    # cycle it can only increase, so a drop means a fresh cycle started.
    is_new_key = key != last_guild_task_key
    is_restarted_cycle = (
        not is_new_key
        and last_guild_task_current is not None
        and current < last_guild_task_current
    )

    if is_new_key or is_restarted_cycle:
        last_guild_task_key = key
        guild_task_completed_notified = False
        last_guild_task_current = current
        persist_state()
        return ("new", task), task

    last_guild_task_current = current

    if current >= target and not guild_task_completed_notified:
        guild_task_completed_notified = True
        persist_state()
        return ("completed", task), task

    persist_state()
    return None, task


# ==========================================================
# GUILD SANCTUARY CHECK
# ==========================================================


def _sanctuary_tier_key(tier):
    return tier.get("tier", {}).get("key")


async def check_sanctuary():
    """Returns (newly_active, newly_completed, tiers):
    - newly_active: the tier dict that just became the active one
      (is_active flipped to true on a different tier than before), or None
      if the active tier didn't change.
    - newly_completed: list of tier dicts whose goal was just reached
      (percentage >= 100) for the first time, i.e. not already notified.
    - tiers: the full raw tier list from the API (for the always-up-to-date
      status embed), or None if the API call failed this tick.
    """
    global last_sanctuary_active, sanctuary_completed_tiers

    tiers = await get_sanctuary()
    if not tiers:
        return None, [], None

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
        if key and tier.get("percentage", 0) >= 100 and key not in sanctuary_completed_tiers:
            sanctuary_completed_tiers.append(key)
            newly_completed.append(tier)

    if newly_completed:
        persist_state()

    return newly_active, newly_completed, tiers


# ==========================================================
# DISCORD EMBEDS
# ==========================================================


def parse_timestamp(ts_str):
    """Formats an ISO-8601 timestamp as a Discord dynamic timestamp,
    combining the exact date/time (style 'f') with the relative countdown
    (style 'R') — e.g. 'June 18, 2026 3:53 PM (in 3 hours)'. Both styles are
    rendered client-side by Discord, so every reader sees the exact time
    already converted to THEIR OWN local timezone automatically — no need
    for the bot to know or guess where anyone is."""
    if not ts_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        unix_ts = int(dt.timestamp())
        return f"<t:{unix_ts}:f> (<t:{unix_ts}:R>)"
    except ValueError:
        return ts_str


def _iso_to_unix(iso_str):
    """Parses an API ISO-8601 timestamp (e.g. raid started_at/expires_at) to
    a unix timestamp, or None if it's missing/unparseable."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# Discord hard limits for embeds (https://discord.com/developers/docs/resources/message#embed-object-embed-limits).
# Exceeding these raises an HTTPException when sending the message.
DISCORD_MAX_EMBED_FIELDS = 25
DISCORD_MAX_FIELD_VALUE_LENGTH = 1024

# A consistent color per feature, reused across both its notification
# embeds and its persistent status embed so the two always feel like part
# of the same "section" at a glance.
COLOR_RAID = 0xE74C3C
COLOR_RAID_WARNING = 0xF39C12
COLOR_WORLDBOSS = 0xE67E22
COLOR_WORLDBOSS_DEFEATED = 0x7F8C8D
COLOR_ORPHANAGE = 0x2ECC71
COLOR_GUILD_TASK = 0x3498DB
COLOR_GUILD_TASK_COMPLETE = 0x2ECC71
COLOR_SANCTUARY = 0x9B59B6
COLOR_SANCTUARY_COMPLETE = 0xF1C40F
COLOR_ERROR = 0xC0392B
COLOR_NEUTRAL = 0x99AAB5  # used for "nothing active right now" states

DIVIDER = "▬" * 24

# Optional thumbnail for orphanage embeds (image shown top-right). We
# don't hardcode a "guessed" URL here because if it doesn't actually exist
# Discord just silently shows nothing broken, but it's still better not to
# assert an unverified link: if you want a custom icon, set its direct URL
# in ORPHANAGE_THUMBNAIL_URL in your .env (e.g. a link to an image uploaded
# to Discord itself, or the official icon if you source it from the site).
# If unset, the embed simply has no thumbnail.
ORPHANAGE_THUMBNAIL_URL = os.getenv("ORPHANAGE_THUMBNAIL_URL") or None

# The SimpleMMO API returns each world boss's avatar as a RELATIVE path
# (e.g. "bosses/19"), not a full URL, so it must be combined with a base
# URL and extension to become something Discord can load as a thumbnail,
# e.g. "bosses/19" -> "https://web.simple-mmo.com/img/sprites/bosses/19.png"
# (confirmed real path). Kept configurable via .env in case SimpleMMO
# changes this path later.
WORLDBOSS_AVATAR_BASE_URL = os.getenv(
    "WORLDBOSS_AVATAR_BASE_URL", "https://web.simple-mmo.com/img/sprites/"
)
WORLDBOSS_AVATAR_EXTENSION = os.getenv("WORLDBOSS_AVATAR_EXTENSION", "png")


def _worldboss_avatar_url(boss):
    """Builds a full image URL from the boss's relative 'avatar' path
    (e.g. "bosses/24" -> "https://.../storage/bosses/24.png"), or None if
    the API didn't provide an avatar path for this boss."""
    avatar_path = boss.get("avatar")
    if not avatar_path:
        return None

    base = WORLDBOSS_AVATAR_BASE_URL.rstrip("/")
    path = avatar_path.strip("/")
    return f"{base}/{path}.{WORLDBOSS_AVATAR_EXTENSION}"


def _add_divider(embed):
    """Adds a thin horizontal rule between sections of a multi-part status
    embed (e.g. between per-tier fields and a summary field below them)."""
    embed.add_field(name="\u200b", value=DIVIDER, inline=False)


def _bullet_list(items):
    """Formats a list of strings as a bulleted list, one per line — reads
    much better in an embed than a comma-separated blob of text."""
    if not items:
        return "*None*"
    return "\n".join(f"• {item}" for item in items)


def _finalize_embed(embed, footer_text="Automated update"):
    """Applies the branding/chrome shared by every embed the bot sends: the
    bot's own avatar as a small author icon (top-left), a footer note, and
    a 'Last Updated' field using Discord's live relative timestamp (e.g.
    'a few seconds ago') — it keeps counting up in the client on its own,
    with no need for the bot to re-edit the message."""
    if bot.user:
        embed.set_author(name="SimpleMMO Monitor", icon_url=bot.user.display_avatar.url)
    now = datetime.now(timezone.utc)
    embed.add_field(name="🕐 Last Updated", value=f"<t:{int(now.timestamp())}:R>", inline=False)
    embed.set_footer(text=footer_text)
    embed.timestamp = now
    return embed


def _safe_field_value(text, limit=DISCORD_MAX_FIELD_VALUE_LENGTH):
    """Truncates a field value so it never exceeds Discord's per-field
    character limit, instead of letting embed.add_field() raise later."""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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


def format_unix_relative(unix_ts):
    """Formats a raw Unix timestamp (int/float) as a Discord relative
    timestamp, e.g. 'in 3 hours'. Used for world boss enable_time, which is
    already a Unix timestamp (unlike the ISO strings used elsewhere)."""
    if not unix_ts:
        return "Unknown"
    return f"<t:{int(unix_ts)}:R>"


def _format_duration(seconds):
    """Formats a duration in seconds as e.g. '8h', '1h 30m', '45m' — used
    for the raid's total duration (expires_at - started_at)."""
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "Unknown"

    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60

    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _orphanage_tier_visual(tier):
    """Determines the icon, status label, and bar color for a single
    orphanage tier, centralizing the styling logic used by both the
    single-tier notification embed and the all-tiers status embed, so
    they always stay visually consistent with each other."""
    percentage = tier.get("percentage", 0)
    is_active = tier.get("is_active", False)

    if is_active:
        return "🟢", "In Progress", "🟩"
    if tier.get("has_expired"):
        return "⌛", "Expired", "🟥"
    if percentage >= 100 or tier.get("goal_reached_at"):
        return "🏆", "Goal Reached", "🟨"
    if tier.get("in_progress"):
        return "🔻", "Pending", "🟦"
    return "⚙️", "Not Started", "⬛"


def _orphanage_progress_block(tier, bar_length=14):
    """Builds the shared 'bar + numbers' block used by every orphanage
    embed: a bar colored to match the tier's status, the percentage in
    bold, and the numeric breakdown (current / target / remaining)."""
    current = tier.get("current_value", 0)
    target = tier.get("target_value", 0)
    remaining = tier.get("target_remaining", max(target - current, 0))
    percentage = tier.get("percentage", 0)

    _, _, bar_fill = _orphanage_tier_visual(tier)
    bar, _ = _progress_bar(current, target, length=bar_length, fill=bar_fill)

    return (
        f"`{bar}` **{percentage}%**\n"
        f"🔹 {format_number(current)} / {format_number(target)}\n"
        f"🔸 **{format_number(remaining)}** remaining"
    )


def create_orphanage_embed(orphanage):
    """Notification embed for a new orphanage tier that just became
    active — designed to stand out in the channel: title with the tier
    name, thumbnail (if configured), a large progress bar, and an
    'unlocked bonuses' box if the API data includes any."""
    tier_name = orphanage.get("tier", {}).get("name", "Unknown Tier")
    icon, status_label, _ = _orphanage_tier_visual(orphanage)

    embed = discord.Embed(
        title="🏠✨ New Orphanage Tier Active!",
        description=(
            f"## {icon} {tier_name}\n"
            f"A new goal has kicked off for the guild's orphanage — "
            f"let's get to it! 🎗️"
        ),
        color=COLOR_ORPHANAGE,
    )

    if ORPHANAGE_THUMBNAIL_URL:
        embed.set_thumbnail(url=ORPHANAGE_THUMBNAIL_URL)

    embed.add_field(
        name="📊 Progress",
        value=_safe_field_value(_orphanage_progress_block(orphanage)),
        inline=False,
    )
    embed.add_field(name="🏷️ Status", value=f"**{status_label}**", inline=True)

    effects = orphanage.get("effects") or orphanage.get("tier", {}).get("effects")
    if effects:
        embed.add_field(
            name="✨ Tier Bonuses",
            value=_safe_field_value(_bullet_list(effects)),
            inline=True,
        )

    embed.add_field(
        name="\u200b",
        value=f"{DIVIDER}\n💚 Every contribution counts — thanks to everyone pitching in!",
        inline=False,
    )

    return _finalize_embed(embed, "📣 Orphanage update")


def create_orphanage_status_embed(tiers):
    """Always-current status of ALL orphanage tiers, for the persistent
    status message. Design goals:
    - the active tier is called out right in the description;
    - one tier per field with a status icon, colored bar, and numbers;
    - the active tier is always shown first, then the rest in the API's
      original order — so the most relevant info is always the first
      thing you see, no scrolling needed.
    """
    embed = discord.Embed(
        title="🏠 Orphanage Status",
        color=COLOR_ORPHANAGE,
    )

    if ORPHANAGE_THUMBNAIL_URL:
        embed.set_thumbnail(url=ORPHANAGE_THUMBNAIL_URL)

    if not tiers:
        embed.description = "😴 No orphanage data available right now."
        return _finalize_embed(embed, "🔄 Live status — updates automatically")

    active_tier = next((t for t in tiers if t.get("is_active")), None)

    if active_tier:
        active_name = active_tier.get("tier", {}).get("name", "Unknown Tier")
        embed.description = f"🟢 Active tier: **{active_name}**"
    else:
        embed.description = "😴 No tier is currently active."

    _add_divider(embed)

    # Active tier always first, then the rest in the API's own order — so
    # whoever reads the status immediately sees what matters most.
    ordered_tiers = tiers
    if active_tier:
        ordered_tiers = [active_tier] + [t for t in tiers if t is not active_tier]

    # Safety margin to stay under Discord's 25-field-per-embed hard limit:
    # summary (description) + divider + N tiers + optional "and N more"
    # row + the "Last Updated" field added at the end.
    max_tier_fields = DISCORD_MAX_EMBED_FIELDS - 3
    shown_tiers = ordered_tiers[:max_tier_fields]

    for tier in shown_tiers:
        tier_name = tier.get("tier", {}).get("name", "Unknown Tier")
        icon, status_label, _ = _orphanage_tier_visual(tier)
        is_active = tier.get("is_active", False)

        # The active tier also stands out visually with a markdown quote
        # border ("> ") on top of the icon, so it jumps out immediately
        # in a list of several tiers.
        field_name = f"{icon}  {tier_name}  •  {status_label}"
        if is_active:
            field_name = f"▶️ {field_name} ◀️"

        value = _orphanage_progress_block(tier)
        if is_active:
            value = "\n".join(f"> {line}" for line in value.split("\n"))

        embed.add_field(
            name=field_name,
            value=_safe_field_value(value),
            inline=False,
        )

    remaining = len(ordered_tiers) - len(shown_tiers)
    if remaining > 0:
        embed.add_field(name="…", value=f"and {remaining} more tiers", inline=False)

    return _finalize_embed(embed, "🔄 Live status — updates automatically")


def _raid_duration_only(raid):
    """The raid's total duration (expires_at - started_at) as a short
    string like '8h', or None if either timestamp is missing/unparseable."""
    started_ts = _iso_to_unix(raid.get("started_at"))
    expires_ts = _iso_to_unix(raid.get("expires_at"))
    if started_ts is None or expires_ts is None:
        return None
    return _format_duration(expires_ts - started_ts)


def _raid_location_line(locations):
    """Formats the raid location(s) for a single-line header when there's
    just one (the common case, per the API), or falls back to a bulleted
    block for the rare multi-location raid."""
    if not locations:
        return "Unknown", False
    if len(locations) == 1:
        return locations[0], False
    return _bullet_list(locations), True


def _raid_time_bar(raid, now=None, length=12):
    """Builds a color-coded bar showing how much of the raid's total time
    window is still left. The fill color shifts green -> yellow -> red as
    expiry approaches, so urgency reads at a glance. Returns
    (bar_string, percent_remaining, seconds_remaining), or None if
    timestamps are missing or unparseable."""
    if now is None:
        now = datetime.now(timezone.utc).timestamp()

    started_ts = _iso_to_unix(raid.get("started_at"))
    expires_ts = _iso_to_unix(raid.get("expires_at"))
    if started_ts is None or expires_ts is None or expires_ts <= started_ts:
        return None

    total = expires_ts - started_ts
    elapsed = max(0, min(total, now - started_ts))
    remaining_seconds = max(0, total - elapsed)
    remaining_pct = max(0, min(100, round(remaining_seconds / total * 100)))

    if remaining_pct > 50:
        fill = "🟩"
    elif remaining_pct > 20:
        fill = "🟨"
    else:
        fill = "🟥"

    bar, _ = _progress_bar(elapsed, total, length=length, fill=fill)
    return bar, remaining_pct, remaining_seconds


def create_raid_embed(raid):
    location_line, is_multi = _raid_location_line(raid.get("locations", []))

    embed = discord.Embed(
        title="⚔️ Guild Raid Started!",
        description=f"### 📍 {location_line}\nRally the guild — let's get in there!"
        if not is_multi
        else "### ⚔️ A new raid has begun!\nRally the guild — let's get in there!",
        color=COLOR_RAID,
    )

    if is_multi:
        embed.add_field(name="📍 Locations", value=_safe_field_value(location_line), inline=False)

    embed.add_field(name="🕐 Started", value=parse_timestamp(raid.get("started_at")), inline=False)
    embed.add_field(name="⏰ Expires", value=parse_timestamp(raid.get("expires_at")), inline=False)

    duration = _raid_duration_only(raid)
    if duration:
        embed.add_field(name="⏳ Duration", value=f"**{duration}**", inline=True)

    return _finalize_embed(embed, "📣 New raid alert")


def create_raid_reminder_embed(raid):
    embed = discord.Embed(
        title="⏰ Raid Expiring Soon!",
        description="### 🔴 Time's almost up!\nGet your hits in before it's gone.",
        color=COLOR_RAID_WARNING,
    )

    bar_info = _raid_time_bar(raid)
    if bar_info:
        bar, remaining_pct, remaining_seconds = bar_info
        embed.add_field(
            name="⏳ Time Remaining",
            value=f"`{bar}`\n**{_format_duration(remaining_seconds)} left** ({remaining_pct}%)",
            inline=False,
        )

    embed.add_field(name="⌛ Expires", value=parse_timestamp(raid.get("expires_at")), inline=False)
    return _finalize_embed(embed, "📣 Raid reminder")


def create_raid_status_embed(raid):
    """Always-current raid status, for the persistent status message (as
    opposed to create_raid_embed/create_raid_reminder_embed, which are for
    one-off event notifications)."""
    if raid is None or not is_valid_raid(raid):
        embed = discord.Embed(
            title="⚔️ Raid Status",
            description="### 😴 No active raid right now\nWe'll ping this channel the moment one starts.",
            color=COLOR_NEUTRAL,
        )
        return _finalize_embed(embed, "🔄 Live status — updates automatically")

    location_line, is_multi = _raid_location_line(raid.get("locations", []))

    embed = discord.Embed(
        title="⚔️ Raid Status",
        description=f"### 🟢 Active — 📍 {location_line}"
        if not is_multi
        else "### 🟢 A raid is currently active!",
        color=COLOR_RAID,
    )

    if is_multi:
        embed.add_field(name="📍 Locations", value=_safe_field_value(location_line), inline=False)

    embed.add_field(name="🕐 Started", value=parse_timestamp(raid.get("started_at")), inline=False)
    embed.add_field(name="⏰ Expires", value=parse_timestamp(raid.get("expires_at")), inline=False)

    bar_info = _raid_time_bar(raid)
    duration = _raid_duration_only(raid)
    if bar_info:
        bar, remaining_pct, remaining_seconds = bar_info
        value = f"`{bar}`\n**{_format_duration(remaining_seconds)} left** ({remaining_pct}%)"
        if duration:
            value += f"  •  {duration} total"
        embed.add_field(name="⏳ Time Remaining", value=_safe_field_value(value), inline=False)
    elif duration:
        embed.add_field(name="⏳ Duration", value=f"**{duration}**", inline=True)

    return _finalize_embed(embed, "🔄 Live status — updates automatically")


def _progress_bar(current, target, length=12, fill="🟩", empty="⬛"):
    """Colorful square-emoji progress bar — reads much better in Discord
    than plain ASCII block characters. `fill` lets callers match each
    feature's accent color (e.g. red for HP, blue for guild tasks)."""
    pct = 0 if not target else min(100, max(0, int(current / target * 100)))
    filled = min(length, round(length * pct / 100))
    bar = fill * filled + empty * (length - filled)
    return bar, pct


def create_worldboss_embed(boss, killed=False):
    name = boss.get("name", "Unknown")
    avatar_url = _worldboss_avatar_url(boss)

    if killed:
        embed = discord.Embed(
            title="💀 World Boss Defeated!",
            description=f"**{name}** has fallen. GG! 🎉",
            color=COLOR_WORLDBOSS_DEFEATED,
        )
    else:
        embed = discord.Embed(
            title="🔥 World Boss Active!",
            description=f"### ⚔️ {name}\n**It has spawned — go get it now!**",
            color=COLOR_WORLDBOSS,
        )

    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    return _finalize_embed(embed, "📣 World boss update")


def create_worldboss_incoming_embed(boss):
    """Embed for the 'next world boss is about to spawn' reminder. Shows
    only the boss name and how long until it spawns — no stats."""
    name = boss.get("name", "Unknown")
    enable_time = boss.get("enable_time")
    avatar_url = _worldboss_avatar_url(boss)

    embed = discord.Embed(
        title="⏳ World Boss Incoming!",
        description=f"### 🐾 {name}\nGet ready — it's about to spawn!",
        color=COLOR_RAID_WARNING,
    )
    embed.add_field(
        name="🕐 Spawns", value=f"**{format_unix_relative(enable_time)}**", inline=False
    )

    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    return _finalize_embed(embed, "📣 World boss reminder")


# ----------------------------------------------------------
# Worldboss carousel (single-boss card + ◀ 🔄 ▶ navigation)
# ----------------------------------------------------------
#
# Instead of listing every boss at once, the worldboss status message (and
# the /worldboss command) show ONE boss at a time as a compact card — the
# currently active one if there is one, otherwise the next upcoming one —
# with buttons to browse to other active/upcoming bosses. Only the name and
# timing are shown, deliberately no stats.


def _worldboss_carousel_list(bosses, now=None):
    """Builds the ordered list of bosses the carousel can browse through:
    currently active bosses first (API order), then upcoming (not yet
    spawned) bosses sorted by soonest enable_time — so pressing ▶ from an
    active boss naturally leads into what's coming up next."""
    if now is None:
        now = time.time()

    active = [b for b in bosses if is_boss_active(b, now)]
    upcoming = get_upcoming_worldbosses(bosses, now, limit=len(bosses))
    return active + upcoming


def _carousel_index_for(ordered, boss_id):
    """Finds `boss_id` in `ordered` and returns its index, or 0 (the first
    entry) if it's None or no longer present — e.g. the previously shown
    boss just died or its cycle ended, so browsing safely resets to
    whatever is now first rather than erroring or hiding the card."""
    if boss_id is not None:
        for i, b in enumerate(ordered):
            if str(b.get("id")) == boss_id:
                return i
    return 0


def create_worldboss_card_embed(boss, now=None):
    """Single-boss 'card' embed: just the name, plus either 'active now' or
    how long until it spawns — no HP/level/other stats."""
    if now is None:
        now = time.time()

    name = boss.get("name", "Unknown")
    avatar_url = _worldboss_avatar_url(boss)

    if is_boss_active(boss, now):
        embed = discord.Embed(
            title="🔥 Current World Boss",
            description=f"### ⚔️ {name}",
            color=COLOR_WORLDBOSS,
        )
        embed.add_field(name="Status", value="🟢 **Active now** — go get it!", inline=False)
    else:
        enable_time = boss.get("enable_time")
        embed = discord.Embed(
            title="⏳ Next World Boss",
            description=f"### 🐾 {name}",
            color=COLOR_RAID_WARNING,
        )
        embed.add_field(
            name="Activate in", value=f"**{format_unix_relative(enable_time)}**", inline=True
        )
        if enable_time:
            embed.add_field(name="Activate on", value=f"<t:{int(enable_time)}:f>", inline=True)

    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    return _finalize_embed(embed, "🔄 Live status — use the buttons to browse")


def create_worldboss_card_empty_embed():
    """Card shown when there's currently nothing to browse (no active and
    no upcoming bosses at all)."""
    embed = discord.Embed(
        title="🔥 World Boss",
        description="😴 No world boss is active or scheduled right now.",
        color=COLOR_NEUTRAL,
    )
    return _finalize_embed(embed, "🔄 Live status — use the buttons to browse")


def _update_worldboss_carousel(bosses, now=None):
    """Validates the shared carousel pointer against the latest boss list
    (falling back to the first entry if the previously shown boss is gone)
    and returns the card embed for whichever boss it now points to. Persists
    the pointer only when it actually changes, to avoid needless disk writes
    on every monitor tick."""
    global worldboss_carousel_boss_id

    if now is None:
        now = time.time()

    ordered = _worldboss_carousel_list(bosses, now)
    if not ordered:
        if worldboss_carousel_boss_id is not None:
            worldboss_carousel_boss_id = None
            persist_state()
        return create_worldboss_card_empty_embed()

    idx = _carousel_index_for(ordered, worldboss_carousel_boss_id)
    boss = ordered[idx]
    new_id = str(boss.get("id"))
    if new_id != worldboss_carousel_boss_id:
        worldboss_carousel_boss_id = new_id
        persist_state()

    return create_worldboss_card_embed(boss, now)


class WorldBossCarouselView(discord.ui.View):
    """Persistent ◀ 🔄 ▶ controls for browsing world bosses one at a time.
    Shared by the persistent status message and the /worldboss command —
    the browsing position (worldboss_carousel_boss_id) is global, so every
    copy of this view stays in sync and pressing a button anywhere updates
    what the status message shows on its next refresh too. `timeout=None`
    plus fixed custom_ids make this a persistent view: it keeps working
    after a bot restart as long as it's re-registered via bot.add_view()
    (see on_ready)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="worldboss_carousel_prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, step=-1)

    @discord.ui.button(label="🔄", style=discord.ButtonStyle.secondary, custom_id="worldboss_carousel_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, step=0)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="worldboss_carousel_next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, step=1)

    async def _show(self, interaction: discord.Interaction, step: int):
        global worldboss_carousel_boss_id

        bosses = await get_worldboss()
        if bosses is None:
            await interaction.response.send_message(
                "⚠️ Couldn't fetch world boss data from the API right now.", ephemeral=True
            )
            return

        now = time.time()
        ordered = _worldboss_carousel_list(bosses, now)

        if not ordered:
            worldboss_carousel_boss_id = None
            persist_state()
            await interaction.response.edit_message(embed=create_worldboss_card_empty_embed(), view=self)
            return

        idx = _carousel_index_for(ordered, worldboss_carousel_boss_id)
        idx = (idx + step) % len(ordered)
        boss = ordered[idx]
        worldboss_carousel_boss_id = str(boss.get("id"))
        persist_state()

        await interaction.response.edit_message(embed=create_worldboss_card_embed(boss, now), view=self)


def create_guild_task_embed(task, completed=False):
    task_type = str(task.get("type", "Unknown")).capitalize()
    exp_reward = task.get("exp_reward", 0)
    pp_reward = task.get("power_point_reward", 0)
    target = task.get("target_amount", 0)

    if completed:
        embed = discord.Embed(
            title="✅ Guild Task Completed!",
            description=f"The **{task_type}** task is done — nice work, guild!",
            color=COLOR_GUILD_TASK_COMPLETE,
        )
        embed.add_field(
            name="🎯 Final Tally", value=f"{target:,} / {target:,} (**100%**)", inline=False
        )
    else:
        embed = discord.Embed(
            title="📋 New Guild Task!",
            description=f"Type: **{task_type}**",
            color=COLOR_GUILD_TASK,
        )
        current = task.get("current_amount", 0)
        bar, pct = _progress_bar(current, target, fill="🟦")
        embed.add_field(
            name="📊 Progress",
            value=f"{bar} **{pct}%**\n{current:,} / {target:,}",
            inline=False,
        )

    embed.add_field(
        name="🎁 Reward",
        value=f"{format_number(exp_reward)} EXP  •  {format_number(pp_reward)} Power Points",
        inline=False,
    )

    return _finalize_embed(embed, "📣 Guild task update")


def create_guild_task_status_embed(task):
    task_type = str(task.get("type", "Unknown")).capitalize()
    current = task.get("current_amount", 0)
    target = task.get("target_amount", 0)
    bar, pct = _progress_bar(current, target, fill="🟦")

    embed = discord.Embed(title="📋 Guild Task Status", color=COLOR_GUILD_TASK)
    embed.add_field(name="Type", value=f"**{task_type}**", inline=True)
    embed.add_field(name="Progress", value=f"**{pct}%**", inline=True)
    embed.add_field(
        name="📊 Progress Bar", value=f"{bar}\n{current:,} / {target:,}", inline=False
    )
    embed.add_field(
        name="🎁 Reward",
        value=f"{format_number(task.get('exp_reward', 0))} EXP  •  "
        f"{format_number(task.get('power_point_reward', 0))} Power Points",
        inline=False,
    )

    return _finalize_embed(embed, "🔄 Live status — updates automatically")


def create_guild_task_status_embed_safe(task):
    """Same as create_guild_task_status_embed, but also handles the "no
    active task right now" case — used by the persistent status message,
    which (unlike the /task command) must always produce *something* to
    display rather than an error message."""
    if not is_valid_guild_task(task):
        embed = discord.Embed(
            title="📋 Guild Task Status",
            description="😴 No active guild task right now.",
            color=COLOR_NEUTRAL,
        )
        return _finalize_embed(embed, "🔄 Live status — updates automatically")

    return create_guild_task_status_embed(task)


def create_sanctuary_active_embed(tier):
    tier_name = tier.get("tier", {}).get("name", "Unknown Tier")

    embed = discord.Embed(
        title="🏛️ Sanctuary Tier Active!",
        description=f"**{tier_name}** is now the active guild sanctuary tier!",
        color=COLOR_SANCTUARY,
    )

    embed.add_field(
        name="✨ Active Bonuses",
        value=_safe_field_value(_bullet_list(tier.get("effects", []))),
        inline=False,
    )

    return _finalize_embed(embed, "📣 Sanctuary update")


def create_sanctuary_completed_embed(tier):
    tier_name = tier.get("tier", {}).get("name", "Unknown Tier")
    current = tier.get("current_value", 0)
    target = tier.get("target_value", 0)

    embed = discord.Embed(
        title="🏆 Sanctuary Tier Completed!",
        description=f"**{tier_name}** has reached its goal — well done, guild!",
        color=COLOR_SANCTUARY_COMPLETE,
    )

    embed.add_field(
        name="🎯 Final Tally",
        value=f"{format_number(current)} / {format_number(target)} (**100%**)",
        inline=False,
    )

    embed.add_field(
        name="✨ Effects Unlocked",
        value=_safe_field_value(_bullet_list(tier.get("effects", []))),
        inline=False,
    )

    return _finalize_embed(embed, "📣 Sanctuary update")


def create_sanctuary_status_embed(tiers):
    embed = discord.Embed(title="🏛️ Guild Sanctuary Status", color=COLOR_SANCTUARY)

    for tier in tiers:
        tier_name = tier.get("tier", {}).get("name", "Unknown Tier")
        current = tier.get("current_value", 0)
        target = tier.get("target_value", 0)
        pct = tier.get("percentage", 0)
        bar, _ = _progress_bar(current, target, fill="🟪")

        if tier.get("is_active"):
            status_icon, status_label = "🟢", "Active"
        elif tier.get("has_expired"):
            status_icon, status_label = "⌛", "Expired"
        elif tier.get("goal_reached_at") or pct >= 100:
            status_icon, status_label = "✅", "Goal Reached"
        elif tier.get("in_progress"):
            status_icon, status_label = "🔨", "In Progress"
        else:
            status_icon, status_label = "⚙️", "Not Started"

        value_lines = [
            f"*{status_label}*",
            f"{bar} **{pct}%**",
            f"{current:,} / {target:,}",
        ]

        embed.add_field(
            name=f"{status_icon}  {tier_name}",
            value=_safe_field_value("\n".join(value_lines)),
            inline=False,
        )

    return _finalize_embed(embed, "🔄 Live status — updates automatically")


def create_auth_failure_embed(count, endpoint):
    embed = discord.Embed(
        title="🚨 API Authentication Failing",
        description=(
            f"The bot has failed to authenticate with the SimpleMMO API "
            f"on `{endpoint}` **{count} times in a row**. The API key may be "
            f"invalid or expired — please check the `.env` configuration."
        ),
        color=COLOR_ERROR,
    )
    return _finalize_embed(embed, "🚨 Action required")


# ==========================================================
# BACKGROUND MONITOR
# ==========================================================


@tasks.loop(seconds=MONITOR_INTERVAL_SECONDS)
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

    channels = await resolve_notification_channels(
        [
            RAID_CHANNEL_ID,
            WORLDBOSS_CHANNEL_ID,
            ORPHANAGE_CHANNEL_ID,
            GUILD_TASK_CHANNEL_ID,
            SANCTUARY_CHANNEL_ID,
            ERROR_ALERT_CHANNEL_ID,
        ]
    )

    if not channels:
        # None of the configured channels could be resolved at all (e.g.
        # right after startup, or a total misconfiguration) — nothing to do
        # this tick, try again on the next one.
        logger.warning("No notification channels could be resolved this tick")
        return

    await cleanup_expired_notifications(channels)

    global raid_reminder_sent, no_raid_logged

    raw_raid = await get_raid()
    raid_fetch_ok = raw_raid is not None
    raid = raw_raid
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
            await send_notification(
                channels,
                RAID_CHANNEL_ID,
                content=ping_content,
                embed=create_raid_embed(raid),
                expire_seconds=RAID_START_NOTIFICATION_LIFETIME_SECONDS,
            )
            raid_reminder_sent = False
            persist_state()
        elif not raid_reminder_sent and raid_expiring_soon(raid):
            logger.info("Raid expiring soon, sending reminder")
            raid_expire_at = _iso_to_unix(raid.get("expires_at"))
            await send_notification(
                channels,
                RAID_CHANNEL_ID,
                embed=create_raid_reminder_embed(raid),
                expire_at=raid_expire_at,
                expire_seconds=None if raid_expire_at else DEFAULT_NOTIFICATION_LIFETIME_SECONDS,
            )
            raid_reminder_sent = True
            persist_state()

    if raid_fetch_ok:
        await upsert_status_message(
            channels, RAID_CHANNEL_ID, "raid", create_raid_status_embed(raid)
        )

    newly_active_orphanage, orphanage_tiers = await check_orphanage()
    if newly_active_orphanage:
        logger.info("New orphanage tier active, sending notification")
        await send_notification(
            channels,
            ORPHANAGE_CHANNEL_ID,
            embed=create_orphanage_embed(newly_active_orphanage),
            expire_seconds=DEFAULT_NOTIFICATION_LIFETIME_SECONDS,
        )
    if orphanage_tiers is not None:
        await upsert_status_message(
            channels,
            ORPHANAGE_CHANNEL_ID,
            "orphanage",
            create_orphanage_status_embed(orphanage_tiers),
        )

    boss_activated, boss_killed, boss_incoming, bosses = await check_worldboss()

    for boss in boss_activated:
        logger.info(f"World boss activated: {boss.get('name')}")
        boss_id = str(boss.get("id"))
        # No flat expiry here — this notification is deleted PRECISELY when
        # this same boss is confirmed killed, below. The long expire_seconds
        # is just a safety net in case that boss somehow never registers as
        # killed (e.g. it gets reset/removed by the API without ever
        # hitting 0 HP), so it doesn't linger forever either way.
        message = await send_notification(
            channels,
            WORLDBOSS_CHANNEL_ID,
            embed=create_worldboss_embed(boss, killed=False),
            expire_seconds=24 * 60 * 60,
        )
        if message is not None:
            worldboss_active_notification_ids[boss_id] = {
                "channel_id": WORLDBOSS_CHANNEL_ID,
                "message_id": message.id,
            }
            persist_state()

    for boss in boss_killed:
        logger.info(f"World boss killed: {boss.get('name')}")
        boss_id = str(boss.get("id"))

        # Remove the earlier "Active" notification for this exact boss right
        # now — it's stale info the instant the boss is dead, so there's no
        # reason to wait for its own timer.
        stale = worldboss_active_notification_ids.pop(boss_id, None)
        if stale:
            stale_channel = channels.get(stale["channel_id"])
            if stale_channel is not None:
                try:
                    stale_message = await stale_channel.fetch_message(stale["message_id"])
                    await stale_message.delete()
                except discord.NotFound:
                    pass  # already gone somehow — fine
                except discord.HTTPException as e:
                    logger.warning(f"Failed to delete stale 'boss active' notification: {e}")
            persist_state()

        await send_notification(
            channels,
            WORLDBOSS_CHANNEL_ID,
            embed=create_worldboss_embed(boss, killed=True),
            expire_seconds=DEFAULT_NOTIFICATION_LIFETIME_SECONDS,
        )

    if boss_incoming:
        logger.info(f"World boss incoming soon: {boss_incoming.get('name')}")
        # Expires the moment the boss actually spawns — by then the
        # "incoming" heads-up is no longer useful info.
        await send_notification(
            channels,
            WORLDBOSS_CHANNEL_ID,
            embed=create_worldboss_incoming_embed(boss_incoming),
            expire_at=boss_incoming.get("enable_time"),
        )
    if bosses is not None:
        await upsert_status_message(
            channels,
            WORLDBOSS_CHANNEL_ID,
            "worldboss",
            _update_worldboss_carousel(bosses),
            view=WorldBossCarouselView(),
        )

    task_event, task = await check_guild_task()
    if task_event:
        event_type, event_task = task_event
        if event_type == "new":
            logger.info("New guild task detected, sending notification")
            await send_notification(
                channels,
                GUILD_TASK_CHANNEL_ID,
                embed=create_guild_task_embed(event_task, completed=False),
                expire_seconds=DEFAULT_NOTIFICATION_LIFETIME_SECONDS,
            )
        elif event_type == "completed":
            logger.info("Guild task completed, sending notification")
            await send_notification(
                channels,
                GUILD_TASK_CHANNEL_ID,
                embed=create_guild_task_embed(event_task, completed=True),
                expire_seconds=DEFAULT_NOTIFICATION_LIFETIME_SECONDS,
            )
    if task is not None:
        await upsert_status_message(
            channels,
            GUILD_TASK_CHANNEL_ID,
            "guild_task",
            create_guild_task_status_embed_safe(task),
        )

    sanctuary_active, sanctuary_completed, sanctuary_tiers = await check_sanctuary()
    if sanctuary_active:
        logger.info(
            f"Sanctuary tier became active: {sanctuary_active.get('tier', {}).get('name')}"
        )
        await send_notification(
            channels,
            SANCTUARY_CHANNEL_ID,
            embed=create_sanctuary_active_embed(sanctuary_active),
            expire_seconds=DEFAULT_NOTIFICATION_LIFETIME_SECONDS,
        )
    for tier in sanctuary_completed:
        logger.info(
            f"Sanctuary tier goal reached: {tier.get('tier', {}).get('name')}"
        )
        await send_notification(
            channels,
            SANCTUARY_CHANNEL_ID,
            embed=create_sanctuary_completed_embed(tier),
            expire_seconds=DEFAULT_NOTIFICATION_LIFETIME_SECONDS,
        )
    if sanctuary_tiers is not None:
        await upsert_status_message(
            channels,
            SANCTUARY_CHANNEL_ID,
            "sanctuary",
            create_sanctuary_status_embed(sanctuary_tiers),
        )

    # Warn once per endpoint if the API key seems to be failing repeatedly
    # on that specific endpoint (see _handle_response for how counts accrue).
    # NOTE: intentionally no expire_at/expire_seconds here — this is an
    # operational alert, not a game event, so it should stick around until
    # a human notices and fixes the API key rather than quietly vanishing.
    for endpoint, count in list(consecutive_401_counts.items()):
        if count >= AUTH_FAILURE_THRESHOLD and endpoint not in auth_failure_notified_endpoints:
            logger.error(
                f"{count} consecutive auth failures on {endpoint}, notifying channel"
            )
            await send_notification(
                channels, ERROR_ALERT_CHANNEL_ID, embed=create_auth_failure_embed(count, endpoint)
            )
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

    # Re-registers the worldboss carousel's ◀ 🔄 ▶ buttons as a persistent
    # view. Without this, buttons on messages sent before a restart would
    # stop responding once the process restarts (discord.py forgets
    # non-persistent views on restart; this re-attaches by custom_id).
    bot.add_view(WorldBossCarouselView())

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
    # FIX: on_disconnect fires on ANY gateway disconnect, including
    # transient ones discord.py auto-reconnects from — not just real
    # shutdown. Closing the shared aiohttp session here could kill
    # in-flight SimpleMMO API requests for no reason. The session is
    # already closed properly in _graceful_shutdown() on real shutdown.
    logger.warning("Disconnected from Discord gateway (will attempt to reconnect automatically)")


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


async def _restrict_commands_to_channel(interaction: discord.Interaction) -> bool:
    """Global check applied to every slash command via bot.tree.interaction_check
    (see assignment below). If COMMANDS_CHANNEL_ID is configured, commands
    used anywhere else get a friendly ephemeral redirect instead of running.
    If COMMANDS_CHANNEL_ID is not set, this is a no-op and commands work
    anywhere, same as before this feature existed."""
    if COMMANDS_CHANNEL_ID is None or interaction.channel_id == COMMANDS_CHANNEL_ID:
        return True

    await interaction.response.send_message(
        f"🚫 Use this command in <#{COMMANDS_CHANNEL_ID}>.", ephemeral=True
    )
    return False


bot.tree.interaction_check = _restrict_commands_to_channel


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


@bot.tree.command(name="sanctuary", description="Show the current guild sanctuary status")
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


@bot.tree.command(name="worldboss", description="Show the current world boss (browsable)")
@discord.app_commands.checks.cooldown(1, 15.0)
async def worldboss_command(interaction: discord.Interaction):
    await interaction.response.defer()

    bosses = await get_worldboss()

    if bosses is None:
        await interaction.followup.send(
            "⚠️ Couldn't fetch world boss data from the API right now. Check the logs for details."
        )
        return

    embed = _update_worldboss_carousel(bosses)
    await interaction.followup.send(embed=embed, view=WorldBossCarouselView())


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
        await interaction.followup.send(embed=create_worldboss_incoming_embed(upcoming[0]))
        return

    embed = discord.Embed(
        title=f"⏳ Upcoming World Bosses ({len(upcoming)})", color=COLOR_RAID_WARNING
    )
    for boss in upcoming:
        embed.add_field(
            name=f"🐾 {boss.get('name', 'Unknown')}",
            value=_safe_field_value(
                f"Spawns **{format_unix_relative(boss.get('enable_time'))}**"
            ),
            inline=False,
        )
    await interaction.followup.send(embed=_finalize_embed(embed))


@bot.tree.command(name="status", description="Show bot status")
async def status(interaction: discord.Interaction):
    _prune_request_times()
    requests_count = len(request_times)
    bar, _ = _progress_bar(requests_count, MAX_REQUESTS_PER_MINUTE, length=10, fill="🟦")

    embed = discord.Embed(
        title="🤖 SimpleMMO Bot Status",
        description="🟢 Online and monitoring",
        color=COLOR_GUILD_TASK,
    )

    embed.add_field(name="Guild ID", value=f"`{GUILD_ID}`", inline=True)
    if last_check_time:
        embed.add_field(
            name="Last Check", value=f"<t:{int(last_check_time)}:R>", inline=True
        )

    embed.add_field(
        name="📡 API Requests (last minute)",
        value=f"{bar} **{requests_count}/{MAX_REQUESTS_PER_MINUTE}**",
        inline=False,
    )

    await interaction.response.send_message(embed=_finalize_embed(embed))


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
