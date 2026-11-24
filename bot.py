"""
PS99 Stats Bot  (discord.py 2.3+, Python 3.11+)

    pip install "discord.py>=2.3" aiohttp
    .env file next to this script:
        DISCORD_TOKEN=your-token
        GUILD_ID=your-server-id        (optional, instant command sync)
    python bot.py

Commands
    /help                           what every command does, in plain words
    /leaderboard [board] [user]     dropdown -> type a username -> rank card + forecast
    /league [name] [user] [season]  league card, pace vs the Top-100 line, ETA
    /clan [name] [user]             clan card, pace vs the Top-50 line, ETA
    /battle  /season                overviews with cutoffs and rewards
    /player <username>              avatar, public profile, clan and league standing
    /new                            latest game update
    /rap /lowrap /toprap            RAP, cheapest Huge/Titanic/Gargantuan, most valuable

How forecasts work
    Pace = points per hour. The bot compares your pace with the pace of whoever sits
    on the target line (Top 100 league, Top 50 clan, next leaderboard milestone).
    Leagues and clans use the season/battle average until the bot has 20+ minutes of
    its own samples, then switch to the recent trend. Leaderboards have no public start
    time, so they need the trend: the first lookup starts tracking, check again ~20 min later.
"""
import asyncio
import datetime as dt
import logging
import os
import re
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

BASE = "https://ps99.biggamesapi.io"
UA = {"User-Agent": "ps99-stats-discord-bot/3.0"}
CACHE_TTL = 60.0
ROBLOX_TTL = 3600.0
TIMEOUT_SECONDS = 15.0
MAX_RETRIES = 3
LEAGUE_TOP_N = 100
CLAN_TOP_N = 50
SAMPLE_INTERVAL_MINUTES = 10
TREND_MIN_SECONDS = 1200
MILESTONES = [1, 3, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

BLURPLE, GREEN, RED, GOLD, ORANGE, PINK, GREY = 0x5865F2, 0x57F287, 0xED4245, 0xFEE75C, 0xFAA61A, 0xEB459E, 0x95A5A6
STATE_EMOJI = {"live": "🟢", "ended": "🟡", "final": "🏁", "inactive": "⚪", "upcoming": "🕒", "past": "🏁"}
MEDAL_EMOJI = {"gold": "🥇", "silver": "🥈", "bronze": "🥉", "good": "🎖️"}
VARIANT_LABEL = {
    "regular": "", "golden": "Golden", "rainbow": "Rainbow", "shiny": "Shiny",
    "shinyGold": "Shiny Golden", "shinyRainbow": "Shiny Rainbow", "unknown": "",
}

log = logging.getLogger("ps99bot")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# =========================================================================== formatting

def fmt_int(value: Any) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "?"


def fmt_compact(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.2f}".rstrip("0").rstrip(".") + suffix
    return f"{value:,.0f}"


def fmt_duration(hours: float) -> str:
    total_minutes = max(0, int(round(hours * 60)))
    days, rem = divmod(total_minutes, 1440)
    hrs, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hrs}h"
    if hrs:
        return f"{hrs}h {mins}m"
    return f"{mins}m"


def progress_bar(fraction: float, width: int = 16) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "▰" * filled + "▱" * (width - filled)


def asset_url(icon: str | None) -> str | None:
    match = re.search(r"(\d{5,})", icon or "")
    return f"{BASE}/image/{match.group(1)}" if match else None


def flag(code: str | None) -> str:
    if code and len(code) == 2 and code.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code.upper())
    return ""


def trunc(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def rank_badge(rank: int | None) -> str:
    if rank is None:
        return "—"
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"#{rank}")


def when_rel(ts: float) -> str:
    return f"<t:{int(ts)}:R>"


def when_full(ts: float) -> str:
    return f"<t:{int(ts)}:f>"


def table(rows: list[list[str]], header: list[str] | None = None) -> str:
    data = ([header] if header else []) + rows
    widths = [max(len(str(r[i])) for r in data) for i in range(len(data[0]))]
    lines = ["   ".join(str(c).ljust(widths[i]) if i == 0 else str(c).rjust(widths[i]) for i, c in enumerate(r)) for r in data]
    return "```\n" + "\n".join(lines) + "\n```"


def fmt_value(value: Any, codec: str | None) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    if codec and codec.startswith("Race"):
        return f"{value:,.0f} (race-encoded)"
    if value > 2_000_000_000:
        return f"{2_147_483_647 - value:,.0f} (inverted time, lower = faster)"
    return f"{value:,.0f}"


def reward_text(item: dict[str, Any]) -> str:
    label = VARIANT_LABEL.get(item.get("variant", ""), "")
    name = f"{label} {item.get('id', '?')}".strip()
    amount = item.get("amount", 1)
    return f"{amount:,}× {name}" if amount and amount != 1 else name


def band_for(meta: dict[str, Any], rank: int | None) -> dict[str, Any] | None:
    if rank is None:
        return None
    for band in meta.get("placementRewards") or []:
        best, worst = band.get("best"), band.get("worst")
        if best is not None and best <= rank <= (worst if worst is not None else best):
            return band
    return None


def band_text(band: dict[str, Any] | None) -> str:
    if not band:
        return "No placement reward"
    items = ", ".join(reward_text(i) for i in band.get("items") or [])
    return f"**{band.get('placement', '?')}** → {items}"


def footer(embed: discord.Embed, extra: str = "") -> discord.Embed:
    embed.set_footer(text=trunc(f"PS99 Stats • data refreshes every ~60s{(' • ' + extra) if extra else ''}", 2000))
    embed.timestamp = dt.datetime.now(dt.timezone.utc)
    return embed


# =========================================================================== http

class Http:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[Any, tuple[float, Any]] = {}
        self.locks: dict[Any, asyncio.Lock] = {}
        self.roblox_cache: dict[Any, tuple[float, Any]] = {}

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS), headers=UA)

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def _request(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                       json: Any = None) -> tuple[int, Any]:
        assert self.session is not None
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self.session.request(method, url, params=params, json=json) as resp:
                    if resp.status == 429:
                        delay = float(resp.headers.get("Retry-After", 2 * attempt))
                        log.warning("429 from %s, sleeping %.1fs", url, delay)
                        await asyncio.sleep(min(delay, 20))
                        continue
                    if resp.status >= 500 and attempt < MAX_RETRIES:
                        await asyncio.sleep(attempt)
                        continue
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        body = None
                    return resp.status, body
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                await asyncio.sleep(attempt)
        raise ApiError(f"Couldn't reach the game API ({last_error or 'rate limited'}). Try again in a minute.")

    async def api(self, path: str, params: dict[str, Any] | None = None, ttl: float = CACHE_TTL) -> Any:
        key = (path, tuple(sorted((params or {}).items())))
        hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self.cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            status, body = await self._request("GET", BASE + path, params=params)
            if isinstance(body, dict) and body.get("status") == "ok":
                self.cache[key] = (time.monotonic(), body.get("data"))
                if len(self.cache) > 2000:
                    for old in sorted(self.cache, key=lambda k: self.cache[k][0])[:500]:
                        self.cache.pop(old, None)
                return body.get("data")
            err = (body or {}).get("error") if isinstance(body, dict) else None
            message = (err or {}).get("message") or (err or {}).get("code") or f"HTTP {status}"
            raise ApiError(str(message), status)

    def clear_cache(self) -> None:
        self.cache.clear()

    async def roblox_user(self, text: str) -> dict[str, Any]:
        text = text.strip().lstrip("@")
        key = ("user", text.lower())
        hit = self.roblox_cache.get(key)
        if hit and time.monotonic() - hit[0] < ROBLOX_TTL:
            return hit[1]
        _, body = await self._request("POST", "https://users.roblox.com/v1/usernames/users",
                                      json={"usernames": [text], "excludeBannedUsers": False})
        rows = (body or {}).get("data") or []
        user = None
        if rows:
            row = rows[0]
            user = {"id": int(row["id"]), "name": row["name"], "displayName": row.get("displayName") or row["name"]}
        elif text.isdigit():
            status, body = await self._request("GET", f"https://users.roblox.com/v1/users/{text}")
            if status == 200 and body:
                user = {"id": int(body["id"]), "name": body["name"], "displayName": body.get("displayName") or body["name"]}
        if not user:
            raise ApiError(f"No Roblox user named `{text}`. Check the spelling.")
        self.roblox_cache[key] = (time.monotonic(), user)
        return user

    async def headshots(self, ids: list[int]) -> dict[int, str]:
        result: dict[int, str] = {}
        missing: list[int] = []
        for uid in ids:
            hit = self.roblox_cache.get(("shot", uid))
            if hit and time.monotonic() - hit[0] < ROBLOX_TTL:
                result[uid] = hit[1]
            else:
                missing.append(uid)
        for start in range(0, len(missing), 100):
            chunk = missing[start:start + 100]
            try:
                _, body = await self._request(
                    "GET", "https://thumbnails.roblox.com/v1/users/avatar-headshot",
                    params={"userIds": ",".join(map(str, chunk)), "size": "420x420", "format": "Png", "isCircular": "false"})
            except ApiError:
                continue
            for row in (body or {}).get("data") or []:
                if row.get("imageUrl"):
                    uid = int(row["targetId"])
                    result[uid] = row["imageUrl"]
                    self.roblox_cache[("shot", uid)] = (time.monotonic(), row["imageUrl"])
        return result


