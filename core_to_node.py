#!/usr/bin/env python3
"""
CORE → Node mapper.

Takes raw CORE API responses for one game and returns a flat dict shaped
for your singular.live graphics / data-node schema (team metadata, a
single consensus odds line, public-bet and public-money splits).

Designed to be:
- Pure (no network calls inside the mapping functions); easy to unit-test
  against existing dump files.
- Deterministic; same inputs always produce the same node row.
- Defensive; tolerates missing fields, missing percents, single-book
  coverage, etc., without throwing.

There's also a CLI wrapper at the bottom that fetches from CORE and
prints the mapped node row, so you can verify field-by-field against
what the old feeder produced.

Usage:
    python3 core_to_node.py --league MLB --event-id 12283530
    python3 core_to_node.py --league MLB --event-id 12283530 --book 15  # force FanDuel
    python3 core_to_node.py --league MLB --event-id 12283530 --pretty
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import urllib.request
import urllib.error

BASE_URL = "https://core-external-api.actionnetwork.com"

# Books that returned data in our coverage tests, in preference order.
# Mapper picks the FIRST one with full main-line coverage for a given event.
BOOK_PREFERENCE = [15, 3, 123, 68, 49, 69, 75, 247]
SPORTSBOOK_IDS = ",".join(str(b) for b in BOOK_PREFERENCE)

SPORTSBOOK_NAMES = {
    3: "DraftKings", 15: "FanDuel", 49: "BetMGM", 68: "Caesars",
    69: "PointsBet", 75: "BetRivers", 123: "Bet365", 146: "ESPN BET",
    247: "Hard Rock", 477: "Fanatics", 911: "Fliff",
}

SPORT_ROOT_BY_LEAGUE = {
    "MLB": "baseball",
    "NBA": "basketball", "WNBA": "basketball",
    "NCAAMB": "basketball", "NCAAWB": "basketball",
    "NFL": "football", "NCAAFB": "football",
}

DUMP_DIR = Path("dumps")


# ─── .env loading ──────────────────────────────────────────────────────────

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_script_dir = Path(__file__).resolve().parent
load_dotenv(_script_dir / ".env.local")
load_dotenv(_script_dir / ".env")


# ─── HTTP (cache-aware) ────────────────────────────────────────────────────

def fetch(url: str, api_key: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={
        "X-API-KEY": api_key, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"  HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return None


def cached_or_fetch(cache_path: Path, url: str, api_key: str, use_cache: bool) -> Optional[dict]:
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text())
    data = fetch(url, api_key)
    if data is not None and not use_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, indent=2, default=str))
    return data


# ─── CORE response unwrapping ──────────────────────────────────────────────

def unwrap_event(payload: dict) -> Optional[dict]:
    """CORE's /events/{id} returns {"result": {...}} (singular). Unwrap."""
    if not isinstance(payload, dict):
        return None
    if "id" in payload:
        return payload
    if "result" in payload and isinstance(payload["result"], dict):
        return payload["result"]
    if "results" in payload:
        results = payload["results"]
        if isinstance(results, list) and results:
            return results[0]
    return None


# ─── The core mapping functions (PURE — no network) ─────────────────────────

def map_event_metadata(event: dict) -> dict:
    """Pull team metadata, schedule, and status from a CORE event object."""
    teams = event.get("teams", []) or []
    home = next((t for t in teams if t.get("side") == "HOME"), {})
    away = next((t for t in teams if t.get("side") == "AWAY"), {})

    event_status = (event.get("eventStatus") or {})
    status_name = event_status.get("name")

    # Winning team — CORE typically populates eventStatus with a winnerTeamId
    # once the game is final. Fall back to scanning the team objects too.
    winning_team_id = event_status.get("winnerTeamId") or event.get("winnerTeamId")

    return {
        "event_id": event.get("id"),
        "league_id": event.get("leagueId"),
        "start_time": event.get("scheduledDate"),
        "status": status_name,
        "status_id": event_status.get("id"),
        "season": (event.get("season") or {}).get("name"),
        "season_schedule": (event.get("seasonSchedule") or {}).get("name"),
        "venue_id": event.get("venueId"),
        "broadcast": (event.get("broadcast") or {}).get("network"),
        "winning_team_id": winning_team_id,

        # Teams — minimal fields. Logos/colors come from /teams hydration.
        "home_team_id": home.get("id"),
        "home_team_name": home.get("name"),
        "away_team_id": away.get("id"),
        "away_team_name": away.get("name"),
    }


