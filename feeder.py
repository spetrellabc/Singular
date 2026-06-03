#!/usr/bin/env python3
"""
Slate feeder for the BBOC Singular graphics — multi-league, efficient.

Per cycle, per league:
  1 slate call      (events by date)               -> which games exist
  1 bulk markets    (/{sport}/{league}/markets)    -> ALL odds in one call
  N percents calls  ONLY for games inside the league's window
  teams cached for the life of the process

  -> core_to_node mappers -> node shape -> merge all leagues -> PUT to Singular

Place NEXT TO core_to_node.py.

Run:
  MODE=inspect python3 feeder.py
  LEAGUE="MLB,NFL" python3 feeder.py
"""

import json
import os
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from core_to_node import (
    BASE_URL,
    SPORTSBOOK_IDS,
    fetch,
    map_event_metadata,
    map_main_lines,
    map_percents,
    map_team_metadata,
    unwrap_event,
    resolve_sport,
    get_api_key,
)

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone.utc

# config (env)

LEAGUES       = [l.strip().upper() for l in os.environ.get("LEAGUE", "MLB").split(",") if l.strip()]
GAME_NODE     = os.environ.get("SINGULAR_GAME_NODE", "3846")
TICKER_NODE   = os.environ.get("SINGULAR_TICKER_NODE", "4084")
GAME_FIELD    = os.environ.get("SINGULAR_GAME_FIELD", "games")
TICKER_FIELD  = os.environ.get("SINGULAR_TICKER_FIELD", "ticker")
POLL_SECONDS  = int(os.environ.get("POLL_SECONDS", "300"))   # odds at 5 min is fine
MODE          = os.environ.get("MODE", "live")
PREFERRED_BOOK = os.environ.get("PREFERRED_BOOK")
PREFERRED_BOOK = int(PREFERRED_BOOK) if PREFERRED_BOOK else None

# Hydration window (hours from now) — how far ahead to keep games and pull
# percents. One global knob: set WINDOW_HOURS in Railway (default 24).
# Optional per-league override if you ever want it: WINDOW_NFL=72, etc.
GLOBAL_WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "24"))

def window_hours(league: str) -> int:
    env = os.environ.get(f"WINDOW_{league}")
    return int(env) if env else GLOBAL_WINDOW_HOURS

# Statuses that are over — never re-hydrate, never keep.
CLOSED_STATUSES = {"closed", "final", "complete", "completed"}

# Fetch percents for these splits-bearing graphics? If you only show splits on
# featured games, set NEED_PERCENTS=0 and skip them entirely.
NEED_PERCENTS = os.environ.get("NEED_PERCENTS", "1") != "0"


# helpers

def to_unix(iso):
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def to_node_team(team, fallback_name=None):
    team = team or {}
    return {
        "id": team.get("id"),
        "full_name": team.get("name") or team.get("full_name") or fallback_name,
        "logo": team.get("logo"),
        "primary_color": team.get("primary_color"),
        "secondary_color": team.get("secondary_color"),
        "abbr": team.get("abbr"),
    }


def assemble_node_game(meta, lines, pcts, home_team, away_team, league_name):
    return {
        "id": meta.get("event_id"),
        "league_name": league_name,
        "status": meta.get("status"),
        "start_time": meta.get("start_time"),
        "away_team_id": meta.get("away_team_id"),
        "home_team_id": meta.get("home_team_id"),
        "winning_team_id": meta.get("winning_team_id"),
        "start_time_unix": to_unix(meta.get("start_time")),
        "home_team": to_node_team(home_team, meta.get("home_team_name")),
        "away_team": to_node_team(away_team, meta.get("away_team_name")),
        "bet_percent": None,
        "money_percent": None,
        "odds": {
            "ml_away": lines.get("ml_away"), "ml_away_public": pcts.get("ml_away_public"), "ml_away_money": pcts.get("ml_away_money"),
            "ml_home": lines.get("ml_home"), "ml_home_public": pcts.get("ml_home_public"), "ml_home_money": pcts.get("ml_home_money"),
            "spread_away": lines.get("spread_away"), "spread_away_public": pcts.get("spread_away_public"), "spread_away_money": pcts.get("spread_away_money"),
            "spread_home": lines.get("spread_home"), "spread_home_public": pcts.get("spread_home_public"), "spread_home_money": pcts.get("spread_home_money"),
            "total": lines.get("total"),
            "total_over_public": pcts.get("over_public"), "total_over_money": pcts.get("over_money"),
            "total_under_public": pcts.get("under_public"), "total_under_money": pcts.get("under_money"),
            "over": lines.get("over_odds"), "under": lines.get("under_odds"),
            "away_total": None, "away_over": None, "away_under": None,
            "home_total": None, "home_over": None, "home_under": None,
        },
    }


# ticker

def fmt_time(iso):
    u = to_unix(iso)
    return datetime.fromtimestamp(u, ET).strftime("%-I:%M %p ET") if u is not None else ""


def ticker_for(g):
    away = g["away_team"].get("abbr") or g["away_team"].get("full_name") or ""
    home = g["home_team"].get("abbr") or g["home_team"].get("full_name") or ""
    line = g["odds"].get("spread_home")
    line_str = "" if line is None else (f"+{line}" if line > 0 else f"{line}")
    total = g["odds"].get("total")
    ou = "" if total is None else f"O/U {total}"
    inner = ", ".join(x for x in (line_str, ou) if x)
    t = fmt_time(g.get("start_time"))
    return f"{away} @ {home}" + (f" ({inner})" if inner else "") + (f" {t}" if t else "")


