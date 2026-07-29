#!/usr/bin/env python3
"""Fetches the current F1 season calendar + standings from the Jolpica-F1 API
(https://api.jolpi.ca/ergast/f1/), a free, keyless, community-maintained
drop-in replacement for the retired Ergast API. Writes
nfl-f1-schedules/data/f1-schedule.json and f1-standings.json.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import fetch_json, write_json_atomic, die  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCHEDULE_OUT = os.path.join(REPO_ROOT, "nfl-f1-schedules", "data", "f1-schedule.json")
STANDINGS_OUT = os.path.join(REPO_ROOT, "nfl-f1-schedules", "data", "f1-standings.json")

BASE = "https://api.jolpi.ca/ergast/f1"


def is_red_bull(name):
    return bool(name) and "red bull" in name.lower()


def combine_date_time(date_str, time_str):
    if not date_str:
        return None
    if time_str:
        # time_str already ends in "Z" in Ergast/Jolpica responses
        return f"{date_str}T{time_str}"
    return f"{date_str}T00:00:00Z"


def fetch_winner(round_num):
    """Best-effort: only used for races whose date has already passed.
    Any failure here just leaves winner fields null; it must never abort
    the overall run."""
    try:
        raw = fetch_json(f"{BASE}/current/{round_num}/results.json")
        races = raw["MRData"]["RaceTable"]["Races"]
        if not races:
            return None, None
        results = races[0].get("Results") or []
        if not results:
            return None, None
        winner = results[0]
        driver = winner.get("Driver", {})
        name = f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip()
        constructor = winner.get("Constructor", {}).get("name")
        return name or None, constructor
    except Exception as e:
        print(f"WARN: couldn't fetch winner for round {round_num}: {e}", file=sys.stderr)
        return None, None


def fetch_schedule():
    raw = fetch_json(f"{BASE}/current.json")
    try:
        races_raw = raw["MRData"]["RaceTable"]["Races"]
    except (KeyError, TypeError):
        die(f"UNEXPECTED F1 SCHEDULE SHAPE: top-level keys={list(raw.keys())}")

    if not races_raw:
        die("ZERO races parsed from Jolpica-F1 schedule — refusing to overwrite existing data")

    season = raw.get("MRData", {}).get("RaceTable", {}).get("season")
    try:
        season = int(season)
    except (TypeError, ValueError):
        season = None

    now = datetime.datetime.now(datetime.timezone.utc)
    races = []
    for r in races_raw:
        try:
            circuit = r.get("Circuit", {})
            location = circuit.get("Location", {})
            race_dt = combine_date_time(r.get("date"), r.get("time"))
            qual = r.get("Qualifying", {})
            sprint = r.get("Sprint", {})

            entry = {
                "round": int(r["round"]),
                "race_name": r.get("raceName", "Unknown"),
                "circuit": circuit.get("circuitName", ""),
                "locality": location.get("locality", ""),
                "country": location.get("country", ""),
                "race_date_utc": race_dt,
                "qualifying_date_utc": combine_date_time(qual.get("date"), qual.get("time")),
                "sprint_date_utc": combine_date_time(sprint.get("date"), sprint.get("time")) if sprint else None,
                "status": "scheduled",
                "winner": None,
                "winner_constructor": None,
            }

            if race_dt:
                try:
                    race_time = datetime.datetime.fromisoformat(race_dt.replace("Z", "+00:00"))
                except ValueError:
                    race_time = None
                if race_time and race_time < now:
                    entry["status"] = "completed"
                    winner, constructor = fetch_winner(entry["round"])
                    entry["winner"] = winner
                    entry["winner_constructor"] = "Red Bull" if is_red_bull(constructor) else constructor

            races.append(entry)
        except (KeyError, TypeError, ValueError) as e:
            print(f"WARN: skipping malformed race entry: {e}", file=sys.stderr)

    return races, season


def fetch_driver_standings():
    raw = fetch_json(f"{BASE}/current/driverStandings.json")
    try:
        standings_lists = raw["MRData"]["StandingsTable"]["StandingsLists"]
    except (KeyError, TypeError):
        die(f"UNEXPECTED F1 DRIVER STANDINGS SHAPE: top-level keys={list(raw.keys())}")

    if not standings_lists:
        return []

    drivers = []
    for d in standings_lists[0].get("DriverStandings", []):
        try:
            driver = d.get("Driver", {})
            constructors = d.get("Constructors", [{}])
            constructor_name = constructors[0].get("name") if constructors else None
            drivers.append({
                "position": int(d.get("position", 0) or 0),
                "driver": f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip(),
                "driver_code": driver.get("code", ""),
                "constructor": constructor_name or "Unknown",
                "points": float(d.get("points", 0) or 0),
                "wins": int(d.get("wins", 0) or 0),
                "is_red_bull": is_red_bull(constructor_name),
            })
        except (KeyError, TypeError, ValueError) as e:
            print(f"WARN: skipping malformed driver standing entry: {e}", file=sys.stderr)
    return drivers


def fetch_constructor_standings():
    raw = fetch_json(f"{BASE}/current/constructorStandings.json")
    try:
        standings_lists = raw["MRData"]["StandingsTable"]["StandingsLists"]
    except (KeyError, TypeError):
        die(f"UNEXPECTED F1 CONSTRUCTOR STANDINGS SHAPE: top-level keys={list(raw.keys())}")

    if not standings_lists:
        return []

    constructors = []
    for c in standings_lists[0].get("ConstructorStandings", []):
        try:
            constructor = c.get("Constructor", {})
            name = constructor.get("name", "Unknown")
            constructors.append({
                "position": int(c.get("position", 0) or 0),
                "constructor": name,
                "points": float(c.get("points", 0) or 0),
                "wins": int(c.get("wins", 0) or 0),
                "is_red_bull": is_red_bull(name),
            })
        except (KeyError, TypeError, ValueError) as e:
            print(f"WARN: skipping malformed constructor standing entry: {e}", file=sys.stderr)
    return constructors


def main():
    races, season = fetch_schedule()

    write_json_atomic(SCHEDULE_OUT, {
        "season": season,
        "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "races": races,
    })
    print(f"Wrote {len(races)} F1 races to {SCHEDULE_OUT}")

    drivers = fetch_driver_standings()
    constructors = fetch_constructor_standings()
    if len(drivers) == 0 and len(constructors) == 0:
        die("ZERO standings entries parsed from Jolpica-F1 — refusing to overwrite existing standings data")

    write_json_atomic(STANDINGS_OUT, {
        "season": season,
        "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "drivers": drivers,
        "constructors": constructors,
    })
    print(f"Wrote F1 standings ({len(drivers)} drivers, {len(constructors)} constructors) to {STANDINGS_OUT}")


if __name__ == "__main__":
    main()