def map_team_metadata(teams_payload: dict, team_id: int) -> dict:
    """
    Pull logo, colors, abbreviation, full name from /<sport>/<league>/teams.
    Field names vary by sport — soccer has homeKit/primaryColorHexCode;
    other sports may use 'colors' or 'primaryColor'. Tries common variants.
    """
    if not teams_payload or not team_id:
        return {}
    teams_list = teams_payload.get("results", []) if isinstance(teams_payload, dict) else []
    team = next((t for t in teams_list if t.get("id") == team_id), None)
    if not team:
        return {}

    return {
        "id": team.get("id"),
        "name": team.get("name"),
        "full_name": (team.get("displayName")
                      or team.get("fullName")
                      or team.get("name")),
        "abbr": team.get("abbreviation") or team.get("abbr"),
        "nickname": team.get("nickname"),
        "location": team.get("location"),
        "logo": team.get("logo") or team.get("imageUrl"),
        "primary_color": (team.get("primaryColorHexCode")
                          or team.get("primaryColor")
                          or (team.get("colors") or {}).get("primary")),
        "secondary_color": (team.get("secondaryColorHexCode")
                            or team.get("secondaryColor")
                            or (team.get("colors") or {}).get("secondary")),
        "conference": (team.get("conference") or {}).get("name") if isinstance(team.get("conference"), dict) else team.get("conference"),
        "division": (team.get("division") or {}).get("name") if isinstance(team.get("division"), dict) else team.get("division"),
    }


def map_main_lines(markets_payload: dict,
                   home_team_id: int,
                   away_team_id: int,
                   preferred_book: Optional[int] = None) -> dict:
    """
    Extract a single consensus main-line set (spread/ML/total) from
    /events/{id}/markets.

    Strategy:
    - Filter to betType.id == 1 (Matchup umbrella) AND linePeriodType.name
      == "Game" (game-level only — excludes inning/half/quarter markets).
    - Pick a single book per the preference order. By default, the first
      book that has all three lines present. Override with preferred_book.
    - Return flat fields: spread_home, spread_home_odds, spread_away,
      spread_away_odds, ml_home, ml_away, total, over_odds, under_odds.
    """
    out = {
        "book_id": None, "book_name": None,
        "spread_home": None, "spread_home_odds": None,
        "spread_away": None, "spread_away_odds": None,
        "ml_home": None, "ml_away": None,
        "total": None, "over_odds": None, "under_odds": None,
    }
    if not markets_payload:
        return out

    results = markets_payload.get("results", []) if isinstance(markets_payload, dict) else []

    # Filter to game-level main markets
    main_markets = []
    for m in results:
        bt = (m.get("betType") or {}).get("id")
        lpt_name = ((m.get("linePeriodType") or {}).get("name") or "").lower()
        if bt == 1 and lpt_name in {"game", "match", "full game", "full match", "regulation"}:
            main_markets.append(m)

    if not main_markets:
        return out

    # Bucket outcomes by (lineType, sportsbookId)
    by_book: dict = {}  # book_id -> {"spread": [], "moneyline": [], "total": []}
    for m in main_markets:
        lt_name = (m.get("lineType") or {}).get("name", "")
        for o in m.get("outcomes", []):
            book = o.get("sportsbookId")
            if book is None:
                continue
            by_book.setdefault(book, {"Spread": [], "Moneyline": [], "Total": []})
            if lt_name in by_book[book]:
                by_book[book][lt_name].append(o)

    # Pick book: explicit preference, else first one with all three lines
    chosen_book = None
    if preferred_book is not None and preferred_book in by_book:
        chosen_book = preferred_book
    else:
        for b in BOOK_PREFERENCE:
            if b in by_book and all(by_book[b][k] for k in ("Spread", "Moneyline", "Total")):
                chosen_book = b
                break
        # Fall back: any book with at least one of the three
        if chosen_book is None and by_book:
            chosen_book = sorted(by_book.keys(),
                                 key=lambda b: -sum(len(v) for v in by_book[b].values()))[0]

    if chosen_book is None:
        return out

    out["book_id"] = chosen_book
    out["book_name"] = SPORTSBOOK_NAMES.get(chosen_book)
    bucket = by_book[chosen_book]

    # Spread — resolve sides by teamId
    for o in bucket["Spread"]:
        if o.get("teamId") == home_team_id:
            out["spread_home"] = o.get("line")
            out["spread_home_odds"] = o.get("americanOdds")
        elif o.get("teamId") == away_team_id:
            out["spread_away"] = o.get("line")
            out["spread_away_odds"] = o.get("americanOdds")

    # Moneyline — resolve sides by teamId
    for o in bucket["Moneyline"]:
        if o.get("teamId") == home_team_id:
            out["ml_home"] = o.get("americanOdds")
        elif o.get("teamId") == away_team_id:
            out["ml_away"] = o.get("americanOdds")

    # Total — Over/Under via optionType; line value should match across both
    for o in bucket["Total"]:
        opt = (o.get("optionType") or "").lower()
        if opt == "over":
            out["total"] = o.get("line")  # store on Over; same value as Under
            out["over_odds"] = o.get("americanOdds")
        elif opt == "under":
            if out["total"] is None:
                out["total"] = o.get("line")
            out["under_odds"] = o.get("americanOdds")

    return out