http = Http()


# =========================================================================== pace + forecast

class Tracker:
    def __init__(self) -> None:
        self.samples: dict[str, list[tuple[float, float]]] = defaultdict(list)

    def record(self, key: str, value: float) -> None:
        now = time.time()
        series = self.samples[key]
        if series and now - series[-1][0] < 240:
            return
        series.append((now, float(value)))
        while series and series[0][0] < now - 6 * 3600:
            series.pop(0)

    def rate(self, key: str, current: float) -> tuple[float, int] | None:
        now = time.time()
        eligible = [(t, v) for t, v in self.samples.get(key, []) if TREND_MIN_SECONDS <= now - t <= 3 * 3600]
        if not eligible:
            return None
        t, v = eligible[0]
        hours = (now - t) / 3600
        return max(0.0, (current - v) / hours), int(hours * 60)

    def ready_at(self, key: str) -> float | None:
        series = self.samples.get(key)
        return series[0][0] + TREND_MIN_SECONDS if series else None


tracker = Tracker()


@dataclass
class Pace:
    you: float
    line: float
    avg_you: float
    avg_line: float
    trend_you: float | None
    trend_line: float | None
    trend_min: int | None
    basis: str


def compute_pace(self_key: str, points: float, self_hours: float,
                 line_key: str, line_points: float, line_hours: float) -> Pace:
    t_you, t_line = tracker.rate(self_key, points), tracker.rate(line_key, line_points)
    tracker.record(self_key, points)
    tracker.record(line_key, line_points)
    avg_you, avg_line = points / max(self_hours, 0.25), line_points / max(line_hours, 0.25)
    if t_you and t_line:
        minutes = min(t_you[1], t_line[1])
        return Pace(t_you[0], t_line[0], avg_you, avg_line, t_you[0], t_line[0], minutes, f"recent trend (last {minutes} min)")
    return Pace(avg_you, avg_line, avg_you, avg_line, None, None, None, "season average")


@dataclass
class Forecast:
    label: str
    in_top: bool
    finished: bool
    makes_it: bool
    headline: str
    chance: str
    ratio: float
    color: int
    gap: float
    buffer: float
    eta_h: float | None
    slip_h: float | None
    required_pace: float
    projected_you: float
    projected_line: float


def _chance_text(ratio: float, in_top: bool) -> str:
    if in_top:
        if ratio >= 1.3:
            return "🟢 Very safe"
        if ratio >= 1.0:
            return "🟢 Safe"
        if ratio >= 0.8:
            return "🟡 At risk"
        return "🔴 Likely to drop out"
    if ratio >= 1.5:
        return "🟢 Very likely"
    if ratio >= 1.0:
        return "🟢 Likely"
    if ratio >= 0.7:
        return "🟡 Possible but tight"
    if ratio >= 0.4:
        return "🟠 Unlikely"
    return "🔴 Very unlikely"


def forecast(*, points: float, pace: float, line_points: float, line_pace: float,
             remaining_h: float, rank: int | None, top_n: int, label: str) -> Forecast:
    in_top = rank is not None and rank <= top_n
    finished = remaining_h <= 0
    horizon = max(remaining_h, 0.0)
    proj_you, proj_line = points + pace * horizon, line_points + line_pace * horizon
    gap, buffer = line_points - points, points - line_points
    required = max(0.0, (proj_line - points) / horizon) if horizon > 0 else 0.0
    ratio = pace / required if required > 0 else 99.0
    eta_h = slip_h = None
    makes_it = proj_you >= proj_line if in_top else False
    lead = top_n == 1

    if finished:
        text = (f"🏁 Finished {'#1' if lead else 'in the ' + label}" if in_top
                else f"🏁 Finished outside the {label}")
        return Forecast(label, in_top, True, in_top, text, "Final result", ratio, GREEN if in_top else RED,
                        gap, buffer, None, None, 0.0, points, line_points)

    if in_top:
        if not makes_it:
            slip_h = buffer / (line_pace - pace) if line_pace > pace else horizon
        if makes_it:
            headline = "👑 You're #1 and on track to hold the lead" if lead else f"✅ You're in the {label} and on track to stay"
            color = GREEN
        else:
            headline = "⚠️ You're #1 but the next player is catching up" if lead else f"⚠️ You're in the {label} but slipping out"
            color = ORANGE
    else:
        closing = pace - line_pace
        if gap <= 0:
            eta_h = 0.0
        elif closing > 0:
            eta_h = gap / closing
        makes_it = eta_h is not None and eta_h <= horizon
        if makes_it:
            headline = f"🚀 You'll reach the {label} in about {fmt_duration(eta_h or 0)}"
            color = GREEN
        else:
            headline = f"❌ You won't reach the {label} at this pace"
            color = RED
    return Forecast(label, in_top, False, makes_it, headline, _chance_text(ratio, in_top), ratio, color,
                    gap, buffer, eta_h, slip_h, required, proj_you, proj_line)


def forecast_bullets(fc: Forecast, *, pace_you: float, remaining_h: float, line_name: str) -> list[str]:
    now = time.time()
    lines: list[str] = []
    if fc.finished:
        lines.append(f"• Final gap to the {line_name} line: **{fmt_int(abs(fc.buffer))}** pts {'ahead' if fc.in_top else 'behind'}")
        return lines
    lines.append(f"• Confidence: **{fc.chance}** (you're moving at **{min(fc.ratio, 9.9):.1f}×** the pace you need)")
    if fc.in_top:
        lines.append(f"• Cushion over the {line_name} line: **{fmt_int(fc.buffer)}** pts")
        if fc.makes_it:
            lines.append(f"• Projected at the end: **{fmt_compact(fc.projected_you)}** vs line **{fmt_compact(fc.projected_line)}**")
        else:
            lines.append(f"• The line catches you {when_rel(now + (fc.slip_h or 0) * 3600)} unless you speed up")
            lines.append(f"• To stay in you need **{fmt_compact(fc.required_pace)}**/hr (you: {fmt_compact(pace_you)}/hr)")
    else:
        lines.append(f"• Gap to the {line_name} line: **{fmt_int(fc.gap)}** pts")
        if fc.makes_it and fc.eta_h is not None:
            lines.append(f"• You pass it {when_rel(now + fc.eta_h * 3600)} ({when_full(now + fc.eta_h * 3600)})")
            lines.append(f"• Projected at the end: **{fmt_compact(fc.projected_you)}** vs line **{fmt_compact(fc.projected_line)}**")
        else:
            short = max(0.0, fc.projected_line - fc.projected_you)
            lines.append(f"• At this pace you finish **{fmt_compact(short)}** pts short")
            lines.append(f"• You need **{fmt_compact(fc.required_pace)}**/hr from now (that's **{fc.required_pace / max(pace_you, 1):.1f}×** your pace)")
    lines.append(f"• Time left: **{fmt_duration(remaining_h)}**")
    return lines


def pace_table(pace: Pace, *, you: str, line: str, points: float, line_points: float) -> str:
    rows = [["Points now", fmt_compact(points), fmt_compact(line_points)],
            ["Season avg /hr", fmt_compact(pace.avg_you), fmt_compact(pace.avg_line)]]
    if pace.trend_you is not None and pace.trend_line is not None:
        rows.append([f"Last {pace.trend_min}m /hr", fmt_compact(pace.trend_you), fmt_compact(pace.trend_line)])
    return table(rows, ["", you, line])