def build_ticker(games):
    return "     ".join(ticker_for(g) for g in games)


# Singular push

def put_datanode(node, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://app.singular.live/apiv1/datanodes/{node}/data",
        data=body, method="PUT", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Singular PUT {node} -> {e.code}: {e.read().decode('utf-8','replace')[:200]}")


# bulk markets: group rows by eventId, reuse map_main_lines per game

def fetch_bulk_markets(sport, league, api_key):
    url = f"{BASE_URL}/{sport}/{league}/markets?sportsbookIds={SPORTSBOOK_IDS}&live=false"
    payload = fetch(url, api_key)
    rows = (payload or {}).get("results", []) if isinstance(payload, dict) else (payload or [])
    by_event = defaultdict(list)
    for row in rows:
        eid = row.get("eventId")
        if eid is not None:
            by_event[eid].append(row)
    return by_event


# cycle

_teams_cache = {}

def get_teams(sport, league, api_key):
    if league not in _teams_cache:
        _teams_cache[league] = fetch(f"{BASE_URL}/{sport}/{league}/teams", api_key)
    return _teams_cache[league]


def collect_for_league(league, api_key, now_unix):
    sport = resolve_sport(league, None)
    teams = get_teams(sport, league, api_key)
    events = fetch_slate_events(sport, league, api_key)
    markets_by_event = fetch_bulk_markets(sport, league, api_key)  # 1 call for all odds

    league_name = league.lower()
    horizon = now_unix + window_hours(league) * 3600
    games, kept, percents_calls = {}, 0, 0

    for ev in events:
        event = unwrap_event(ev) or ev
        meta = map_event_metadata(event)

        if (meta.get("status") or "").lower() in CLOSED_STATUSES:
            continue
        st = to_unix(meta.get("start_time"))
        if st is not None and st > horizon:
            continue  # too far out for this league's window

        home_id, away_id = meta.get("home_team_id"), meta.get("away_team_id")
        eid = meta.get("event_id")

        lines = map_main_lines({"results": markets_by_event.get(eid, [])},
                               home_id, away_id, PREFERRED_BOOK)

        pcts = {}
        if NEED_PERCENTS:
            p = fetch(f"{BASE_URL}/events/{eid}/markets/main/percents", api_key)
            percents_calls += 1
            pcts = map_percents(p, home_id, away_id)

        home_team = map_team_metadata(teams, home_id) if teams else {}
        away_team = map_team_metadata(teams, away_id) if teams else {}

        g = assemble_node_game(meta, lines, pcts, home_team, away_team, league_name)
        if g.get("id") is not None:
            games[str(g["id"])] = g
            kept += 1

    return games, {"kept": kept, "slate": len(events), "percents": percents_calls}


def collect_games(api_key):
    now_unix = int(datetime.now(timezone.utc).timestamp())
    all_games, stats = {}, {}
    for league in LEAGUES:
        g, s = collect_for_league(league, api_key, now_unix)
        all_games.update(g)
        stats[league] = s
    return all_games, stats


def fetch_slate_events(sport, league, api_key):
    today = datetime.now(ET).date()
    start = today.isoformat()
    # Fetch range follows the window (+1 day buffer) so a large WINDOW_HOURS
    # still pulls those games. Minimum 2 days so same-day always works.
    span_days = max(2, window_hours(league) // 24 + 1)
    end = (today + timedelta(days=span_days)).isoformat()
    payload = fetch(f"{BASE_URL}/{sport}/{league}/events?startDate={start}&endDate={end}", api_key)
    if not payload:
        return []
    return payload.get("results") or payload.get("result") or (payload if isinstance(payload, list) else [])


def tick():
    api_key = get_api_key()
    games, stats = collect_games(api_key)
    put_datanode(GAME_NODE, {GAME_FIELD: json.dumps(games)})
    put_datanode(TICKER_NODE, {TICKER_FIELD: build_ticker(games.values())})
    breakdown = ", ".join(f"{k}:{v['kept']}/{v['slate']}(p{v['percents']})" for k, v in stats.items())
    print(f"[{datetime.now(timezone.utc).isoformat()}] pushed {len(games)} game(s) | {breakdown}")


def inspect():
    api_key = get_api_key()
    games, stats = collect_games(api_key)
    for k, v in stats.items():
        calls = 2 + v["percents"]  # slate + bulk markets + percents (teams cached)
        print(f"{k}: kept {v['kept']} of {v['slate']} | ~{calls} CORE calls this cycle")
    print(f"total games: {len(games)}")
    if games:
        first = next(iter(games.values()))
        print("\n--- first MAPPED node game ---")
        print(json.dumps(first, indent=2, default=str))
        print("\n--- sample ticker ---")
        print(ticker_for(first))
    print("\n(inspect mode: nothing pushed to Singular)")


def main():
    if MODE == "inspect":
        inspect()
        return
    print(f"feeder up | leagues={','.join(LEAGUES)} | poll {POLL_SECONDS}s | "
          + " ".join(f"{l}={window_hours(l)}h" for l in LEAGUES))
    while True:
        try:
            tick()
        except Exception as e:
            print("tick error:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
