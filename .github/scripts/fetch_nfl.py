#!/usr/bin/env python3
"""Fetches the NFL season schedule + standings from ESPN's public API and
writes nfl-f1-schedules/data/nfl-schedule.json and nfl-standings.json.

ESPN's site.api.espn.com endpoints are undocumented but widely used
community-known JSON APIs; no API key required. Since their exact field
names can drift, this script validates the shape it expects before writing
and refuses to overwrite good data with an empty/malformed result.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import fetch_json, write_json_atomic, die  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCHEDULE_OUT = os.path.join(REPO_ROOT, "nfl-f1-schedules", "data", "nfl-schedule.json")
STANDINGS_OUT = os.path.join(REPO_ROOT, "nfl-f1-schedules", "data", "nfl-standings.json")

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"

EAGLES_ABBR = "PHI"

SEASON_TYPES = {
    2: ("regular", range(1, 19)),   # regular season, weeks 1-18
    3: ("postseason", range(1, 6)),  # wild card through Super Bowl
}


def season_year(today=None):
    today = today or datetime.date.today()
    # NFL "season" is named for the year it starts in (Sep); Jan/Feb games
    # still belong to the season that started the previous calendar year.
    return today.year if today.month >= 3 else today.year - 1


def normalize_status(state):
    return {"pre": "scheduled", "in": "in_progress", "post": "final"}.get(state, "scheduled")


def parse_event(event, season_type_label, week_num):
    comp = event["competitions"][0]
    competitors = comp["competitors"]
    home = next(c for c in competitors if c.get("homeAway") == "home")
    away = next(c for c in competitors if c.get("homeAway") == "away")
    status_state = comp.get("status", {}).get("type", {}).get("state", "pre")
    status = normalize_status(status_state)

    def score(c):
        val = c.get("score")
        if val in (None, ""):
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    home_abbr = (home.get("team") or {}).get("abbreviation", "")
    away_abbr = (away.get("team") or {}).get("abbreviation", "")

    return {
        "id": str(event.get("id")),
        "week": week_num,
        "season_type": season_type_label,
        "date_utc": comp.get("date") or event.get("date"),
        "status": status,
        "home_team": (home.get("team") or {}).get("displayName", "Unknown"),
        "home_abbr": home_abbr,
        "away_team": (away.get("team") or {}).get("displayName", "Unknown"),
        "away_abbr": away_abbr,
        "home_score": score(home) if status != "scheduled" else None,
        "away_score": score(away) if status != "scheduled" else None,
        "venue": (comp.get("venue") or {}).get("fullName", ""),
        "is_favorite": home_abbr == EAGLES_ABBR or away_abbr == EAGLES_ABBR,
    }


def fetch_schedule(year):
    games = []
    for season_type, (label, weeks) in SEASON_TYPES.items():
        for week in weeks:
            url = f"{SCOREBOARD_URL}?seasontype={season_type}&week={week}&year={year}&limit=100"
            try:
                raw = fetch_json(url)
            except Exception as e:
                print(f"WARN: failed to fetch {label} week {week}: {e}", file=sys.stderr)
                continue
            if "events" not in raw:
                print(f"WARN: unexpected shape for {label} week {week}, keys={list(raw.keys())}", file=sys.stderr)
                continue
            for event in raw["events"]:
                try:
                    games.append(parse_event(event, label, week))
                except (KeyError, StopIteration, TypeError) as e:
                    print(f"WARN: skipping malformed event {event.get('id')}: {e}", file=sys.stderr)
    return games


def parse_division(div):
    name = div.get("name") or div.get("abbreviation") or "Division"
    entries = ((div.get("standings") or {}).get("entries")) or []
    teams = []
    for entry in entries:
        team = entry.get("team") or {}
        stats = {s.get("name"): s.get("value") for s in entry.get("stats", []) if isinstance(s, dict)}
        abbr = team.get("abbreviation", "")
        teams.append({
            "team": team.get("displayName", "Unknown"),
            "abbr": abbr,
            "wins": int(stats.get("wins", 0) or 0),
            "losses": int(stats.get("losses", 0) or 0),
            "ties": int(stats.get("ties", 0) or 0),
            "win_pct": stats.get("winPercent", stats.get("percentage")),
            "division_rank": int(stats.get("divisionRank", stats.get("rank", 0)) or 0),
            "is_eagles": abbr == EAGLES_ABBR,
        })
    teams.sort(key=lambda t: t["division_rank"] or 99)
    return {"name": name, "teams": teams}


def fetch_standings(year):
    raw = fetch_json(f"{STANDINGS_URL}?season={year}")
    top_children = raw.get("children") or []
    if not top_children:
        die(f"UNEXPECTED STANDINGS SHAPE: no 'children' key, top-level keys={list(raw.keys())}")

    conferences = []
    for conf in top_children:
        conf_name = conf.get("name") or conf.get("abbreviation") or "Conference"
        div_children = conf.get("children")
        if div_children:
            divisions = [parse_division(d) for d in div_children]
        else:
            divisions = [parse_division(conf)]
        conferences.append({"name": conf_name, "divisions": divisions})
    return conferences


def main():
    year = season_year()

    games = fetch_schedule(year)
    if len(games) == 0:
        die("ZERO games parsed from ESPN scoreboard — refusing to overwrite existing schedule data")
    write_json_atomic(SCHEDULE_OUT, {
        "season": year,
        "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "games": games,
    })
    print(f"Wrote {len(games)} NFL games to {SCHEDULE_OUT}")

    try:
        conferences = fetch_standings(year)
    except SystemExit:
        raise
    except Exception as e:
        die(f"STANDINGS FETCH FAILED: {e}")

    total_teams = sum(len(d["teams"]) for c in conferences for d in c["divisions"])
    if total_teams == 0:
        die("ZERO teams parsed from ESPN standings — refusing to overwrite existing standings data")
    write_json_atomic(STANDINGS_OUT, {
        "season": year,
        "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "conferences": conferences,
    })
    print(f"Wrote NFL standings ({total_teams} teams) to {STANDINGS_OUT}")


if __name__ == "__main__":
    main()
