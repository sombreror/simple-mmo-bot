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
RAID_ROLE_ID_RAW = os.getenv("RAID_ROLE_ID")  # opzionale: ruolo da pingare sui raid

# Validazione robusta delle variabili d'ambiente
errors = []

if DISCORD_TOKEN is None:
    errors.append("DISCORD_TOKEN missing in .env")

if API_KEY is None:
    errors.append("API_KEY missing in .env")
else:
    API_KEY = API_KEY.strip()  # rimuove eventuali spazi/newline accidentali

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

# RAID_ROLE_ID è opzionale: se assente o non valido, semplicemente non si pinga nessun ruolo
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

last_check_time = None
bot_start_time = time.time()
raid_reminder_sent = (
    False  # evita di mandare il promemoria di scadenza più volte per lo stesso raid
)
RAID_REMINDER_MINUTES_BEFORE = 10  # quanto tempo prima della scadenza avvisare


def persist_state():
    save_state(
        {"last_raid_started": last_raid_started, "last_orphanage": last_orphanage}
    )


# ==========================================================
# RATE LIMITER
# ==========================================================

# L'API di SimpleMMO ha un limite reale di 40 richieste/minuto
# (vedi header "x-ratelimit-limit: 40" nelle risposte).
# Teniamo un margine di sicurezza.
MAX_REQUESTS_PER_MINUTE = 35

request_times = deque()


async def rate_limit():
    now = time.time()

    while request_times and request_times[0] < now - 60:
        request_times.popleft()

    if len(request_times) >= MAX_REQUESTS_PER_MINUTE:
        wait_time = 60 - (now - request_times[0])
        logger.warning(f"Rate limit reached. Waiting {wait_time:.2f}s")
        await asyncio.sleep(wait_time)

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
# L'API pubblica di SimpleMMO (https://web.simple-mmo.com/p-api/home)
# richiede la api_key come QUERY PARAMETER nell'URL, non come header
# Authorization. Esempio confermato funzionante:
#
#   POST https://api.simple-mmo.com/v2/orphanage?api_key=XXXX
#
# (nessun header Authorization/Bearer, nessun body richiesto)


async def smmo_request(endpoint, method="POST"):
    """Make request to the SimpleMMO API using api_key as a query parameter."""

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
    if response.status == 429:
        logger.error(f"API rate limit hit (429) on {endpoint}")
        return None
    if response.status == 401:
        body_text = await response.text()
        logger.warning(f"Auth failed (401) on {endpoint} — body: {body_text}")
        return None
    if response.status == 405:
        body_text = await response.text()
        logger.error(f"Method not allowed (405) on {endpoint} — body: {body_text}")
        return None
    if response.status != 200:
        body_text = await response.text()
        logger.error(f"API Error: {response.status} on {endpoint} — body: {body_text}")
        return None
    return await response.json()


async def get_raid():
    return await smmo_request(f"/v1/guilds/raid/{GUILD_ID}")


async def get_orphanage():
    return await smmo_request("/v2/orphanage")


# ==========================================================
# RAID CHECK
# ==========================================================


def is_valid_raid(raid):
    """True solo se il raid ha dati reali: location(s) presenti ed expires_at presente.
    L'API restituisce comunque un oggetto quando non c'è nessun raid attivo
    (started_at/locations/expires_at vuoti o null): questo va trattato come
    'nessun raid', non come un nuovo raid da notificare."""
    if raid is None:
        return False

    locations = raid.get("locations")
    expires = raid.get("expires_at")

    return bool(locations) and bool(expires)


def is_new_raid(raid):
    """Confronta il raid corrente con l'ultimo notificato. Ritorna True se è nuovo."""
    global last_raid_started

    started = raid.get("started_at")

    if started != last_raid_started:
        last_raid_started = started
        persist_state()
        return True

    return False


def raid_expiring_soon(raid):
    """True se il raid è ancora attivo ma scade entro RAID_REMINDER_MINUTES_BEFORE minuti."""
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


# ==========================================================
# BACKGROUND MONITOR
# ==========================================================


@tasks.loop(minutes=1)
async def monitor():
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

    global raid_reminder_sent

    raid = await get_raid()
    if raid is not None and not is_valid_raid(raid):
        logger.info("No active raid (empty/placeholder data from API) — skipping")
        raid = None

    if raid is not None:
        if is_new_raid(raid):
            logger.info("New raid detected, sending notification")
            ping_content = f"<@&{RAID_ROLE_ID}>" if RAID_ROLE_ID else None
            await channel.send(content=ping_content, embed=create_raid_embed(raid))
            raid_reminder_sent = False
        elif not raid_reminder_sent and raid_expiring_soon(raid):
            logger.info("Raid expiring soon, sending reminder")
            await channel.send(
                f"⏰ Reminder: the current raid expires "
                f"{parse_timestamp(raid.get('expires_at'))} — get in before it's gone!"
            )
            raid_reminder_sent = True

    orphanage = await check_orphanage()
    if orphanage:
        logger.info("New orphanage event detected, sending notification")
        await channel.send(embed=create_orphanage_embed(orphanage))


# ==========================================================
# EVENTS
# ==========================================================


@bot.event
async def on_ready():
    logger.info(f"Bot connected as {bot.user}")
    logger.info(f"Guild ID: {GUILD_ID}")

    await bot.tree.sync()

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
        # Nessun tier attivo: mostriamo comunque quello con più progresso, per contesto
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


@bot.tree.command(name="status", description="Show bot status")
async def status(interaction: discord.Interaction):
    requests_count = len(request_times)

    embed = discord.Embed(title="🤖 SimpleMMO Bot Status", color=0x4444FF)

    embed.add_field(name="Status", value="🟢 Online", inline=False)
    embed.add_field(name="Guild ID", value=str(GUILD_ID), inline=False)
    embed.add_field(
        name="API Requests",
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