# =========================================================================== shared lookups

async def league_season_meta(season: str | None) -> dict[str, Any]:
    data = await http.api("/v1/leagues/seasons")
    wanted = (season or data.get("active") or "").lower()
    for meta in data.get("seasons") or []:
        if meta["id"].lower() == wanted:
            return meta
    raise ApiError("League season not found.", 404)


async def active_battle_id() -> str | None:
    data = await http.api("/v1/clans/players", {"page": 1, "pageSize": 1})
    return data.get("activeBattleConfigName")


async def battle_detail(battle_id: str | None) -> dict[str, Any]:
    battle_id = battle_id or await active_battle_id()
    if not battle_id:
        raise ApiError("There's no live clan battle right now.")
    return await http.api(f"/v1/clans/battles/{urllib.parse.quote(battle_id, safe='')}")


def time_window(meta: dict[str, Any], start_floor: float = 0) -> tuple[float, float, float]:
    now = time.time()
    start = max(meta.get("startTime") or now, start_floor)
    finish = meta.get("finishTime") or (now + 86400)
    return max((min(now, finish) - start) / 3600, 0.0), max(0.0, (finish - now) / 3600), finish


# =========================================================================== views

HOW_IT_WORKS = discord.Embed(
    title="ℹ️ How the forecast works", color=BLURPLE,
    description=(
        "**Pace** is points per hour. The bot compares **your** pace with the pace of whoever sits **on the line** "
        "(the last spot that counts, like #100 for leagues or #50 for clans).\n\n"
        "**Reading the verdict**\n"
        "🚀 / ✅ You make it or you're safe\n⚠️ You're in, but the line is catching up\n❌ You miss it at this pace\n\n"
        "**Confidence** shows how fast you're moving compared with the pace you'd need. "
        "`1.0×` means exactly enough, `1.5×` is comfortable, `0.5×` means you need to double your speed.\n\n"
        "**Season average vs recent trend:** the bot uses the average at first, then switches to the last-hour trend "
        "after it has watched for about 20 minutes. Trends react faster when someone speeds up.\n\n"
        "This assumes everyone keeps their current pace. Late pushes and drops change the result."))


class LiveView(discord.ui.View):
    def __init__(self, builder: Callable[[], Awaitable[list[discord.Embed]]],
                 nearby: Callable[[], Awaitable[discord.Embed]] | None = None, explain: bool = True) -> None:
        super().__init__(timeout=600)
        self.builder, self.nearby = builder, nearby
        self.last_refresh = 0.0
        if nearby is None:
            self.remove_item(self.nearby_button)
        if not explain:
            self.remove_item(self.info_button)

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.primary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if time.monotonic() - self.last_refresh < 20:
            await interaction.response.send_message("Easy there. Give it 20 seconds between refreshes.", ephemeral=True)
            return
        self.last_refresh = time.monotonic()
        await interaction.response.defer()
        http.clear_cache()
        await interaction.edit_original_response(embeds=await self.builder(), view=self)

    @discord.ui.button(label="Nearby", emoji="📍", style=discord.ButtonStyle.secondary)
    async def nearby_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        assert self.nearby is not None
        await interaction.followup.send(embed=await self.nearby(), ephemeral=True)

    @discord.ui.button(label="How it works", emoji="❔", style=discord.ButtonStyle.secondary)
    async def info_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=HOW_IT_WORKS, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error)


async def report_error(interaction: discord.Interaction, error: Exception) -> None:
    original = getattr(error, "original", error)
    if isinstance(original, ApiError):
        text = str(original)
    else:
        log.exception("Unhandled error", exc_info=original)
        text = "Something broke on my side. Try again in a moment."
    embed = discord.Embed(title="⚠️ Hold up", description=text, color=RED)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not deliver error message")


# =========================================================================== leaderboards

watch: dict[str, dict[str, set[int]]] = defaultdict(lambda: {"users": set(), "ranks": set()})


