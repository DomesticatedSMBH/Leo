import sys
import os
import random
from more_itertools import sliced
import discord
from discord import app_commands
from discord.ui import Button
from discord.ext import tasks, commands
from io import BytesIO
import re
import fastf1
from datetime import datetime, timedelta, timezone
from textwrap import wrap
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any, Literal
from discord import PartialEmoji
import json
import pytz 


sys.stdout.reconfigure(line_buffering=True)

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD = os.getenv('DISCORD_GUILD')
TEST_GUILD = os.getenv('DISCORD_TEST_GUILD')
STAFF_ID1 = int(os.getenv("STAFF_ID1"))
STAFF_ID2 = int(os.getenv("STAFF_ID2"))
ADMIN_IDS = [STAFF_ID1, STAFF_ID2]
MAX_POLL_HOURS = 32 * 24  # 32 days
F1_CACHE_PATH = ".fastf1cache"

intents = discord.Intents.all()
intents.members = True
if not os.path.exists(F1_CACHE_PATH):
    os.mkdir(F1_CACHE_PATH)  # Creates only the specified directory
    print(f"Directory '{F1_CACHE_PATH}' created.")

fastf1.Cache.enable_cache(".fastf1cache")

# Optional: set your server/default timezone for input like dd.mm.yyyy hh:mm
DEFAULT_TZ = pytz.utc

# In-memory schedule store; persisted to disk
SCHEDULES: List[Dict[str, Any]] = []
SCHEDULES_PATH = "schedules.json"

F1_CHANNELS = {
    "eventname": 1425959683264610394,   # e.g., "United States GP"
    "date": 1425959704307175494,        # e.g., "Sun 19 Oct 2025"
    # "time": 1425949737646821464,        # e.g., "20:00 BST"
    "countdown": 1425959786062545099,  # e.g., "in 9d Xh Ym"
}
MAX_CH_NAME = 100  # Discord hard limit

_SESSION_ORDER = ["FP1", "FP2", "FP3", "SQ", "S", "Q", "R"]
_SESSION_LABELS = {
    "FP1": "FP1",
    "FP2": "FP2",
    "FP3": "FP3",
    "SQ" : "Sprint Quali",  # use this wording regardless of year naming
    "S"  : "Sprint",
    "Q"  : "Quali",
    "R"  : "GRAND PRIX",
}

