#!/usr/bin/env python3
"""Fetches the NFL season schedule + standings from ESPN's public API and
writes nfl-schedule/data/schedule.json and standings.json.

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
SCHEDULE_OUT = os.path.join(REPO_ROOT, "nfl-schedule", "data", "schedule.json")
STANDINGS_OUT = os.path.join(REPO_ROOT, "nfl-schedule", "data", "standings.json")

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"

EAGLES_ABBR = "PHI"

# NFL divisions are fixed and don't change season to season. ESPN's
# standings endpoint returns each conference's 16 teams as one flat list
# with no division-level nesting, so we group them ourselves rather than
# relying on API structure that doesn't actually exist.
TEAM_DIVISIONS = {
    "BUF": "AFC East", "MIA": "AFC East", "NE": "AFC East", "NYJ": "AFC East",
    "BAL": "AFC North", "CIN": "AFC North", "CLE": "AFC North", "PIT": "AFC North",
    "HOU": "AFC South", "IND": "AFC South", "JAX": "AFC South", "TEN": "AFC South",
    "DEN": "AFC West", "KC": "AFC West", "LV": "AFC West", "LAC": "AFC West",
    "DAL": "NFC East", "NYG": "NFC East", "PHI": "NFC East", "WSH": "NFC East",
    "CHI": "NFC North", "DET": "NFC North", "GB": "NFC North", "MIN": "NFC North",
    "ATL": "NFC South", "CAR": "NFC South", "NO": "NFC South", "TB": "NFC South",
    "ARI": "NFC West", "LAR": "NFC West", "SF": "NFC West", "SEA": "NFC West",
}

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
        "home_record": None,
        "away_record": None,
    }


def format_record(wins, losses, ties):
    return f"{wins}-{losses}-{ties}" if ties else f"{wins}-{losses}"


def compute_running_records(games):
    """Fills in each game's home_record/away_record: each team's W-L-T
    entering that game (not their final season record), computed by
    replaying the regular season in chronological order. Postseason games
    show both teams' final regular-season record, matching how NFL
    schedules conventionally display it (the record itself doesn't keep
    incrementing through the playoffs)."""
    record = {}

    def tally(abbr):
        return record.setdefault(abbr, {"w": 0, "l": 0, "t": 0})

    regular = sorted(
        (g for g in games if g["season_type"] == "regular"),
        key=lambda g: g["date_utc"] or "",
    )
    for g in regular:
        home, away = tally(g["home_abbr"]), tally(g["away_abbr"])
        g["home_record"] = format_record(home["w"], home["l"], home["t"])
        g["away_record"] = format_record(away["w"], away["l"], away["t"])

        if g["status"] == "final" and g["home_score"] is not None and g["away_score"] is not None:
            if g["home_score"] > g["away_score"]:
                home["w"] += 1
                away["l"] += 1
            elif g["away_score"] > g["home_score"]:
                away["w"] += 1
                home["l"] += 1
            else:
                home["t"] += 1
                away["t"] += 1

    for g in games:
        if g["season_type"] != "regular":
            home, away = tally(g["home_abbr"]), tally(g["away_abbr"])
            g["home_record"] = format_record(home["w"], home["l"], home["t"])
            g["away_record"] = format_record(away["w"], away["l"], away["t"])

    return games


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
    return compute_running_records(games)


def parse_team_entry(entry):
    team = entry.get("team") or {}
    stats = {s.get("name"): s.get("value") for s in entry.get("stats", []) if isinstance(s, dict)}
    abbr = team.get("abbreviation", "")
    return {
        "team": team.get("displayName", "Unknown"),
        "abbr": abbr,
        "wins": int(stats.get("wins", 0) or 0),
        "losses": int(stats.get("losses", 0) or 0),
        "ties": int(stats.get("ties", 0) or 0),
        "win_pct": stats.get("winPercent", stats.get("percentage")) or 0.0,
        "division_rank": 0,  # filled in below, after grouping into real divisions
        "is_eagles": abbr == EAGLES_ABBR,
    }


def group_by_division(teams):
    groups = {}
    for t in teams:
        div_name = TEAM_DIVISIONS.get(t["abbr"], "Other")
        groups.setdefault(div_name, []).append(t)

    divisions = []
    for name in sorted(groups.keys()):  # alphabetical sorts East/North/South/West correctly
        group_teams = groups[name]
        group_teams.sort(key=lambda t: (-(t["win_pct"] or 0), -t["wins"]))
        for i, t in enumerate(group_teams, start=1):
            t["division_rank"] = i
        divisions.append({"name": name, "teams": group_teams})
    return divisions


def fetch_standings(year):
    raw = fetch_json(f"{STANDINGS_URL}?season={year}")
    top_children = raw.get("children") or []
    if not top_children:
        die(f"UNEXPECTED STANDINGS SHAPE: no 'children' key, top-level keys={list(raw.keys())}")

    conferences = []
    for conf in top_children:
        conf_name = conf.get("name") or conf.get("abbreviation") or "Conference"
        entries = []
        div_children = conf.get("children")
        if div_children:
            for div in div_children:
                entries.extend(((div.get("standings") or {}).get("entries")) or [])
        else:
            entries = ((conf.get("standings") or {}).get("entries")) or []

        teams = [parse_team_entry(e) for e in entries]
        conferences.append({"name": conf_name, "divisions": group_by_division(teams)})
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
