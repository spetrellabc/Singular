#!/usr/bin/env python3
"""
Slate feeder for the BBOC_NFL Singular graphics.

Pipeline:
  CORE External (events + markets + percents + teams)
    -> core_to_node.build_node_row()   (your existing flat intermediate)
    -> to_node_game()                  (flat -> Singular node shape)
    -> PUT to Singular data nodes 3846 (games) + 4084 (ticker)

Place this file NEXT TO core_to_node.py (same directory) so the import works.

Run:
  MODE=inspect python3 feeder.py    # pull today's slate, print, push NOTHING
  python3 feeder.py                 # live: pull + push on a loop
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# Reuse everything you already built.
from core_to_node import (
    BASE_URL,
    SPORTSBOOK_IDS,
    fetch,
    build_node_row,
    resolve_sport,
    get_api_key,
)

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone.utc  # fallback if tzdata is missing

# ─── config (env) ───────────────────────────────────────────────────────────

LEAGUE        = os.environ.get("LEAGUE", "MLB")
GAME_NODE     = os.environ.get("SINGULAR_GAME_NODE", "3846")
TICKER_NODE   = os.environ.get("SINGULAR_TICKER_NODE", "4084")
GAME_FIELD    = os.environ.get("SINGULAR_GAME_FIELD", "games")
TICKER_FIELD  = os.environ.get("SINGULAR_TICKER_FIELD", "ticker")
POLL_SECONDS  = int(os.environ.get("POLL_SECONDS", "30"))
MODE          = os.environ.get("MODE", "live")  # live | inspect
PREFERRED_BOOK = os.environ.get("PREFERRED_BOOK")  # optional sportsbookId
PREFERRED_BOOK = int(PREFERRED_BOOK) if PREFERRED_BOOK else None

# league_id 3 == MLB, but we derive the node's league_name straight from LEAGUE.
NODE_LEAGUE_NAME = LEAGUE.lower()


# ─── slate URL  >>> CONFIRM PARAM NAMES IN SWAGGER <<< ───────────────────────
# This is the ONE thing I can't verify: the exact query params on
# /baseball/{leagueId}/events ("Events by date range"). Open that endpoint in
# the CORE Swagger and check whether it wants startDate/endDate, from/to, or
# date — and the format. Adjust the line below to match, then `MODE=inspect`.
def build_slate_url(sport: str) -> str:
    today = datetime.now(ET).date()
    start = today.isoformat()
    end = (today + timedelta(days=1)).isoformat()
    # Most likely shape — edit to match Swagger:
    return f"{BASE_URL}/{sport}/{LEAGUE}/events?startDate={start}&endDate={end}"


# ─── flat (core_to_node) -> Singular node shape ──────────────────────────────

def to_unix(iso):
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def to_node_team(team: dict, fallback_name=None) -> dict:
    team = team or {}
    return {
        "id": team.get("id"),
        # node full_name is the full "City Nickname" form == CORE team `name`
        "full_name": team.get("name") or team.get("full_name") or fallback_name,
        "logo": team.get("logo"),
        "primary_color": team.get("primary_color"),
        "secondary_color": team.get("secondary_color"),
        "abbr": team.get("abbr"),
    }


def to_node_game(r: dict) -> dict:
    """Transform one build_node_row() flat dict into the data-node game shape."""
    return {
        "id": r.get("event_id"),
        "league_name": NODE_LEAGUE_NAME,
        "status": r.get("status"),
        "start_time": r.get("start_time"),
        "away_team_id": r.get("away_team_id"),
        "home_team_id": r.get("home_team_id"),
        "winning_team_id": r.get("winning_team_id"),
        "start_time_unix": to_unix(r.get("start_time")),
        "home_team": to_node_team(r.get("home_team"), r.get("home_team_name")),
        "away_team": to_node_team(r.get("away_team"), r.get("away_team_name")),
        "bet_percent": None,
        "money_percent": None,
        "odds": {
            "ml_away": r.get("ml_away"), "ml_away_public": r.get("ml_away_public"), "ml_away_money": r.get("ml_away_money"),
            "ml_home": r.get("ml_home"), "ml_home_public": r.get("ml_home_public"), "ml_home_money": r.get("ml_home_money"),
            "spread_away": r.get("spread_away"), "spread_away_public": r.get("spread_away_public"), "spread_away_money": r.get("spread_away_money"),
            "spread_home": r.get("spread_home"), "spread_home_public": r.get("spread_home_public"), "spread_home_money": r.get("spread_home_money"),
            "total": r.get("total"),
            "total_over_public": r.get("over_public"), "total_over_money": r.get("over_money"),
            "total_under_public": r.get("under_public"), "total_under_money": r.get("under_money"),
            "over": r.get("over_odds"), "under": r.get("under_odds"),
            "away_total": None, "away_over": None, "away_under": None,
            "home_total": None, "home_over": None, "home_under": None,
        },
    }


# ─── ticker ──────────────────────────────────────────────────────────────────

def fmt_time(iso):
    u = to_unix(iso)
    if u is None:
        return ""
    return datetime.fromtimestamp(u, ET).strftime("%-I:%M %p ET")


def ticker_for(g: dict) -> str:
    away = g["away_team"].get("abbr") or g["away_team"].get("full_name") or ""
    home = g["home_team"].get("abbr") or g["home_team"].get("full_name") or ""
    line = g["odds"].get("spread_home")
    line_str = "" if line is None else (f"+{line}" if line > 0 else f"{line}")
    total = g["odds"].get("total")
    ou = "" if total is None else f"O/U {total}"
    inner = ", ".join(x for x in (line_str, ou) if x)
    t = fmt_time(g.get("start_time"))
    return f"{away} @ {home}" + (f" ({inner})" if inner else "") + (f" {t}" if t else "")


def build_ticker(games) -> str:
    return "     ".join(ticker_for(g) for g in games)


# ─── CORE fetches ────────────────────────────────────────────────────────────

def fetch_slate_events(sport: str, api_key: str) -> list:
    payload = fetch(build_slate_url(sport), api_key)
    if not payload:
        return []
    return payload.get("results") or payload.get("result") or (payload if isinstance(payload, list) else [])


def hydrate_event(event: dict, sport: str, api_key: str, teams_payload) -> dict:
    eid = event.get("id")
    markets = fetch(
        f"{BASE_URL}/{sport}/{LEAGUE}/events/{eid}/markets?sportsbookIds={SPORTSBOOK_IDS}&live=false",
        api_key,
    )
    percents = fetch(f"{BASE_URL}/events/{eid}/markets/main/percents", api_key)
    flat = build_node_row(event, markets, percents, teams_payload, preferred_book=PREFERRED_BOOK)
    return to_node_game(flat)


# ─── Singular push ───────────────────────────────────────────────────────────

def put_datanode(node: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://app.singular.live/apiv1/datanodes/{node}/data",
        data=body, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Singular PUT {node} -> {e.code}: {e.read().decode('utf-8','replace')[:200]}")


# ─── cycle ───────────────────────────────────────────────────────────────────

_teams_cache = {}  # sport -> teams_payload (logos/colors are static within a run)

def get_teams(sport: str, api_key: str):
    if sport not in _teams_cache:
        _teams_cache[sport] = fetch(f"{BASE_URL}/{sport}/{LEAGUE}/teams", api_key)
    return _teams_cache[sport]


def tick():
    api_key = get_api_key()
    sport = resolve_sport(LEAGUE, None)
    teams = get_teams(sport, api_key)
    events = fetch_slate_events(sport, api_key)

    games = {}
    for ev in events:
        g = hydrate_event(ev, sport, api_key, teams)
        if g.get("id") is not None:
            games[str(g["id"])] = g

    put_datanode(GAME_NODE, {GAME_FIELD: json.dumps(games)})
    put_datanode(TICKER_NODE, {TICKER_FIELD: build_ticker(games.values())})
    print(f"[{datetime.now(timezone.utc).isoformat()}] pushed {len(games)} game(s)")


def inspect():
    api_key = get_api_key()
    sport = resolve_sport(LEAGUE, None)
    url = build_slate_url(sport)
    print("GET", url)
    events = fetch_slate_events(sport, api_key)
    print(f"games in slate: {len(events)}")
    if events:
        teams = get_teams(sport, api_key)
        g = hydrate_event(events[0], sport, api_key, teams)
        print("\n--- first MAPPED node game ---")
        print(json.dumps(g, indent=2, default=str))
        print("\n--- sample ticker ---")
        print(ticker_for(g))
    print("\n(inspect mode: nothing pushed to Singular)")


def main():
    if MODE == "inspect":
        inspect()
        return
    print(f"feeder up | league={LEAGUE} | game={GAME_NODE} ticker={TICKER_NODE} | every {POLL_SECONDS}s")
    while True:
        try:
            tick()
        except Exception as e:
            print("tick error:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
