#!/usr/bin/env python3
"""Generate a Steam showcase SVG using the Steam Web API.

The script resolves a vanity URL (or accepts an existing SteamID64), pulls the
player summary, level, and recently played games, then renders a compact SVG
suited for README embeds. When API access is not available the script can fall
back to cached JSON data provided via ``--cache``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import textwrap
import base64
import mimetypes
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - urllib is part of stdlib
    from urllib.request import urlopen
except ImportError:  # pragma: no cover
    urlopen = None  # type: ignore

try:
    import requests
except ImportError:  # pragma: no cover - requests is part of the environment
    requests = None  # type: ignore

from xml.sax.saxutils import escape

API_BASE = "https://api.steampowered.com"

_DEFAULT_AVATAR_SVG = """
<svg xmlns='http://www.w3.org/2000/svg' width='88' height='88' viewBox='0 0 88 88'>
  <defs>
    <linearGradient id='avatarGradient' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0%' stop-color='#1B2838'/>
      <stop offset='100%' stop-color='#3C9BD6'/>
    </linearGradient>
  </defs>
  <rect width='88' height='88' rx='18' fill='url(#avatarGradient)'/>
  <g fill='none' stroke='rgba(255,255,255,0.4)' stroke-width='2'>
    <circle cx='44' cy='36' r='16'/>
    <path d='M18 76c6-12 15-20 26-20s20 8 26 20' stroke-linecap='round'/>
  </g>