class aclient(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        intents.reactions = True
        self.synced = False

    async def on_ready(self):
        await client.change_presence(activity=discord.Game('with Charles'))
        await self.wait_until_ready()
        if not self.synced:
            await tree.sync(guild=discord.Object(id=GUILD))
            await tree.sync()
            self.synced = True
        if not scheduler_loop.is_running():
            scheduler_loop.start()
        await is_ready()
        print(f"{self.user} is ready!")
        # testing
        if not f1_clock_loop.is_running():
            f1_clock_loop.start()
        await _update_f1_channels_once()  # run once immediately at startup
        

client = aclient()
guild = discord.Object(id=GUILD)
test_guild = discord.Object(id=TEST_GUILD)
tree = app_commands.CommandTree(client)

async def is_ready():
    channel = client.get_channel(1421152351888085135)  # Replace with a valid channel ID
    embed = discord.Embed(
        title="Leo is up and ready!",
        color=0xff9117
    )
    await channel.send(embed=embed)

def _aware_utc(ts) -> datetime:
    if ts is None:
        return None
    py = ts.to_pydatetime()
    return py.replace(tzinfo=timezone.utc) if py.tzinfo is None else py.astimezone(timezone.utc)

def _fmt_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(DEFAULT_TZ).strftime("%a %d %b %Y • %H:%M UTC")

def _countdown(dt_utc: datetime) -> str:
    now = datetime.now(timezone.utc)
    s = int((dt_utc - now).total_seconds())
    if s <= 0: return "started"
    d, r = divmod(s, 86400); h, r = divmod(r, 3600); m, _ = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if d or h: parts.append(f"{h}h")
    parts.append(f"<{m}m")
    return "in " + " ".join(parts)

def _add_session(embed, event, idents: list[str], label: str):
    for ident in idents:
        try:
            dt = event.get_session_date(ident, utc=True)
            if dt is None:
                continue
            dt_utc = _aware_utc(dt)
            embed.add_field(name=label, value=f"{_fmt_local(dt_utc)}\n{_countdown(dt_utc)}", inline=False)
            return
        except Exception:
            continue

def _is_testing_row(row) -> bool:
    # Filter out all testing-like rows regardless of column naming
    fields = []
    for col in ("EventName", "OfficialEventName", "EventFormat", "EventType", "Name"):
        if col in row:
            fields.append(str(row[col]))
    s = " ".join(fields).lower()
    return "test" in s  # matches "testing", "pre-season test", etc.

def _iter_race_rounds(schedule):
    rounds = []
    col = schedule["RoundNumber"]
    for x in col:
        xs = str(x).strip()
        if xs.isdigit():  # numeric rounds only
            rounds.append(int(xs))
    # unique + sorted
    return sorted(set(rounds))

def _short_event_label(ev: dict) -> str:
    """
    Shorten the event name per your rules. Fallback: country, else EventName.
    Examples:
      - Austin race => "United States"
      - Monza (Italian GP) => "Italy"
      - Emilia Romagna (Imola) => "Imola"
    """
    name = (ev.get("EventName") or ev.get("OfficialEventName") or "").strip()
    country = (ev.get("Country") or "").strip()
    location = (ev.get("Location") or "").strip()
    nlow = name.lower()
    llow = location.lower()

    # Explicit overrides
    if "emilia" in nlow or "romagna" in nlow or "imola" in llow:
        return "Imola"
    if ("italian" in nlow or "italy" in nlow) and "monza" in llow:
        return "Monza"
    if "united states" in nlow and ("austin" in llow or "cota" in nlow):
        return "CIRCUIT OF THE AMERICAS"
    if "united states" in nlow and "miami" in llow:
        return "Miami International Autodrome"
    if "united states" in nlow and "las vegas" in llow:
        return "Las Vegas Street Circuit"
    if "british" in nlow and "silverstone" in llow:
        return "Silverstone"
    # General fallback: prefer country when present
    return country or name or "Grand Prix"

def _iter_existing_session_datetimes(ev, *, utc=True):
    """
    Yield (session_code, dt) for sessions that exist on this event.
    Handles missing sessions safely.
    """
    for code in _SESSION_ORDER:
        try:
            dt = ev.get_session_date(code, utc=utc)
        except Exception:
            dt = None
        if dt:
            # Ensure tz-aware UTC datetime
            yield code, _aware_utc(dt)

def _find_next_session():
    """
    Return (ev, session_code, session_dt_utc).
    Scans this year, then next year if needed. Skips testing.
    """
    now = datetime.now(timezone.utc)
    year = now.year
    for _ in range(2):  # try current, then next season
        try:
            schedule = fastf1.get_event_schedule(year, include_testing=False)
        except TypeError:
            schedule = fastf1.get_event_schedule(year)
        sched_no_testing = schedule.loc[~schedule.apply(_is_testing_row, axis=1)]

        candidates = []
        for rnd in _iter_race_rounds(sched_no_testing):
            try:
                ev = schedule.get_event_by_round(rnd)
            except Exception:
                continue
            for code, dt_utc in _iter_existing_session_datetimes(ev, utc=True):
                if dt_utc and dt_utc > now:
                    # Earlier sessions take precedence naturally by datetime
                    # If two sessions share identical dt (shouldn't), session order breaks ties
                    order_idx = _SESSION_ORDER.index(code)
                    candidates.append((dt_utc, order_idx, code, ev))

        if candidates:
            dt_utc, _, code, ev = min(candidates, key=lambda x: (x[0], x[1]))
            return ev, code, dt_utc
        year += 1
    return None, None, None

def _format_f1_channel_strings_for_session(ev, session_code, session_dt_utc):
    """
    Build the 4 channel strings for the next session.
    eventname: "<Short Event> – <SESSION LABEL>"
    date:      "Sun 19 Oct 2025 20:00 BST"
    countdown: "in 9d Xh <Ym"
    """
    short_event = _short_event_label(ev)
    session_label = _SESSION_LABELS.get(session_code, session_code)
    local_dt = session_dt_utc.astimezone(DEFAULT_TZ)

    name = f"{short_event} – {session_label}"
    date_str = local_dt.strftime("%a %d %b %Y")
    time_str = local_dt.strftime("%H:%M UTC")
    countdown_str = _countdown(session_dt_utc)

    clip = lambda s: s[:MAX_CH_NAME]
    return clip(name), clip(date_str), clip(time_str), clip(countdown_str)

async def _update_f1_channels_once():
    try:
        ev, code, sdt = _find_next_session()
        if ev is None or code is None or sdt is None:
            print("[f1clock] No upcoming session found.")
            return

        eventname, date_str, time_str, countdown_str = _format_f1_channel_strings_for_session(ev, code, sdt)

        desired = {
            F1_CHANNELS["eventname"]: eventname,
            F1_CHANNELS["date"]: date_str + " • " + time_str,
            F1_CHANNELS["countdown"]: countdown_str,
        }

        for cid, target in desired.items():
            ch = client.get_channel(cid)
            if not ch:
                print(f"[f1clock] Channel not found: {cid}")
                continue
            if ch.name != target:
                try:
                    await ch.edit(name=target, reason="F1 next session hourly update")
                except Exception as e:
                    print(f"[f1clock] edit failed for {cid}: {e}")
    except Exception as e:
        print(f"[f1clock] update error: {e}")

def _find_next_race():
    """
    Returns (event, race_dt_utc) for the next race using your f1_next logic.
    """
    now = datetime.now(timezone.utc)
    year = now.year
    for _ in range(2):  # this year, else next
        try:
            schedule = fastf1.get_event_schedule(year, include_testing=False)
        except TypeError:
            schedule = fastf1.get_event_schedule(year)  # older fastf1

        sched_no_testing = schedule.loc[~schedule.apply(_is_testing_row, axis=1)]
        candidates = []
        for rnd in _iter_race_rounds(sched_no_testing):
            try:
                ev = schedule.get_event_by_round(rnd)
            except Exception as e:
                if "testing" in str(e).lower():
                    continue
                continue
            r_dt = ev.get_session_date("R", utc=True)
            r_dt_utc = _aware_utc(r_dt)
            if (r_dt_utc is not None) and (r_dt_utc > now):
                candidates.append((r_dt_utc, ev))

        if candidates:
            r_dt_utc, ev = min(candidates, key=lambda x: x[0])
            return ev, r_dt_utc
        year += 1
    return None, None

def _format_f1_channel_strings(ev, race_dt_utc):
    """
    Builds the 4 strings for channel names, using your DEFAULT_TZ and countdown helper.
    """
    # Event name
    name = ev.get("OfficialEventName", None) or ev.get("EventName", "Grand Prix")

    # Localized date/time strings
    local_dt = race_dt_utc.astimezone(DEFAULT_TZ)
    date_str = local_dt.strftime("%a %d %b %Y")        # e.g., "Sun 19 Oct 2025"
    time_str = local_dt.strftime("%H:%M UTC")           # e.g., "20:00 BST"

    # Countdown
    countdown_str = _countdown(race_dt_utc)            # e.g., "in 9d Xh Ym" / "started"

    # Trim to Discord channel name limit just in case
    def _clip(s): return s[:MAX_CH_NAME]
    return _clip(name), _clip(date_str), _clip(time_str), _clip(countdown_str)

def load_schedules():
    global SCHEDULES
    try:
        with open(SCHEDULES_PATH, "r", encoding="utf-8") as f:
            SCHEDULES = json.load(f)
    except FileNotFoundError:
        SCHEDULES = []
    except Exception as e:
        print(f"[schedule] load error: {e}")
        SCHEDULES = []

def save_schedules():
    try:
        with open(SCHEDULES_PATH, "w", encoding="utf-8") as f:
            json.dump(SCHEDULES, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[schedule] save error: {e}")

def parse_when(s: str) -> datetime:
    # expects 'dd.mm.yyyy hh:mm'
    dt = datetime.strptime(s, "%d.%m.%Y %H:%M")
    # treat as DEFAULT_TZ local time -> convert to UTC for storage
    localized = DEFAULT_TZ.localize(dt)
    return localized.astimezone(pytz.utc)

def parse_duration(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    s = s.strip().lower()

    # Flexible forms: Nh / Nd / Nw, optionally combined and/or spaced: "36h", "10d", "2w", "1w2d", "2d 6h"
    tokens = re.findall(r'(\d+)\s*([hdw])', s)
    if not tokens:
        return None

    total_hours = 0
    for num, unit in tokens:
        n = int(num)
        if unit == 'h':
            total_hours += n
        elif unit == 'd':
            total_hours += n * 24
        elif unit == 'w':
            total_hours += n * 7 * 24

    if total_hours <= 0:
        return None

    # Clamp to API practical maximum (~32 days)
    if total_hours > MAX_POLL_HOURS:
        total_hours = MAX_POLL_HOURS

    # Return seconds (your job storage uses seconds; native polls convert with timedelta later)
    return total_hours * 3600

@tasks.loop(seconds=30)
async def scheduler_loop():
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    due: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []
    for job in SCHEDULES:
        run_at = datetime.fromisoformat(job["run_at"]).astimezone(pytz.utc)
        if run_at <= now_utc:
            due.append(job)
        else:
            remaining.append(job)

    for job in due:
        try:
            channel = client.get_channel(job["channel_id"])
            if not channel:
                print(f"[schedule] channel not found: {job['channel_id']}")
                continue

            if job["kind"] == "message":
                await channel.send(job["content"])
            else:
                # --- native poll (discord.py ≥ 2.4) ---
                p = discord.Poll(
                    question=job["question"],                         # str is fine
                    duration=timedelta(seconds=job["duration_s"]),    # must be timedelta
                    multiple=bool(job["allow_multi"])                 # allow multi-select
                )

                # add answers (with optional emoji per answer)
                emojis = job.get("emojis", []) or []
                for i, text in enumerate(job["options"]):
                    kwargs = {"text": text}
                    if i < len(emojis) and emojis[i]:
                        try:
                            # supports unicode (e.g., "👍") or custom "<:name:id>"
                            kwargs["emoji"] = PartialEmoji.from_str(emojis[i])
                        except Exception:
                            # if parse fails but it's a plain unicode emoji, pass the string
                            kwargs["emoji"] = emojis[i]
                    p.add_answer(**kwargs)

                await channel.send(poll=p)   # <- native poll send
        except Exception as e:
                print(f"[schedule] run error: {e}")

    # Replace storage only if we executed something
    if due:
        SCHEDULES.clear()
        SCHEDULES.extend(remaining)
        save_schedules()

# === [ADD A TASK LOOP NEAR YOUR OTHER TASKS] =================================
@tasks.loop(minutes=5, reconnect=True)
async def f1_clock_loop():
    await _update_f1_channels_once()

@f1_clock_loop.before_loop
async def _f1_clock_before_loop():
    await client.wait_until_ready()

@tree.command(name="f1_next", description="Countdowns to the next F1 GP weekend")
async def f1_next(interaction: discord.Interaction):
    try:
        now = datetime.now(timezone.utc)
        year = now.year
        next_event = None
        next_race_dt = None

        for _ in range(2):  # this year, else next
            try:
                schedule = fastf1.get_event_schedule(year, include_testing=False)  # newer fastf1
            except TypeError:
                schedule = fastf1.get_event_schedule(year)  # older fastf1 (may include testing)

            candidates = []
            # drop testing rows early (works across fastf1 versions)
            sched_no_testing = schedule.loc[~schedule.apply(_is_testing_row, axis=1)]

            for rnd in _iter_race_rounds(sched_no_testing):
                try:
                    ev = schedule.get_event_by_round(rnd)
                except Exception as e:
                    # if fastf1 still complains about testing, skip
                    if "testing" in str(e).lower():
                        continue
                    continue

                r_dt = ev.get_session_date("R", utc=True)
                r_dt_utc = _aware_utc(r_dt)
                if (r_dt_utc is not None) and (r_dt_utc > now):
                    candidates.append((r_dt_utc, ev))

            if candidates:
                next_race_dt, next_event = min(candidates, key=lambda x: x[0])
                break
            year += 1

        if next_event is None:
            await interaction.response.send_message("No upcoming race found.", ephemeral=True)
            return

        name = next_event.get("OfficialEventName", None) or next_event.get("EventName", "Grand Prix")
        loc  = next_event.get("Location", "") or ""
        ctry = next_event.get("Country", "") or ""
        where = " • ".join(x for x in [loc, ctry] if x)

        embed = discord.Embed(
            title=f"Next F1 Weekend: {name}",
            description=f"**Race:** {_fmt_local(next_race_dt)}\n{_countdown(next_race_dt)}",
            color=0xE10600
        )
        if where:
            embed.set_footer(text=where)

        # Sessions (try multiple identifiers for robustness across seasons/FF1 versions)
        _add_session(embed, next_event, ["Practice 1", "FP1"], "Free Practice 1")
        _add_session(embed, next_event, ["Practice 2", "FP2"], "Free Practice 2")
        _add_session(embed, next_event, ["Practice 3", "FP3"], "Free Practice 3")
        _add_session(embed, next_event, ["Sprint Shootout", "Sprint Qualifying", "SQ"], "Sprint Qualifying")
        _add_session(embed, next_event, ["Sprint"], "Sprint")
        _add_session(embed, next_event, ["Qualifying", "Q"], "Qualifying")

        await interaction.response.send_message(embed=embed)

    except Exception as e:
        await interaction.response.send_message(f"Failed to fetch FastF1 schedule: {e}", ephemeral=True)

@tree.command(name="schedule", description="Schedule a message or a poll", guild=guild)
@app_commands.describe(
    kind="Type: message or poll",
    when="Time as dd.mm.yyyy hh:mm (London time)",
    channel="Target text channel (default: current)",
    content="Message content (for kind=message) or poll question (for kind=poll)",
    answers="For polls: comma-separated list of answers",
    emojis="Optional: comma-separated emojis matching answers; supports <:name:id>",
    multi="For polls: allow multiple answers (default false)",
    duration="For polls: Nh/Nd/Nw, max. 32 days (default 24h)"
)
async def schedule(
    interaction: discord.Interaction,
    kind: Literal["message", "poll"] = "message",
    when: str = None,
    channel: discord.TextChannel = None,
    content: str = None,
    answers: Optional[str] = None,
    emojis: Optional[str] = None,
    multi: Optional[bool] = False,
    duration: Optional[str] = None
):
    print(f"/schedule by {interaction.user} ({interaction.user.id})")
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("Unauthorised.", ephemeral=True)
        return
    try:
        run_at_utc = parse_when(when)
    except Exception:
        await interaction.response.send_message("Invalid time. Use dd.mm.yyyy hh:mm.", ephemeral=True)
        return

    if kind not in ("message", "poll"):
        await interaction.response.send_message("kind must be 'message' or 'poll'.", ephemeral=True)
        return
   
  # 2) pick target channel
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message("Target must be a text channel.", ephemeral=True)
        return

    # 3) use target_channel everywhere below
    job: Dict[str, Any] = {
        "id": f"{int(datetime.utcnow().timestamp()*1000)}-{interaction.id}",
        "kind": kind,
        "guild_id": interaction.guild.id if interaction.guild else None,
        "channel_id": target_channel.id,   # <-- was channel.id
        "run_at": run_at_utc.isoformat(),
        "created_by": interaction.user.id
    }

    if kind == "message":
        job["content"] = content
    else:
        # poll
        if not answers:
            await interaction.response.send_message("Provide answers for the poll (comma-separated).", ephemeral=True)
            return
        opts = [a.strip() for a in answers.split(",") if a.strip()]
        if len(opts) < 2 or len(opts) > 25:
            await interaction.response.send_message("Poll needs 2–25 answers.", ephemeral=True)
            return
        emoji_list = []
        if emojis:
            emoji_list = [e.strip() for e in emojis.split(",")]
            if len(emoji_list) != len(opts):
                await interaction.response.send_message("Number of emojis must match number of answers.", ephemeral=True)
                return
        dur_s = parse_duration(duration) if duration else parse_duration("24h")
        if not dur_s:
            await interaction.response.send_message("Invalid duration. Usage: e.g., 1h or 48h or 3w5d13h; max. 32d", ephemeral=True)
            return

        job.update({
            "question": content,
            "options": opts,
            "emojis": emoji_list,
            "allow_multi": bool(multi),
            "duration_s": dur_s
        })

    # Store and persist
    SCHEDULES.append(job)
    save_schedules()

    # Acknowledge
    local_time = run_at_utc.astimezone(DEFAULT_TZ).strftime("%d.%m.%Y %H:%M UTC")
    await interaction.response.send_message(
        f"Scheduled {kind} for <#{target_channel.id}> at {local_time}. ID: `{job['id']}`",
        ephemeral=True
    )

@tree.command(name="schedule_list", description="List all scheduled messages and polls", guild=guild)
async def schedule_list(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("Unauthorised.", ephemeral=True)
        return

    if not SCHEDULES:
        await interaction.response.send_message("No scheduled items.", ephemeral=True)
        return

    embed = discord.Embed(title="Scheduled Items", color=0x00AAFF)
    for job in SCHEDULES:
        dt_local = datetime.fromisoformat(job["run_at"]).astimezone(DEFAULT_TZ)
        time_str = dt_local.strftime("%d.%m.%Y %H:%M")
        kind = job["kind"]
        channel_id = job.get("channel_id", "unknown")
        embed.add_field(
            name=f"ID: {job['id']}",
            value=f"**Type:** {kind}\n**Channel:** <#{channel_id}>\n**When:** {time_str}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="schedule_remove", description="Remove a scheduled message or poll by ID", guild=guild)
async def schedule_remove(interaction: discord.Interaction, job_id: str):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("Unauthorised.", ephemeral=True)
        return

    global SCHEDULES
    before = len(SCHEDULES)
    SCHEDULES = [job for job in SCHEDULES if job["id"] != job_id]
    if len(SCHEDULES) < before:
        save_schedules()
        await interaction.response.send_message(f"Removed scheduled job `{job_id}`.", ephemeral=True)
    else:
        await interaction.response.send_message(f"No scheduled job found with ID `{job_id}`.", ephemeral=True)

@tree.context_menu(name='Report Message', guild=guild)
async def report_message(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.send_message(
        f'This message by {message.author.mention} has been reported to our staff.', ephemeral=True
    )

    log_channel = interaction.guild.get_channel(1421156169308700874)

    embed = discord.Embed(title='Reported Message')
    if message.content:
        embed.description = message.content
    try:
        embed.set_thumbnail(url = message.attachments[0].url)
    except:
        print("")
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.timestamp = message.created_at
    url_view = discord.ui.View()
    url_view.add_item(discord.ui.Button(label='Go to Message', style=discord.ButtonStyle.url, url=message.jump_url))

    await log_channel.send(embed=embed, view=url_view) 

@client.event
async def on_raw_reaction_add(payload):
#print(payload.emoji)
    if payload.emoji.name == "✉️":
        #print("Working?")
        channel = client.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        user = client.get_user(payload.user_id)
        try:
            await payload.member.send(message.content)
        except:
            print("no text")
        for attachment in message.attachments:
            await payload.member.send(attachment.url)
        print(payload.emoji.name)

replacements = {
    'x.com': 'fixupx.com',
    'instagram.com': 'ddinstagram.com',
    'twitter.com': 'vxtwitter.com',
    'reddit.com': 'rxddit.com'
}

# Precompile regex once (outside message loop)
escaped_domains = map(re.escape, replacements.keys())
pattern = re.compile(
    r'\bhttps://(www\.)?(' + '|'.join(escaped_domains) + r')\b',
    flags=re.IGNORECASE
)

def replace_domain(match):
    return f'https://{replacements[match.group(2)]}'
        
@client.event
async def on_message(message):
    # Ignore bot's own messages
    if message.author == client.user:
        return
    result = ""
    if pattern.search(message.content):
        result = pattern.sub(replace_domain, message.content)
        channel = client.get_channel(message.channel)
        # member = client.get_user(message.id)
        name = message.author.nick
        avatar_url = message.author.avatar
        webhook = await message.channel.create_webhook(name=name)
        await webhook.send(str(result), username=name, avatar_url=avatar_url)

        webhooks = await message.channel.webhooks()
        for webhook in webhooks:
            await webhook.delete()
        await message.delete()
    # Proceed only if the bot is mentioned.
    # Only act if bot is mentioned
    if client.user in message.mentions:
        parts = message.content.split(maxsplit=3)

        # Command: @botname delete [channelid] [messageid]
        if len(parts) >= 4 and parts[1].lower() == "delete":
            if message.author.id in ADMIN_IDS:
                try:
                    channel_id = int(parts[2])
                    msg_id = int(parts[3])

                    target_channel = client.get_channel(channel_id)
                    if not target_channel:
                        await message.reply("Invalid channel ID.")
                        return

                    target_msg = await target_channel.fetch_message(msg_id)
                    await target_msg.delete()
                    await message.delete()
                except Exception as e:
                    await message.channel.send(f"Error: {e}")

        elif message.author.id not in ADMIN_IDS:
            responses = ["Woof?", "Bark!", "Arf arf!", "Grrr...", "Yip!"]
            await message.reply(random.choice(responses))
        

@tree.context_menu(name="Forward Message to DMs", guild=guild)
async def forward_message(interaction: discord.Interaction, message: discord.Message):
    count = 0
    try:
        await interaction.user.send(message.content)
    except:
        print("no text")
    for attachment in message.attachments:
        await interaction.user.send(message.attachments[count].url)
        count += 1
    if message.attachments or message.content:
        await interaction.response.send_message(f'Successfully forwarded {message.author.mention}\'s message to your DMs.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Something went wrong.', ephemeral=True)

client.run(TOKEN)