def map_percents(percents_payload: dict,
                 home_team_id: int,
                 away_team_id: int) -> dict:
    """
    Map /events/{id}/markets/main/percents to flat public/money splits.

    The endpoint returns 12 rows per event (when populated):
    Spread × {home,away} × {Ticket,Volume}
    Moneyline × {home,away} × {Ticket,Volume}
    Total × {Over,Under} × {Ticket,Volume}

    percentType.id == 1 → 'public' (Ticket count share)
    percentType.id == 2 → 'money' (Volume / dollars share)
    """
    out = {
        "ml_home_public": None, "ml_home_money": None,
        "ml_away_public": None, "ml_away_money": None,
        "spread_home_public": None, "spread_home_money": None,
        "spread_away_public": None, "spread_away_money": None,
        "over_public": None, "over_money": None,
        "under_public": None, "under_money": None,
    }
    if not percents_payload:
        return out

    results = percents_payload.get("results", []) if isinstance(percents_payload, dict) else []

    for row in results:
        line_name = (row.get("lineType") or {}).get("name", "").lower()
        ptype_id = (row.get("percentType") or {}).get("id")
        suffix = "money" if ptype_id == 2 else "public" if ptype_id == 1 else None
        if suffix is None:
            continue
        pct = row.get("percent")

        if line_name == "total":
            opt = ((row.get("sideOptionType") or {}).get("name") or "").lower()
            if opt in ("over", "under"):
                out[f"{opt}_{suffix}"] = pct
        else:
            tid = row.get("teamId")
            if tid == home_team_id:
                side = "home"
            elif tid == away_team_id:
                side = "away"
            else:
                continue
            prefix = "ml" if line_name == "moneyline" else "spread"
            out[f"{prefix}_{side}_{suffix}"] = pct

    return out


def build_node_row(event_payload: dict,
                   markets_payload: dict,
                   percents_payload: dict,
                   teams_payload: Optional[dict] = None,
                   preferred_book: Optional[int] = None) -> dict:
    """
    Assemble the full node row from all four CORE payloads.
    Returns a single flat dict ready for the data node / singular.live.
    """
    event = unwrap_event(event_payload) or {}

    meta = map_event_metadata(event)
    home_id = meta.get("home_team_id")
    away_id = meta.get("away_team_id")

    lines = map_main_lines(markets_payload, home_id, away_id, preferred_book)
    pcts = map_percents(percents_payload, home_id, away_id)

    home_team = map_team_metadata(teams_payload, home_id) if teams_payload else {}
    away_team = map_team_metadata(teams_payload, away_id) if teams_payload else {}

    return {
        # Event metadata
        **meta,

        # Team metadata (hydrated from /teams when available)
        "home_team": home_team,
        "away_team": away_team,

        # Consensus odds line (one book)
        **lines,

        # Public betting splits
        **pcts,

        # Source attribution
        "_captured_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── CLI wrapper ───────────────────────────────────────────────────────────

def get_api_key() -> str:
    key = os.environ.get("CORE_API_KEY")
    if not key:
        print("ERROR: CORE_API_KEY not found in env, .env, or .env.local.", file=sys.stderr)
        sys.exit(1)
    return key


def resolve_sport(league: str, override: Optional[str]) -> str:
    if override:
        return override
    return SPORT_ROOT_BY_LEAGUE.get(league.upper(), "soccer")


def parse_args():
    p = argparse.ArgumentParser(description="Map one CORE game to node-row shape.")
    p.add_argument("--league", required=True)
    p.add_argument("--event-id", required=True, type=int)
    p.add_argument("--sport", default=None)
    p.add_argument("--book", type=int, default=None,
                   help="Force a specific sportsbookId for the main lines.")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return p.parse_args()


def main():
    args = parse_args()
    api_key = get_api_key()
    sport = resolve_sport(args.league, args.sport)
    eid = args.event_id
    league = args.league
    use_cache = not args.no_cache

    # Fetch the four CORE payloads (with caching)
    event_url = f"{BASE_URL}/{sport}/{league}/events/{eid}"
    markets_url = (f"{BASE_URL}/{sport}/{league}/events/{eid}/markets?"
                   + urlencode({"sportsbookIds": SPORTSBOOK_IDS, "live": "false"}))
    percents_url = f"{BASE_URL}/events/{eid}/markets/main/percents"
    teams_url = f"{BASE_URL}/{sport}/{league}/teams"

    event = cached_or_fetch(DUMP_DIR / f"{league}_{eid}_event.json",
                            event_url, api_key, use_cache)
    markets = cached_or_fetch(DUMP_DIR / f"{league}_{eid}_markets.json",
                              markets_url, api_key, use_cache)
    percents = cached_or_fetch(DUMP_DIR / f"{league}_{eid}_percents.json",
                               percents_url, api_key, use_cache)
    teams = cached_or_fetch(DUMP_DIR / f"{league}_teams.json",
                            teams_url, api_key, use_cache)

    node_row = build_node_row(event, markets, percents, teams, preferred_book=args.book)
    indent = 2 if args.pretty else None
    print(json.dumps(node_row, indent=indent, default=str))


if __name__ == "__main__":
    main()