async def board_entry_at(board_id: str, source: str, rank: int) -> dict[str, Any] | None:
    data = await http.api(f"/v1/leaderboards/{urllib.parse.quote(board_id, safe='')}",
                          {"page": (rank - 1) // 100 + 1, "pageSize": 100, "source": source})
    entries = data.get("entries", [])
    idx = (rank - 1) % 100
    return entries[idx] if idx < len(entries) else None


def can_forecast(meta: dict[str, Any], codec: str | None, value: Any) -> bool:
    ends = meta.get("scoringEndsAt")
    return (meta.get("state") == "live" and bool(ends) and ends > time.time()
            and (codec in (None, "Integer")) and isinstance(value, (int, float)) and value < 2_000_000_000)


async def leaderboard_embeds(board_id: str, username: str | None) -> list[discord.Embed]:
    qid = urllib.parse.quote(board_id, safe="")
    first = await http.api(f"/v1/leaderboards/{qid}", {"page": 1, "pageSize": 10})
    meta, source = first["leaderboard"], first["source"]
    mid, codec, state = meta["id"], meta.get("codec"), meta.get("state", "inactive")
    color = {"live": GREEN, "ended": GOLD, "final": BLURPLE}.get(state, GREY)
    ends = meta.get("scoringEndsAt")

    overview = discord.Embed(title=f"{STATE_EMOJI.get(state, '')} {mid}", color=color)
    overview.description = (
        f"**Status:** {state.title()} • showing **{source}** standings\n"
        + (f"**Scoring {'ends' if ends and ends > time.time() else 'ended'}:** {when_full(ends)} ({when_rel(ends)})\n" if ends else "")
        + f"**Players tracked:** {fmt_int(first.get('total'))}"
        + (f" • **Updated:** {when_rel(first['updatedAt'])}" if first.get("updatedAt") else ""))
    overview.add_field(name="🏆 Top 10", value="\n".join(
        f"{rank_badge(r['Rank'])} **{trunc(r.get('DisplayName') or r.get('Name') or str(r.get('UserID')), 22)}** · {fmt_value(r['Value'], codec)}"
        for r in first.get("entries", [])[:10]) or "No rows yet.", inline=False)
    if meta.get("final"):
        overview.add_field(name="🎁 Rewards paid down to", value=f"Rank {fmt_int(meta['final'].get('rewardedThroughRank'))}", inline=True)
    footer(overview, "some boards use encoded scores")
    if not username:
        return [overview]

    user = await http.roblox_user(username)
    shots = await http.headshots([user["id"]])
    card = discord.Embed(color=color)
    card.set_author(name=f"{user['displayName']} (@{user['name']})", icon_url=shots.get(user["id"]))
    if user["id"] in shots:
        card.set_thumbnail(url=shots[user["id"]])

    try:
        row = (await http.api(f"/v1/leaderboards/{qid}/players/{user['id']}", {"source": source}))["entry"]
    except ApiError as exc:
        if exc.status != 404:
            raise
        card.title = f"❌ {user['displayName']} isn't on {mid}"
        card.color = RED
        total = first.get("total") or 0
        last = await board_entry_at(board_id, source, total) if total else None
        card.description = ("They aren't in the rows the game API keeps for this board. "
                            "That usually means they're below the cutoff or haven't scored.")
        if last:
            card.add_field(name="🚪 To get on the board", value=f"Beat the last tracked score: **{fmt_value(last['Value'], codec)}** (rank {fmt_int(last['Rank'])}).", inline=False)
        return [footer(card), overview]

    rank, total, value = row["Rank"], max(first.get("total") or 1, 1), row["Value"]
    around = await http.api(f"/v1/leaderboards/{qid}", {"page": (rank - 1) // 100 + 1, "pageSize": 100, "source": source})
    entries = around.get("entries", [])
    idx = next((i for i, e in enumerate(entries) if e["UserID"] == user["id"]), None)

    card.title = f"{rank_badge(rank)}  {user['displayName']} on {mid}"
    card.description = (f"### Rank {fmt_int(rank)} of {fmt_int(total)}\n"
                        f"`{progress_bar(1 - (rank - 1) / total)}` Top **{rank / total * 100:.2f}%**")
    standing = [["Rank", f"#{fmt_int(rank)}"], ["Score", fmt_value(value, codec).split(" (")[0]], ["Top", f"{rank / total * 100:.2f}%"]]
    if row.get("RewardTier"):
        standing.append(["Reward tier", f"Top {row['RewardTier']:,}"])
    card.add_field(name="📍 Standing", value=table(standing), inline=True)
    if idx is not None:
        gaps = []
        if idx > 0:
            gaps.append([f"To pass #{entries[idx - 1]['Rank']}", f"{abs(entries[idx - 1]['Value'] - value):,.0f}"])
        if idx + 1 < len(entries):
            gaps.append([f"Lead on #{entries[idx + 1]['Rank']}", f"{abs(value - entries[idx + 1]['Value']):,.0f}"])
        if gaps:
            card.add_field(name="↕️ Gaps", value=table(gaps), inline=True)
        lines = []
        for e in entries[max(0, idx - 3): idx + 4]:
            name = trunc(e.get("DisplayName") or e.get("Name") or str(e["UserID"]), 14)
            lines.append([str(e["Rank"]), name + (" ◀" if e["UserID"] == user["id"] else ""), f"{e['Value']:,.0f}"])
        card.add_field(name="👀 Around you", value=table(lines), inline=False)

    if can_forecast(meta, codec, value):
        remaining_h = (ends - time.time()) / 3600
        targets = [2] if rank == 1 else []
        if rank > 1:
            lower = [m for m in MILESTONES if m < rank]
            if lower:
                targets.append(lower[-1])
            if rank > 100 and 100 not in targets:
                targets.append(100)
        self_key = f"B:{mid}:u{user['id']}"
        watch[mid]["users"].add(user["id"])
        for target in targets[:2]:
            line_row = await board_entry_at(board_id, source, target)
            if not line_row:
                continue
            line_key = f"B:{mid}:r{target}"
            watch[mid]["ranks"].add(target)
            t_you, t_line = tracker.rate(self_key, value), tracker.rate(line_key, line_row["Value"])
            tracker.record(self_key, value)
            tracker.record(line_key, line_row["Value"])
            label = "lead (#1)" if target == 2 else f"Top {target}"
            line_name = "#2" if target == 2 else f"#{target}"
            title = f"🔮 Forecast: {'holding #1' if target == 2 else f'reaching Top {target}'}"
            if t_you and t_line:
                minutes = min(t_you[1], t_line[1])
                pace = Pace(t_you[0], t_line[0], t_you[0], t_line[0], t_you[0], t_line[0], minutes, f"recent trend (last {minutes} min)")
                fc = forecast(points=value, pace=pace.you, line_points=line_row["Value"], line_pace=pace.line,
                              remaining_h=remaining_h, rank=1 if target == 2 else rank,
                              top_n=1 if target == 2 else target, label=label)
                if target != 2 or rank == 1:
                    card.color = fc.color if target == targets[0] else card.color
                body = (f"**{fc.headline}**\n" + "\n".join(forecast_bullets(fc, pace_you=pace.you, remaining_h=remaining_h, line_name=line_name))
                        + "\n" + table([["Points now", fmt_compact(value), fmt_compact(line_row["Value"])],
                                        [f"Last {minutes}m /hr", fmt_compact(pace.you), fmt_compact(pace.line)]], ["", "You", line_name]))
            else:
                ready = tracker.ready_at(self_key) or (time.time() + TREND_MIN_SECONDS)
                body = ("📡 **Tracking started.** This board has no public start time, so I measure pace from my own samples.\n"
                        f"Check again {when_rel(ready)} for the full forecast.\n"
                        f"Right now: **{fmt_int(abs(line_row['Value'] - value))}** pts {'ahead of' if target == 2 else 'behind'} {line_name}.")
            card.add_field(name=title, value=trunc(body, 1024), inline=False)
    elif state != "live":
        card.add_field(name="🔮 Forecast", value="This board isn't live, so there's nothing left to forecast.", inline=False)
    else:
        card.add_field(name="🔮 Forecast", value="This board uses an encoded score (speedrun or race), so I can't forecast it reliably.", inline=False)
    return [footer(card, mid), overview]


class PlayerModal(discord.ui.Modal):
    def __init__(self, board_id: str) -> None:
        super().__init__(title=trunc(board_id, 45))
        self.board_id = board_id
        self.username = discord.ui.TextInput(label="Roblox username (blank = top 10 only)", required=False, max_length=40)
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        name = self.username.value.strip() or None
        embeds = await leaderboard_embeds(self.board_id, name)
        await interaction.followup.send(embeds=embeds, view=LiveView(lambda: leaderboard_embeds(self.board_id, name)))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction, error)


class BoardSelect(discord.ui.Select):
    def __init__(self, boards: list[dict[str, Any]]) -> None:
        options = []
        for b in boards[:25]:
            ends = b.get("scoringEndsAt")
            when = dt.datetime.fromtimestamp(ends, dt.timezone.utc).strftime("%b %d %Y") if ends else "no end date"
            options.append(discord.SelectOption(label=trunc(b["id"], 100), value=b["id"],
                                                description=f"{b['state']} • ends {when}", emoji=STATE_EMOJI.get(b["state"])))
        super().__init__(placeholder="Pick a leaderboard…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PlayerModal(self.values[0]))


# =========================================================================== leagues

async def league_embeds(name: str, season: str | None) -> list[discord.Embed]:
    meta = await league_season_meta(season)
    sid = meta["id"]
    detail = await http.api(f"/v1/leagues/{urllib.parse.quote(name, safe='')}", {"season": sid})
    board = (await http.api("/v1/leagues", {"season": sid, "page": 1, "pageSize": 100}))["leagues"]
    line = board[min(LEAGUE_TOP_N, len(board)) - 1] if board else None

    place, points = detail.get("Place"), detail.get("Points", 0)
    elapsed_h, remaining_h, finish = time_window(meta, detail.get("Created") or 0)
    line_elapsed_h, _, _ = time_window(meta)
    owner = detail.get("Owner") or {}
    shots = await http.headshots([owner["UserID"]]) if owner.get("UserID") else {}
    state = meta["state"]

    embed = discord.Embed(title=f"🏆 {detail['Name']}", color=BLURPLE)
    if asset_url(detail.get("Icon")):
        embed.set_thumbnail(url=asset_url(detail["Icon"]))
    if owner.get("UserID"):
        embed.set_author(name=f"Owned by {owner.get('DisplayName')}", icon_url=shots.get(owner["UserID"]))

    header = (f"**{meta['title']}** {STATE_EMOJI.get(state, '')} {state}"
              + (f" • ends {when_rel(finish)}" if state == "live" else ""))
    pace = fc = None
    if line and line.get("Points"):
        pace = compute_pace(f"L:{sid}:{detail['Name'].lower()}", points, elapsed_h,
                            f"L:{sid}:#cutoff", line["Points"], line_elapsed_h)
        fc = forecast(points=points, pace=pace.you, line_points=line["Points"], line_pace=pace.line,
                      remaining_h=remaining_h, rank=place, top_n=LEAGUE_TOP_N, label=f"Top {LEAGUE_TOP_N}")
        embed.color = fc.color
        pct = points / line["Points"]
        bar_text = (f"`{progress_bar(pct)}` **{min(pct, 9.99) * 100:.0f}%** of the Top {LEAGUE_TOP_N} line" if not fc.in_top
                    else f"`{progress_bar(1 - ((place or 1) - 1) / LEAGUE_TOP_N)}` position inside the Top {LEAGUE_TOP_N}")
        embed.description = f"{header}\n### {fc.headline}\n{bar_text}"
    else:
        embed.description = header

    standing = [["Place", f"#{place}" if place else "unranked"], ["Points", fmt_compact(points)],
                ["Members", f"{len(detail.get('Members') or []) + 1}/{detail.get('MemberCapacity', 4)}"]]
    embed.add_field(name="📍 Standing", value=table(standing), inline=True)
    if pace and line:
        embed.add_field(name=f"⚡ You vs the #{LEAGUE_TOP_N} line",
                        value=pace_table(pace, you="You", line=f"#{LEAGUE_TOP_N}", points=points, line_points=line["Points"])
                        + f"*Using: {pace.basis}*", inline=True)
    if fc:
        embed.add_field(name="🔮 What happens next",
                        value=trunc("\n".join(forecast_bullets(fc, pace_you=pace.you, remaining_h=remaining_h, line_name=f"#{LEAGUE_TOP_N}")), 1024),
                        inline=False)

    rewards = [f"**Right now:** {band_text(detail.get('Reward') or band_for(meta, place))}"]
    if place and place > LEAGUE_TOP_N and band_for(meta, LEAGUE_TOP_N):
        rewards.append(f"**At #{LEAGUE_TOP_N}:** {band_text(band_for(meta, LEAGUE_TOP_N))}")
    embed.add_field(name="🎁 Rewards", value=trunc("\n".join(rewards), 1024), inline=False)

    contributions = detail.get("PointContributions") or []
    counted_total = sum(c["Points"] for c in contributions if c.get("Counted")) or 1
    roster = []
    for c in contributions[:4]:
        share = c["Points"] / counted_total if c.get("Counted") else 0
        roster.append(f"`{progress_bar(share, 8)}` **{trunc(str(c['DisplayName']), 18)}** · {fmt_compact(c['Points'])} ({share * 100:.0f}%)"
                      + ("" if c.get("Counted") else " *(left league)*"))
    if roster:
        embed.add_field(name="👥 Who's carrying", value="\n".join(roster), inline=False)
    return [footer(embed, f"season {sid}")]


async def league_nearby(name: str, season: str | None) -> discord.Embed:
    meta = await league_season_meta(season)
    detail = await http.api(f"/v1/leagues/{urllib.parse.quote(name, safe='')}", {"season": meta["id"]})
    place = detail.get("Place") or 1
    rows = (await http.api("/v1/leagues", {"season": meta["id"], "page": (place - 1) // 100 + 1, "pageSize": 100}))["leagues"]
    idx = next((i for i, r in enumerate(rows) if r["ID"] == detail["ID"]), 0)
    lines = [[str(r.get("Rank") or "-"), trunc(r["Name"], 14) + (" ◀" if r["ID"] == detail["ID"] else ""),
              fmt_compact(r["Points"]), f"{r['Points'] - detail['Points']:+,.0f}"]
             for r in rows[max(0, idx - 5): idx + 6]]
    embed = discord.Embed(title=f"📍 Around {detail['Name']}", color=BLURPLE,
                          description=table(lines, ["Rank", "League", "Points", "vs you"]))
    return footer(embed)


# =========================================================================== clans

async def clan_embeds(name: str, battle_id: str | None) -> list[discord.Embed]:
    detail = await battle_detail(battle_id)
    meta, clans = detail["meta"], detail["topClans"]
    ranked = sorted(clans, key=lambda c: c["points"], reverse=True)
    for i, c in enumerate(ranked, start=1):
        c["_rank"] = i
    entry = next((c for c in ranked if c["name"].lower() == name.strip().lower()), None)
    line = ranked[min(CLAN_TOP_N, len(ranked)) - 1] if ranked else None
    elapsed_h, remaining_h, finish = time_window(meta)
    state = meta["state"]
    header = (f"**{meta['title']}** {STATE_EMOJI.get(state, '')} {state}" + (f" • ends {when_rel(finish)}" if state == "live" else ""))

    embed = discord.Embed(color=BLURPLE)
    if not entry:
        embed.title = f"🛡️ {name}"
        embed.color = RED
        embed.description = (f"{header}\n### ❌ Not in the top {len(ranked)} of this battle\n"
                             + (f"The Top {CLAN_TOP_N} line is **{fmt_int(line['points'])}** points. Score more than that to get in." if line else ""))
        return [footer(embed, meta["id"])]

    rank = entry["_rank"]
    if asset_url(entry.get("icon")):
        embed.set_thumbnail(url=asset_url(entry["icon"]))
    embed.title = f"🛡️ {entry['name']} {flag(entry.get('countryCode'))}"
    pace = fc = None
    if line and line["points"]:
        pace = compute_pace(f"C:{meta['id']}:{entry['name'].lower()}", entry["points"], elapsed_h,
                            f"C:{meta['id']}:#cutoff", line["points"], elapsed_h)
        fc = forecast(points=entry["points"], pace=pace.you, line_points=line["points"], line_pace=pace.line,
                      remaining_h=remaining_h, rank=rank, top_n=CLAN_TOP_N, label=f"Top {CLAN_TOP_N}")
        embed.color = fc.color
        pct = entry["points"] / line["points"]
        bar_text = (f"`{progress_bar(pct)}` **{min(pct, 9.99) * 100:.0f}%** of the Top {CLAN_TOP_N} line" if not fc.in_top
                    else f"`{progress_bar(1 - (rank - 1) / CLAN_TOP_N)}` position inside the Top {CLAN_TOP_N}")
        embed.description = f"{header}\n### {fc.headline}\n{bar_text}"
    else:
        embed.description = header

    standing = [["Rank", f"#{rank}"], ["Points", fmt_compact(entry["points"])],
                ["Members", f"{entry.get('members', '?')}/{entry.get('memberCapacity', '?')}"]]
    if entry.get("medal"):
        standing.append(["Medal", entry["medal"]])
    embed.add_field(name="📍 Standing", value=table(standing), inline=True)
    if pace and line:
        embed.add_field(name=f"⚡ You vs the #{CLAN_TOP_N} line",
                        value=pace_table(pace, you="You", line=f"#{CLAN_TOP_N}", points=entry["points"], line_points=line["points"])
                        + f"*Using: {pace.basis}*", inline=True)
    if fc:
        embed.add_field(name="🔮 What happens next",
                        value=trunc("\n".join(forecast_bullets(fc, pace_you=pace.you, remaining_h=remaining_h, line_name=f"#{CLAN_TOP_N}")), 1024),
                        inline=False)
    rewards = [f"**Right now:** {band_text(band_for(meta, rank))}"]
    if rank > CLAN_TOP_N and band_for(meta, CLAN_TOP_N):
        rewards.append(f"**At #{CLAN_TOP_N}:** {band_text(band_for(meta, CLAN_TOP_N))}")
    embed.add_field(name="🎁 Rewards", value=trunc("\n".join(rewards), 1024), inline=False)
    contributors = [p for p in detail.get("topPlayers", []) if p["clan"]["name"].lower() == entry["name"].lower()][:5]
    if contributors:
        embed.add_field(name="👥 Who's carrying", value="\n".join(
            f"`{progress_bar(p['points'] / entry['points'] if entry['points'] else 0, 8)}` **{trunc(p['displayName'], 18)}** · {fmt_compact(p['points'])}"
            for p in contributors), inline=False)
    return [footer(embed, meta["id"])]


async def clan_nearby(name: str, battle_id: str | None) -> discord.Embed:
    detail = await battle_detail(battle_id)
    ranked = sorted(detail["topClans"], key=lambda c: c["points"], reverse=True)
    idx = next((i for i, c in enumerate(ranked) if c["name"].lower() == name.lower()), 0)
    mine = ranked[idx]["points"]
    lines = [[str(i + 1), trunc(c["name"], 14) + (" ◀" if i == idx else ""), fmt_compact(c["points"]), f"{c['points'] - mine:+,.0f}"]
             for i, c in enumerate(ranked) if max(0, idx - 5) <= i <= idx + 5]
    embed = discord.Embed(title=f"📍 Around {ranked[idx]['name']}", color=BLURPLE,
                          description=table(lines, ["Rank", "Clan", "Points", "vs you"]))
    return footer(embed)


# =========================================================================== legacy RAP / exists

class ItemRow:
    __slots__ = ("category", "id", "pt", "sh", "tn", "value")

    def __init__(self, entry: dict[str, Any]) -> None:
        cfg = entry.get("configData") or {}
        self.category = str(entry.get("category") or "")
        self.id = str(cfg.get("id") or "")
        self.pt = int(cfg.get("pt") or 0)
        self.sh = bool(cfg.get("sh"))
        self.tn = cfg.get("tn")
        self.value = entry.get("value")

    @property
    def key(self) -> tuple:
        return (self.category, self.id, self.pt, self.sh, self.tn)

    @property
    def variant(self) -> str:
        base = {0: "Normal", 1: "Golden", 2: "Rainbow"}.get(self.pt, f"pt{self.pt}")
        if self.sh:
            return "Shiny" if self.pt == 0 else f"Shiny {base}"
        return base

    @property
    def title(self) -> str:
        return f"{self.id} (T{self.tn})" if self.tn else self.id


async def load_rows(path: str) -> list[ItemRow]:
    data = await http.api(path, ttl=300)
    if isinstance(data, dict):
        data = data.get("data") or list(data.values())
    return [ItemRow(e) for e in data if isinstance(e, dict) and (e.get("configData") or {}).get("id")]


async def rap_and_exists() -> tuple[list[ItemRow], dict[tuple, Any]]:
    rap, exists = await asyncio.gather(load_rows("/api/rap"), load_rows("/api/exists"))
    return rap, {r.key: r.value for r in exists}


def row_line(r: ItemRow, exists: dict[tuple, Any]) -> str:
    tail = f" · {fmt_compact(exists[r.key])} exist" if exists.get(r.key) is not None else ""
    return f"**{r.title}** *({r.variant})* · 💎 {fmt_compact(r.value)}{tail}"


# =========================================================================== bot

class Ps99Bot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await http.start()
        guild_id = os.environ.get("GUILD_ID")
        if guild_id:
            guild = discord.Object(int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        sampler.start()

    async def close(self) -> None:
        sampler.cancel()
        await http.close()
        await super().close()


bot = Ps99Bot()


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    await report_error(interaction, error)


@tasks.loop(minutes=SAMPLE_INTERVAL_MINUTES)
async def sampler() -> None:
    try:
        meta = await league_season_meta(None)
        if meta["state"] == "live":
            board = (await http.api("/v1/leagues", {"season": meta["id"], "page": 1, "pageSize": 100}, ttl=0))["leagues"]
            for row in board:
                tracker.record(f"L:{meta['id']}:{row['Name'].lower()}", row["Points"])
            if board:
                tracker.record(f"L:{meta['id']}:#cutoff", board[min(LEAGUE_TOP_N, len(board)) - 1]["Points"])
    except Exception:
        log.exception("league sampling failed")
    try:
        battle_id = await active_battle_id()
        if battle_id:
            detail = await http.api(f"/v1/clans/battles/{battle_id}", ttl=0)
            ranked = sorted(detail["topClans"], key=lambda c: c["points"], reverse=True)
            for c in ranked:
                tracker.record(f"C:{battle_id}:{c['name'].lower()}", c["points"])
            if ranked:
                tracker.record(f"C:{battle_id}:#cutoff", ranked[min(CLAN_TOP_N, len(ranked)) - 1]["points"])
    except Exception:
        log.exception("clan sampling failed")
    try:
        boards = {b["id"]: b for b in (await http.api("/v1/leaderboards", ttl=0))["leaderboards"]}
        for bid, targets in list(watch.items())[:10]:
            if boards.get(bid, {}).get("state") != "live":
                continue
            for page in sorted({(r - 1) // 100 + 1 for r in targets["ranks"]})[:6]:
                data = await http.api(f"/v1/leaderboards/{bid}", {"page": page, "pageSize": 100, "source": "live"}, ttl=0)
                for e in data.get("entries", []):
                    if e["Rank"] in targets["ranks"]:
                        tracker.record(f"B:{bid}:r{e['Rank']}", e["Value"])
            for uid in list(targets["users"])[:30]:
                try:
                    row = (await http.api(f"/v1/leaderboards/{bid}/players/{uid}", {"source": "live"}, ttl=0))["entry"]
                    tracker.record(f"B:{bid}:u{uid}", row["Value"])
                except ApiError:
                    continue
    except Exception:
        log.exception("leaderboard sampling failed")


@sampler.before_loop
async def before_sampler() -> None:
    await bot.wait_until_ready()


# ----- /help
@bot.tree.command(name="help", description="What every command does, in plain words")
async def help_cmd(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="📖 PS99 Stats Bot", color=BLURPLE,
                          description="Live Pet Simulator 99 stats with forecasts. Every card has 🔄 **Refresh** and ❔ **How it works** buttons.")
    embed.add_field(name="📊 Competitions", value=(
        "`/leaderboard` pick an event board, type a username, get their rank and forecast\n"
        "`/league` a league's place, pace and chance of reaching **Top 100**\n"
        "`/clan` a clan's place, pace and chance of reaching **Top 50**\n"
        "`/battle` the live clan battle • `/season` the league season"), inline=False)
    embed.add_field(name="👤 Players and updates", value=(
        "`/player` avatar, public profile, clan and league\n`/new` what the latest update added"), inline=False)
    embed.add_field(name="💎 Market", value=(
        "`/rap` price per variant • `/lowrap` cheapest Huges, Titanics, Gargantuans • `/toprap` most valuable"), inline=False)
    embed.add_field(name="🔮 Reading forecasts", value=(
        "🚀 ✅ you make it • ⚠️ you're in but slipping • ❌ you miss it at this pace.\n"
        "**Confidence** compares your pace with the pace you'd need. Tap ❔ on any card for the full explanation."), inline=False)
    await interaction.response.send_message(embed=footer(embed))


# ----- /leaderboard
@bot.tree.command(name="leaderboard", description="Event leaderboards: pick a board, then look up any player")
@app_commands.describe(board="Leaderboard id (autocomplete). Leave empty for a dropdown", user="Roblox username to look up")
async def leaderboard(interaction: discord.Interaction, board: str | None = None, user: str | None = None) -> None:
    await interaction.response.defer()
    if board:
        embeds = await leaderboard_embeds(board, user)
        await interaction.followup.send(embeds=embeds, view=LiveView(lambda: leaderboard_embeds(board, user)))
        return
    boards = (await http.api("/v1/leaderboards"))["leaderboards"]
    intro = discord.Embed(
        title="📊 PS99 Leaderboards", color=BLURPLE,
        description=("**1.** Pick a board from the dropdown\n**2.** Type a Roblox username in the popup\n"
                     "**3.** Get their rank, the gaps, and a forecast\n\n"
                     f"Showing the newest 25 of **{len(boards)}** boards. Use `/leaderboard board:` to search them all.\n\n"
                     "🟢 live • 🟡 ended • 🏁 final • ⚪ inactive"))
    view = discord.ui.View(timeout=300)
    view.add_item(BoardSelect(boards))
    await interaction.followup.send(embed=footer(intro), view=view)


@leaderboard.autocomplete("board")
async def board_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    boards = (await http.api("/v1/leaderboards"))["leaderboards"]
    needle = current.lower()
    return [app_commands.Choice(name=trunc(f"{STATE_EMOJI.get(b['state'], '')} {b['id']} ({b['state']})", 100), value=b["id"])
            for b in boards if needle in b["id"].lower()][:25]


# ----- /league
@bot.tree.command(name="league", description="League stats and your chance of reaching the top 100")
@app_commands.describe(name="League name", user="Roblox username (finds their league)", season="Season id (default: active)")
async def league(interaction: discord.Interaction, name: str | None = None, user: str | None = None, season: str | None = None) -> None:
    await interaction.response.defer()
    if not name and not user:
        raise ApiError("Give me a league `name` or a Roblox `user`.")
    if not name:
        roblox = await http.roblox_user(user or "")
        meta = await league_season_meta(season)
        try:
            found = await http.api(f"/v1/leagues/players/{roblox['id']}", {"season": meta["id"]})
        except ApiError as exc:
            if exc.status == 404:
                raise ApiError(f"Couldn't find **{roblox['displayName']}** in the top 500 league contributors. Try `/league name:`.") from exc
            raise
        name = found["League"]["Name"]
    embeds = await league_embeds(name, season)
    await interaction.followup.send(embeds=embeds, view=LiveView(lambda: league_embeds(name, season), lambda: league_nearby(name, season)))


@league.autocomplete("name")
async def league_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    params: dict[str, Any] = {"page": 1, "pageSize": 25}
    if len(current) >= 2:
        params["search"] = current
    rows = (await http.api("/v1/leagues", params))["leagues"]
    return [app_commands.Choice(name=trunc(f"#{r.get('Rank') or '?'} {r['Name']}", 100), value=r["Name"]) for r in rows][:25]


@league.autocomplete("season")
async def league_season_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    seasons = (await http.api("/v1/leagues/seasons"))["seasons"]
    return [app_commands.Choice(name=trunc(f"{s['title']} ({s['state']})", 100), value=s["id"])
            for s in seasons if s.get("run") and current.lower() in s["id"].lower()][:25]


# ----- /clan
@bot.tree.command(name="clan", description="Clan stats and your chance of reaching the top 50")
@app_commands.describe(name="Clan name", user="Roblox username (finds their clan)", battle="Battle id (default: live battle)")
async def clan(interaction: discord.Interaction, name: str | None = None, user: str | None = None, battle: str | None = None) -> None:
    await interaction.response.defer()
    if not name and not user:
        raise ApiError("Give me a clan `name` or a Roblox `user`.")
    if not name:
        roblox = await http.roblox_user(user or "")
        try:
            found = await http.api(f"/v1/clans/players/{roblox['id']}")
        except ApiError as exc:
            if exc.status == 404:
                raise ApiError(f"**{roblox['displayName']}** isn't in the top 25 sampled clans. Try `/clan name:`.") from exc
            raise
        name = found["player"]["Clan"]["Name"]
    embeds = await clan_embeds(name, battle)
    await interaction.followup.send(embeds=embeds, view=LiveView(lambda: clan_embeds(name, battle), lambda: clan_nearby(name, battle)))


@clan.autocomplete("name")
async def clan_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    battle_id = await active_battle_id()
    if not battle_id:
        return []
    detail = await http.api(f"/v1/clans/battles/{urllib.parse.quote(battle_id, safe='')}")
    ranked = sorted(detail["topClans"], key=lambda c: c["points"], reverse=True)
    return [app_commands.Choice(name=trunc(f"#{i} {c['name']}", 100), value=c["name"])
            for i, c in enumerate(ranked, start=1) if current.lower() in c["name"].lower()][:25]


# ----- /battle
@bot.tree.command(name="battle", description="The current (or latest) clan battle")
async def battle(interaction: discord.Interaction) -> None:
    await interaction.response.defer()

    async def build() -> list[discord.Embed]:
        detail = await battle_detail(None)
        meta, stats = detail["meta"], detail["stats"]
        ranked = sorted(detail["topClans"], key=lambda c: c["points"], reverse=True)
        embed = discord.Embed(title=f"⚔️ {meta['title']}", color=GOLD if meta["state"] == "live" else BLURPLE)
        head = meta.get("headlineReward")
        if head and asset_url(head.get("icon")):
            embed.set_thumbnail(url=asset_url(head["icon"]))
        embed.description = (f"{STATE_EMOJI.get(meta['state'], '')} **{meta['state'].title()}**"
                             + (f" • ends {when_full(meta['finishTime'])} ({when_rel(meta['finishTime'])})" if meta.get("finishTime") else "")
                             + f"\n**Clans:** {fmt_int(stats['participatingClans'])} • **Total points:** {fmt_compact(stats['totalClanPoints'])}"
                             + (f"\n🎁 **Headline reward:** {reward_text(head)}" if head else ""))
        embed.add_field(name="🏆 Top 10 clans", value="\n".join(
            f"{rank_badge(i)} {flag(c.get('countryCode'))} **{trunc(c['name'], 18)}** · {fmt_compact(c['points'])}"
            for i, c in enumerate(ranked[:10], start=1)) or "No data.", inline=False)
        cutoffs = [[f"#{n}", fmt_compact(ranked[n - 1]["points"])] for n in (10, 25, 50, 100) if len(ranked) >= n]
        if cutoffs:
            embed.add_field(name="📏 Cutoffs", value=table(cutoffs, ["Rank", "Points needed"]), inline=True)
        embed.add_field(name="⭐ Top players", value="\n".join(
            f"{rank_badge(p['rank'])} **{trunc(p['displayName'], 16)}** ({trunc(p['clan']['name'], 10)}) · {fmt_compact(p['points'])}"
            for p in detail.get("topPlayers", [])[:5]) or "No data.", inline=True)
        bands = meta.get("placementRewards") or []
        if bands:
            embed.add_field(name="🎁 Rewards", value=trunc("\n".join(band_text(b) for b in bands[:7]), 1024), inline=False)
        return [footer(embed, meta["id"])]

    await interaction.followup.send(embeds=await build(), view=LiveView(build, explain=False))


# ----- /season
@bot.tree.command(name="season", description="League season overview, rewards and top leagues")
@app_commands.describe(season="Season id (default: active)")
async def season_cmd(interaction: discord.Interaction, season: str | None = None) -> None:
    await interaction.response.defer()

    async def build() -> list[discord.Embed]:
        meta = await league_season_meta(season)
        detail = await http.api(f"/v1/leagues/seasons/{meta['id']}")
        meta, stats = detail["meta"], detail["stats"]
        embed = discord.Embed(title=f"🏅 {meta['title']}", color=GOLD if meta["state"] == "live" else BLURPLE)
        head = meta.get("headlineReward")
        if head and asset_url(head.get("icon")):
            embed.set_thumbnail(url=asset_url(head["icon"]))
        embed.description = (f"{STATE_EMOJI.get(meta['state'], '')} **{meta['state'].title()}**"
                             + (f" • ends {when_full(meta['finishTime'])} ({when_rel(meta['finishTime'])})" if meta.get("finishTime") and meta["state"] == "live" else "")
                             + f"\n**Leagues:** {fmt_int(stats['totalLeagues'])}"
                             + (f" • **Ranked:** {fmt_int(stats['rankedLeagues'])}" if stats.get("rankedLeagues") is not None else "")
                             + (f"\n🎁 **Headline reward:** {reward_text(head)}" if head else ""))
        embed.add_field(name="🏆 Top 10 leagues", value="\n".join(
            f"{rank_badge(l['rank'])} **{trunc(l['name'], 18)}** · {fmt_compact(l['points'])}"
            for l in detail["topLeagues"][:10]) or "No data.", inline=False)
        embed.add_field(name="⭐ Top players", value="\n".join(
            f"{rank_badge(p['rank'])} **{trunc(p['displayName'], 16)}** ({trunc(p['league']['name'], 10)}) · {fmt_compact(p['points'])}"
            for p in detail["topPlayers"][:5]) or "No data.", inline=False)
        bands = meta.get("placementRewards") or []
        if bands:
            embed.add_field(name="🎁 Placement rewards", value=trunc("\n".join(band_text(b) for b in bands[:8]), 1024), inline=False)
        return [footer(embed, meta["id"])]

    await interaction.followup.send(embeds=await build(), view=LiveView(build, explain=False))


# ----- /player
@bot.tree.command(name="player", description="Roblox avatar, public PS99 profile, clan and league standing")
@app_commands.describe(username="Roblox username")
async def player(interaction: discord.Interaction, username: str) -> None:
    await interaction.response.defer()
    user = await http.roblox_user(username)
    shots = await http.headshots([user["id"]])
    profile_url = f"https://www.roblox.com/users/{user['id']}/profile"
    embed = discord.Embed(title=f"👤 {user['displayName']}", url=profile_url, color=BLURPLE,
                          description=f"@{user['name']} • ID `{user['id']}`")
    if user["id"] in shots:
        embed.set_thumbnail(url=shots[user["id"]])
    extras: list[discord.Embed] = []
    try:
        data = await http.api(f"/v1/players/{urllib.parse.quote(user['name'])}", {"include": "profile,inventory"}, ttl=300)
        views = data.get("views", {})
        prof = (views.get("profile") or {}).get("data") or {}
        if prof:
            stats = prof.get("Statistics") or {}
            diamonds = ((prof.get("Currency") or {}).get("Diamonds") or {}).get("_am")
            embed.add_field(name="📈 Progress", value=table([
                ["Rank", f"{fmt_int(prof.get('Rank'))} ({fmt_int(prof.get('RankStars'))}★)"],
                ["Rebirths", fmt_int(prof.get("Rebirths"))], ["Eggs hatched", fmt_compact(prof.get("EggsHatched"))],
                ["Huges opened", fmt_int(stats.get("Huge Pets Opened"))]]), inline=True)
            playtime = stats.get("Playtime")
            embed.add_field(name="💎 Wealth", value=table([
                ["Diamonds", fmt_compact(diamonds)], ["Booth earned", fmt_compact(prof.get("BoothDiamondsEarned"))],
                ["Playtime", f"{playtime / 86400:.1f} days" if playtime else "?"]]), inline=True)
        else:
            embed.add_field(name="Profile", value="The profile view isn't public.", inline=False)
        equipped = (((views.get("inventory") or {}).get("data") or {}).get("equipped") or {}).get("pets", {}).get("list") or []
        if equipped:
            embed.add_field(name="🐾 Equipped pets", value=trunc(", ".join(
                (("Golden " if p.get("golden") else "Rainbow " if p.get("rainbow") else "") + ("Shiny " if p.get("shiny") else "") + p["displayName"])
                for p in equipped[:8]), 1024), inline=False)
            for p in equipped[:4]:
                image = asset_url(p.get("goldenIcon") if p.get("golden") and p.get("goldenIcon") else p.get("icon"))
                if image:
                    gallery = discord.Embed(url=profile_url, color=BLURPLE)
                    gallery.set_image(url=image)
                    extras.append(gallery)
    except ApiError as exc:
        if exc.status == 404 or "player_not_found" in str(exc):
            embed.add_field(name="Profile", value="No public PS99 profile (the player hasn't opted in).", inline=False)
        else:
            raise
    try:
        found = (await http.api(f"/v1/clans/players/{user['id']}"))["player"]
        embed.add_field(name="🛡️ Clan", value=(f"**{found['Clan']['Name']}** {flag(found['Clan'].get('CountryCode'))}\n"
                                              f"{fmt_compact(found['ActiveBattlePoints'])} pts this battle\n"
                                              f"{found['TotalBattles']} battles • {found['EarnedMedals']} medals"), inline=True)
    except ApiError:
        pass
    try:
        found = await http.api(f"/v1/leagues/players/{user['id']}")
        embed.add_field(name="🏆 League", value=f"**{found['League']['Name']}**\n{fmt_compact(found['Points'])} pts contributed", inline=True)
    except ApiError:
        pass
    await interaction.followup.send(embeds=[footer(embed)] + extras)


# ----- /new
@bot.tree.command(name="new", description="What the latest game update added or changed")
async def new_update(interaction: discord.Interaction) -> None:
    await interaction.response.defer()

    async def build() -> list[discord.Embed]:
        rel = await http.api("/v1/releases/latest", ttl=120)
        embed = discord.Embed(title=f"🆕 Update {rel['version']}", color=PINK)
        stamp = rel.get("publishedAt") or rel.get("importedAt")
        if stamp:
            when = int(dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
            embed.description = f"Published {when_full(when)} ({when_rel(when)})\n**{rel['added']}** added • **{rel['changed']}** changed"
        thumb = None
        for collection, bucket in (rel.get("collections") or {}).items():
            added, changed = bucket.get("added") or [], bucket.get("changed") or []
            if not added and not changed:
                continue
            names = ", ".join(i["displayName"] + (f" ({i['rarity']})" if i.get("rarity") else "") for i in added[:8])
            more = f" +{len(added) - 8} more" if len(added) > 8 else ""
            value = (f"**Added ({len(added)}):** {names}{more}\n" if added else "") + (f"**Changed:** {len(changed)}" if changed else "")
            embed.add_field(name=collection, value=trunc(value, 1024), inline=False)
            thumb = thumb or next((i.get("icon") for i in added if i.get("icon")), None)
        if thumb:
            embed.set_thumbnail(url=thumb)
        if not embed.fields:
            embed.add_field(name="No item changes", value="The game updated but no item data changed.", inline=False)
        return [footer(embed)]

    await interaction.followup.send(embeds=await build(), view=LiveView(build, explain=False))


# ----- RAP tools
VARIANT_CHOICES = [app_commands.Choice(name=n, value=n) for n in
                   ("Any", "Normal", "Golden", "Rainbow", "Shiny", "Shiny Golden", "Shiny Rainbow")]
TIER_CHOICES = [app_commands.Choice(name=n, value=n) for n in ("Huge", "Titanic", "Gargantuan")]


@bot.tree.command(name="rap", description="RAP and exists count for a pet or item, per variant")
@app_commands.describe(item="Item name, e.g. Huge Cat")
async def rap_cmd(interaction: discord.Interaction, item: str) -> None:
    await interaction.response.defer()
    rows, exists = await rap_and_exists()
    needle = item.strip().lower()
    exact = [r for r in rows if r.id.lower() == needle]
    matches = exact or [r for r in rows if needle in r.id.lower()]
    if not matches:
        raise ApiError(f"Nothing matched `{item}`.")
    names = sorted({r.id for r in matches})
    if len(names) > 1:
        embed = discord.Embed(title=f"🔎 Matches for “{item}”", color=PINK,
                              description="\n".join(f"• {n}" for n in names[:20]) + "\n\nRun `/rap` again with the exact name.")
        await interaction.followup.send(embed=footer(embed))
        return
    matches.sort(key=lambda r: (r.tn or 0, r.sh, r.pt))
    embed = discord.Embed(title=f"💎 {names[0]}", color=PINK)
    embed.add_field(name="Prices by variant", value=table(
        [[f"{r.variant}{f' T{r.tn}' if r.tn else ''}", fmt_compact(r.value), fmt_compact(exists.get(r.key)) if exists.get(r.key) is not None else "?"]
         for r in matches[:20]], ["Variant", "RAP", "Exist"]), inline=False)
    await interaction.followup.send(embed=footer(embed, "RAP = recent average price in diamonds"))


@rap_cmd.autocomplete("item")
async def rap_item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    rows = await load_rows("/api/rap")
    names = sorted({r.id for r in rows if current.lower() in r.id.lower()})
    return [app_commands.Choice(name=trunc(n, 100), value=trunc(n, 100)) for n in names[:25]]


@bot.tree.command(name="lowrap", description="Cheapest Huges, Titanics or Gargantuans by RAP")
@app_commands.describe(tier="Pet tier", variant="Variant filter", limit="How many to show")
@app_commands.choices(tier=TIER_CHOICES, variant=VARIANT_CHOICES)
async def lowrap(interaction: discord.Interaction, tier: app_commands.Choice[str] | None = None,
                 variant: app_commands.Choice[str] | None = None, limit: app_commands.Range[int, 5, 25] = 15) -> None:
    await interaction.response.defer()
    rows, exists = await rap_and_exists()
    tiers = [tier.value] if tier else ["Huge", "Titanic", "Gargantuan"]
    wanted = variant.value if variant else "Any"
    pool = [r for r in rows
            if (not r.category or r.category.lower() == "pet") and any(r.id.startswith(f"{t} ") for t in tiers)
            and isinstance(r.value, (int, float)) and r.value > 0 and (wanted == "Any" or r.variant == wanted)]
    if not pool:
        raise ApiError("No priced pets matched those filters.")
    pool.sort(key=lambda r: r.value)
    embed = discord.Embed(title=f"🏷️ Cheapest {', '.join(tiers)}" + (f" ({wanted})" if wanted != "Any" else ""), color=GREEN)
    embed.description = "\n".join(f"`{i:>2}.` {row_line(r, exists)}" for i, r in enumerate(pool[:limit], start=1))
    await interaction.followup.send(embed=footer(embed, "items with 0 RAP are hidden"))


@bot.tree.command(name="toprap", description="Most valuable items by RAP")
@app_commands.describe(category="Category, e.g. Pet", variant="Variant filter", limit="How many to show")
@app_commands.choices(variant=VARIANT_CHOICES)
async def toprap(interaction: discord.Interaction, category: str | None = None,
                 variant: app_commands.Choice[str] | None = None, limit: app_commands.Range[int, 5, 25] = 15) -> None:
    await interaction.response.defer()
    rows, exists = await rap_and_exists()
    wanted = variant.value if variant else "Any"
    pool = [r for r in rows if isinstance(r.value, (int, float)) and r.value > 0
            and (not category or r.category.lower() == category.lower()) and (wanted == "Any" or r.variant == wanted)]
    if not pool:
        raise ApiError("No priced items matched those filters.")
    pool.sort(key=lambda r: r.value, reverse=True)
    embed = discord.Embed(title=f"👑 Highest RAP{f': {category}' if category else ''}", color=GOLD)
    embed.description = "\n".join(f"`{i:>2}.` {row_line(r, exists)}" for i, r in enumerate(pool[:limit], start=1))
    await interaction.followup.send(embed=footer(embed))


@toprap.autocomplete("category")
async def toprap_category_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    rows = await load_rows("/api/rap")
    cats = sorted({r.category for r in rows if r.category and current.lower() in r.category.lower()})
    return [app_commands.Choice(name=c, value=c) for c in cats[:25]]


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN first (in a .env file next to bot.py).")
    bot.run(token, log_handler=None)