</svg>
""".strip()

DEFAULT_AVATAR_DATA_URI = "data:image/svg+xml;base64," + base64.b64encode(
    _DEFAULT_AVATAR_SVG.encode("utf-8")
).decode("ascii")


@dataclass
class BadgeHighlight:
    name: str
    level: Optional[int] = None


@dataclass
class RecentGame:
    name: str
    playtime_2weeks: int


@dataclass
class SteamProfile:
    steamid: str
    personaname: str
    profileurl: str
    avatarfull: str
    avatar_data_uri: Optional[str] = None
    realname: Optional[str] = None
    loccountrycode: Optional[str] = None
    timecreated: Optional[int] = None
    lastlogoff: Optional[int] = None
    personastate: int = 0
    personastateflags: Optional[int] = None
    level: Optional[int] = None
    gameextrainfo: Optional[str] = None
    currentlyplaying_gameid: Optional[str] = None
    fetched_at: Optional[int] = None
    badge_highlights: List[BadgeHighlight] = field(default_factory=list)
    recent_games: List[RecentGame] = field(default_factory=list)

    @property
    def persona_state_label(self) -> str:
        states = {
            0: "Offline",
            1: "Online",
            2: "Busy",
            3: "Away",
            4: "Snooze",
            5: "Looking to Trade",
            6: "Looking to Play",
        }
        return states.get(self.personastate, "Unknown")

    @property
    def country_flag(self) -> Optional[str]:
        if not self.loccountrycode:
            return None
        code = self.loccountrycode.upper()
        if len(code) != 2:
            return None
        base = 0x1F1E6
        try:
            return "".join(chr(base + ord(ch) - ord("A")) for ch in code)
        except ValueError:
            return None

    @property
    def member_since(self) -> Optional[str]:
        if not self.timecreated:
            return None
        try:
            date = dt.datetime.fromtimestamp(self.timecreated, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return date.strftime("%b %Y")

    @property
    def last_seen(self) -> Optional[str]:
        if not self.lastlogoff:
            return None
        try:
            delta = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromtimestamp(
                self.lastlogoff, tz=dt.timezone.utc
            )
        except (OverflowError, OSError, ValueError):
            return None
        if delta.days >= 1:
            return f"{delta.days}d ago"
        hours = delta.seconds // 3600
        if hours:
            return f"{hours}h ago"
        minutes = max(1, delta.seconds // 60)
        return f"{minutes}m ago"

    @property
    def status_color(self) -> str:
        colors = {
            0: "#8BA3BC",
            1: "#6AE6FF",
            2: "#FF7A8A",
            3: "#FFB86B",
            4: "#F5D76E",
            5: "#B38CFF",
            6: "#4DFFB5",
        }
        return colors.get(self.personastate, "#66C0F4")

    @property
    def activity_line(self) -> Optional[str]:
        if self.gameextrainfo:
            return f"Playing {self.gameextrainfo}"
        if self.personastate == 0 and self.last_seen:
            return f"Last online {self.last_seen}"
        if self.personastate != 0:
            return self.persona_state_label
        return None


def fetch_json(session: requests.Session, path: str, *, params: Dict[str, Any]) -> Dict[str, Any]:
    response = session.get(f"{API_BASE}{path}", params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def resolve_vanity(session: requests.Session, api_key: str, vanity: str) -> str:
    data = fetch_json(
        session,
        "/ISteamUser/ResolveVanityURL/v1/",
        params={"key": api_key, "vanityurl": vanity},
    )
    response = data.get("response", {})
    if response.get("success") != 1:
        raise RuntimeError(f"Failed to resolve vanity URL '{vanity}': {response}")
    steamid = response.get("steamid")
    if not steamid:
        raise RuntimeError(f"No steamid returned for vanity '{vanity}'")
    return str(steamid)


def _normalize_content_type(header: Optional[str], url: str) -> str:
    if header:
        ctype = header.split(";", 1)[0].strip()
        if ctype:
            return ctype
    guess, _ = mimetypes.guess_type(url)
    return guess or "image/jpeg"


def fetch_avatar_data(session: Optional[requests.Session], url: str) -> Optional[str]:
    if not url:
        return None
    try:
        if session is not None:
            response = session.get(url, timeout=15)
            response.raise_for_status()
            data = response.content
            content_type = response.headers.get("Content-Type")
        elif urlopen is not None:  # pragma: no cover - fallback branch
            with urlopen(url, timeout=15) as fh:  # type: ignore[arg-type]
                data = fh.read()
                content_type = getattr(fh, "headers", {}).get("Content-Type") if hasattr(fh, "headers") else None
        else:  # pragma: no cover - only triggered when urllib missing
            return None
    except Exception:  # pragma: no cover - network dependent
        return None
    if not data:
        return None
    ctype = _normalize_content_type(content_type, url)
    return f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"


def fetch_profile(session: requests.Session, api_key: str, *, steamid: str) -> SteamProfile:
    summary = fetch_json(
        session,
        "/ISteamUser/GetPlayerSummaries/v2/",
        params={"key": api_key, "steamids": steamid},
    )
    players = summary.get("response", {}).get("players", [])
    if not players:
        raise RuntimeError(f"No player data returned for steamid {steamid}")
    player = players[0]

    level_data = fetch_json(
        session,
        "/IPlayerService/GetSteamLevel/v1/",
        params={"key": api_key, "steamid": steamid},
    )
    level = level_data.get("response", {}).get("player_level")

    badge_data = fetch_json(
        session,
        "/IPlayerService/GetBadges/v1/",
        params={"key": api_key, "steamid": steamid},
    )
    badges = badge_data.get("response", {}).get("badges", []) or []
    badge_highlights: List[BadgeHighlight] = []
    for badge in badges[:3]:
        name = badge.get("name") or badge.get("description") or "Badge"
        badge_highlights.append(BadgeHighlight(name=name, level=badge.get("level")))

    recent_data = fetch_json(
        session,
        "/IPlayerService/GetRecentlyPlayedGames/v1/",
        params={"key": api_key, "steamid": steamid, "count": 3},
    )
    recent_games_raw = recent_data.get("response", {}).get("games", []) or []
    recent_games = [
        RecentGame(name=game.get("name", "Unknown"), playtime_2weeks=game.get("playtime_2weeks", 0))
        for game in recent_games_raw
    ]

    avatar_data_uri = fetch_avatar_data(session, player.get("avatarfull", ""))

    fetched_at = int(dt.datetime.now(dt.timezone.utc).timestamp())

    return SteamProfile(
        steamid=str(player.get("steamid")),
        personaname=player.get("personaname", "Unknown"),
        profileurl=player.get("profileurl", f"https://steamcommunity.com/profiles/{steamid}"),
        avatarfull=player.get("avatarfull", ""),
        avatar_data_uri=avatar_data_uri,
        realname=player.get("realname"),
        loccountrycode=player.get("loccountrycode"),
        timecreated=player.get("timecreated"),
        lastlogoff=player.get("lastlogoff"),
        personastate=int(player.get("personastate", 0) or 0),
        personastateflags=player.get("personastateflags"),
        level=level,
        gameextrainfo=player.get("gameextrainfo"),
        currentlyplaying_gameid=player.get("gameid"),
        fetched_at=fetched_at,
        badge_highlights=badge_highlights,
        recent_games=recent_games,
    )


def load_cached_profile(path: str) -> SteamProfile:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    badge_highlights = [
        BadgeHighlight(name=item.get("name", "Badge"), level=item.get("level"))
        for item in raw.get("badge_highlights", [])
    ]
    recent_games = [
        RecentGame(name=item.get("name", "Unknown"), playtime_2weeks=item.get("playtime_2weeks", 0))
        for item in raw.get("recent_games", [])
    ]
    return SteamProfile(
        steamid=str(raw.get("steamid", "")),
        personaname=raw.get("personaname", "Unknown"),
        profileurl=raw.get("profileurl", "https://steamcommunity.com"),
        avatarfull=raw.get("avatarfull", ""),
        avatar_data_uri=raw.get("avatar_data_uri"),
        realname=raw.get("realname"),
        loccountrycode=raw.get("loccountrycode"),
        timecreated=raw.get("timecreated"),
        lastlogoff=raw.get("lastlogoff"),
        personastate=int(raw.get("personastate", 0) or 0),
        personastateflags=raw.get("personastateflags"),
        level=raw.get("level"),
        gameextrainfo=raw.get("gameextrainfo"),
        currentlyplaying_gameid=raw.get("currentlyplaying_gameid"),
        fetched_at=raw.get("fetched_at"),
        badge_highlights=badge_highlights,
        recent_games=recent_games,
    )


def human_minutes(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def render_svg(profile: SteamProfile) -> str:
    recent = profile.recent_games[:3]
    if not recent:
        recent = [RecentGame(name="No recent games", playtime_2weeks=0)]
    badges = profile.badge_highlights[:3]
    if not badges:
        badges = [BadgeHighlight(name="Collector", level=None)]

    def badge_label(badge: BadgeHighlight) -> str:
        label = badge.name
        if badge.level:
            label += f" · Lv{badge.level}"
        return label

    info_lines: List[str] = []
    if profile.realname:
        info_lines.append(profile.realname)
    flag = profile.country_flag
    if flag:
        info_lines.append(flag)
    if profile.member_since:
        info_lines.append(f"Member since {profile.member_since}")
    info_line = "  ·  ".join(info_lines)

    meta_line_parts: List[str] = []
    if profile.level is not None:
        meta_line_parts.append(f"Level {profile.level}")
    if profile.personastate == 0 and profile.last_seen:
        meta_line_parts.append(f"Last online {profile.last_seen}")
    else:
        meta_line_parts.append(profile.persona_state_label)
    meta_line = "  •  ".join(meta_line_parts)

    activity_line = profile.activity_line or ""

    avatar = escape(profile.avatar_data_uri or DEFAULT_AVATAR_DATA_URI)
    status_color = profile.status_color

    fetched_at = profile.fetched_at
    if fetched_at:
        try:
            generated_at = dt.datetime.fromtimestamp(fetched_at, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):  # pragma: no cover - cache corruption
            generated_at = dt.datetime.now(dt.timezone.utc)
    else:
        generated_at = dt.datetime.now(dt.timezone.utc)
    updated_label = generated_at.strftime("%d %b %Y · %H:%M UTC")

    suffix = (profile.steamid or "profile")[-6:]

    badge_lines = "".join(
        f"<tspan x='22' dy='{0 if idx == 0 else 18}'>• {escape(badge_label(badge))}</tspan>"
        for idx, badge in enumerate(badges)
    )

    max_minutes = max((game.playtime_2weeks for game in recent), default=0)
    max_minutes = max_minutes or 1
    bar_width = 560.0
    progress_gradients: List[str] = []
    progress_palette = ["#47D8E0", "#5B8CFF", "#FF9FD1"]
    progress_rows: List[str] = []
    for idx, game in enumerate(recent):
        width = max(10.0, bar_width * (game.playtime_2weeks / max_minutes)) if max_minutes else 10.0
        gradient_id = f"progressGradient{idx}_{suffix}"
        color = progress_palette[idx % len(progress_palette)]
        progress_gradients.append(
            textwrap.dedent(
                f"""
                <linearGradient id="{gradient_id}" x1="0" y1="0" x2="1" y2="0">
                  <stop offset="0%" stop-color="{color}" stop-opacity="0.35" />
                  <stop offset="100%" stop-color="{color}" stop-opacity="0.95" />
                </linearGradient>
                """
            ).strip()
        )
        progress_rows.append(
            textwrap.dedent(
                f"""
                <g transform="translate(0 {idx * 36})">
                  <text x="0" y="0" font-size="13" font-weight="600" fill="#E6F2FF">{escape(game.name)}</text>
                  <text x="{bar_width}" y="0" font-size="12" fill="#9EBFD8" text-anchor="end">{human_minutes(game.playtime_2weeks)}</text>
                  <rect x="0" y="12" width="{bar_width}" height="10" rx="5" fill="rgba(20, 38, 56, 0.85)" />
                  <rect x="0" y="12" width="{width:.1f}" height="10" rx="5" fill="url(#{gradient_id})" />
                </g>
                """
            ).strip()
        )

    progress_defs = "\n".join(progress_gradients)
    progress_section = "\n".join(progress_rows)

    return textwrap.dedent(
        f"""
        <svg width="960" height="360" viewBox="0 0 960 360" fill="none" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="steamCardGradient_{suffix}" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stop-color="#0A111B" />
              <stop offset="42%" stop-color="#132B46" />
              <stop offset="100%" stop-color="#203D5F" />
            </linearGradient>
            <radialGradient id="avatarGlow_{suffix}" cx="0.5" cy="0.5" r="0.6">
              <stop offset="0%" stop-color="#5CCBF7" stop-opacity="0.75" />
              <stop offset="100%" stop-color="#112030" stop-opacity="0" />
            </radialGradient>
            <filter id="steamCardShadow_{suffix}" x="-12%" y="-12%" width="124%" height="124%">
              <feDropShadow dx="0" dy="16" stdDeviation="22" flood-color="#040A14" flood-opacity="0.55" />
            </filter>
            <clipPath id="avatarClip_{suffix}">
              <rect x="48" y="60" width="160" height="160" rx="32" />
            </clipPath>
            {progress_defs}
          </defs>
          <g filter="url(#steamCardShadow_{suffix})">
            <rect x="0" y="0" width="960" height="360" rx="36" fill="url(#steamCardGradient_{suffix})" stroke="rgba(120, 194, 255, 0.28)" />
            <ellipse cx="128" cy="272" rx="120" ry="94" fill="url(#avatarGlow_{suffix})" />
          </g>
          <g font-family="'Segoe UI', 'Inter', 'Helvetica Neue', sans-serif">
            <image href="{avatar}" x="48" y="60" width="160" height="160" clip-path="url(#avatarClip_{suffix})" preserveAspectRatio="xMidYMid slice" />
            <rect x="48" y="60" width="160" height="160" rx="32" fill="rgba(11, 23, 35, 0.38)" stroke="rgba(115, 191, 255, 0.45)" />
            <circle cx="196" cy="212" r="14" fill="#0A141E" stroke="rgba(255,255,255,0.15)" />
            <circle cx="196" cy="212" r="9" fill="{status_color}" />
            <g transform="translate(248 96)">
              <text x="0" y="0" font-size="30" font-weight="700" fill="#F5FAFF">{escape(profile.personaname)}</text>
              <g transform="translate(0 22)">
                <rect x="0" y="18" width="140" height="34" rx="17" fill="rgba(16, 34, 52, 0.75)" stroke="rgba(108, 186, 255, 0.55)" />
                <text x="70" y="40" font-size="14" font-weight="600" fill="#D5ECFF" text-anchor="middle">Steam Level {escape(str(profile.level) if profile.level is not None else '??')}</text>
              </g>
              <text x="0" y="92" font-size="14" fill="#A7C3DB">{escape(info_line)}</text>
              <text x="0" y="118" font-size="14" fill="#7FB7DA">{escape(meta_line)}</text>
              <text x="0" y="144" font-size="14" fill="#66C0F4">{escape(activity_line)}</text>
            </g>
            <g transform="translate(48 248)">
              <rect x="0" y="0" width="188" height="124" rx="24" fill="rgba(16, 31, 46, 0.62)" stroke="rgba(120, 194, 255, 0.28)" />
              <text x="22" y="34" font-size="14" font-weight="600" fill="#7FCBFF">Featured badges</text>
              <text x="22" y="60" font-size="13" fill="#C7E7FF">{badge_lines}</text>
            </g>
            <g transform="translate(248 176)">
              <rect width="660" height="200" rx="30" fill="rgba(12, 26, 40, 0.78)" stroke="rgba(120, 194, 255, 0.25)" />
              <text x="32" y="40" font-size="16" font-weight="600" fill="#7FCBFF">Recent playtime</text>
              <g transform="translate(32 84)">
                {progress_section}
              </g>
            </g>
            <text x="248" y="332" font-size="13" fill="rgba(199, 231, 255, 0.65)">Updated {escape(updated_label)}</text>
            <a href="{escape(profile.profileurl)}" target="_blank" rel="noreferrer">
              <rect x="792" y="48" width="120" height="40" rx="16" fill="rgba(18, 42, 60, 0.75)" stroke="rgba(120, 194, 255, 0.4)" />
              <text x="852" y="74" font-size="14" font-weight="600" fill="#F5FAFF" text-anchor="middle">View Profile</text>
            </a>
          </g>
        </svg>
        """
    ).strip()


def save_profile_cache(profile: SteamProfile, path: str) -> None:
    data = {
        "steamid": profile.steamid,
        "personaname": profile.personaname,
        "profileurl": profile.profileurl,
        "avatarfull": profile.avatarfull,
        "avatar_data_uri": profile.avatar_data_uri,
        "realname": profile.realname,
        "loccountrycode": profile.loccountrycode,
        "timecreated": profile.timecreated,
        "lastlogoff": profile.lastlogoff,
        "personastate": profile.personastate,
        "personastateflags": profile.personastateflags,
        "level": profile.level,
        "gameextrainfo": profile.gameextrainfo,
        "currentlyplaying_gameid": profile.currentlyplaying_gameid,
        "fetched_at": profile.fetched_at,
        "badge_highlights": [
            {"name": badge.name, "level": badge.level}
            for badge in profile.badge_highlights
        ],
        "recent_games": [
            {"name": game.name, "playtime_2weeks": game.playtime_2weeks}
            for game in profile.recent_games
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a Steam showcase SVG from the Steam Web API.")
    parser.add_argument("--vanity", help="Steam vanity URL handle")
    parser.add_argument("--steamid", help="SteamID64 (skips vanity resolution)")
    parser.add_argument("--api-key", dest="api_key", help="Steam Web API key (falls back to STEAM_API_KEY env var)")
    parser.add_argument("--output", default="img/steam-profile-showcase.svg", help="Path to write the SVG output")
    parser.add_argument("--cache", help="Optional cache JSON to read when API is unavailable")
    parser.add_argument("--write-cache", help="Optional path to write fetched data for offline reuse")

    args = parser.parse_args(argv)

    api_key = args.api_key or os.environ.get("STEAM_API_KEY")

    session = requests.Session() if requests else None
    profile: Optional[SteamProfile] = None

    if api_key and session:
        try:
            steamid = args.steamid or (
                resolve_vanity(session, api_key, args.vanity) if args.vanity else None
            )
            if not steamid:
                raise RuntimeError("A vanity handle or steamid must be provided when using the API")
            profile = fetch_profile(session, api_key, steamid=steamid)
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"Warning: API fetch failed ({exc}).", file=sys.stderr)
            profile = None

    if profile is None:
        if not args.cache:
            parser.error("API fetch failed and no cache provided")
        profile = load_cached_profile(args.cache)

    if args.write_cache:
        save_profile_cache(profile, args.write_cache)

    svg = render_svg(profile)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(svg + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
