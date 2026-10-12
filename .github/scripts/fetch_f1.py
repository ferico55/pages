#!/usr/bin/env python3
"""Fetches the current F1 season calendar + standings from the Jolpica-F1 API
(https://api.jolpi.ca/ergast/f1/) and writes f1-schedule/data/schedule.json
and standings.json.

Jolpica allows roughly four requests per second and also enforces an hourly
cap. This script stays well under that without retrying:

* Reuse finished rounds already stored in schedule.json. A normal daily run
  with no new race makes 3 calls (calendar, driver standings, constructor
  standings). A run with one newly finished round makes 4 (those three, plus
  that round's results).
* When several finished rounds are missing, load the whole season once via
  paged /current/results.json?limit=100 (and sprint.json when the calendar
  has sprints) instead of one results call and one standings call per round.
  points_after is the sum of race points and sprint points up to that round.
* For a single newly finished round, points_after comes from the driver
  standings response this run already fetches (championship points after the
  latest round). That avoids a per-round standings call.
* Sleep REQUEST_PAUSE_SECONDS between calls. There is no retry and no
  backoff loop.

A failed GET leaves both files on disk untouched; the next scheduled run
fills the gap. Cached winners are never replaced with nulls. Each file is
written only after that payload has been built in memory, via a temp file
and os.replace. An empty standings payload skips standings.json (the run
still fails) and still saves a complete schedule.
"""
import copy
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import fetch_json, write_json_atomic  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCHEDULE_OUT = os.path.join(REPO_ROOT, "f1-schedule", "data", "schedule.json")
STANDINGS_OUT = os.path.join(REPO_ROOT, "f1-schedule", "data", "standings.json")

BASE = "https://api.jolpi.ca/ergast/f1"
VERSTAPPEN_CODE = "VER"

# Jolpica's documented page cap. Paging is sequential, not a retry.
PAGE_LIMIT = 100
# A full season is well under this (24 races x ~22 cars = ~528 rows).
# Hitting it means the payload is nonsense; stop rather than hammering.
MAX_PAGES = 15
# Under the ~4 requests/second limit, and only between calls (no backoff).
REQUEST_PAUSE_SECONDS = 0.4


class FetchFailed(Exception):
    """A Jolpica call or payload failed. Callers must not write data files."""


class F1Update:
    def __init__(self, schedule, standings, urls):
        self.schedule = schedule
        self.standings = standings
        self.urls = urls


def warn(msg):
    flat = " ".join(str(msg).split())
    # GitHub Actions surfaces this as an annotation on the run.
    print(f"::warning title=F1 data fetch failed::{flat}")


def is_red_bull(name):
    return bool(name) and "red bull" in name.lower()


def combine_date_time(date_str, time_str):
    if not date_str:
        return None
    if time_str:
        # time_str already ends in "Z" in Ergast/Jolpica responses
        return f"{date_str}T{time_str}"
    return f"{date_str}T00:00:00Z"


def parse_season(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def paged(url, offset):
    return f"{url}?limit={PAGE_LIMIT}&offset={offset}"


def make_getter(fetcher, sleep):
    state = {"urls": []}

    def get(url):
        if state["urls"]:
            sleep(REQUEST_PAUSE_SECONDS)
        state["urls"].append(url)
        try:
            return fetcher(url)
        except FetchFailed:
            raise
        except Exception as e:
            raise FetchFailed(f"GET {url} failed: {e}") from e

    return get, state


def mrdata(raw, url):
    if not isinstance(raw, dict):
        raise FetchFailed(f"UNEXPECTED F1 payload from {url}")
    mr = raw.get("MRData")
    if not isinstance(mr, dict):
        keys = list(raw.keys())
        raise FetchFailed(f"UNEXPECTED F1 payload from {url}: top-level keys={keys}")
    return mr


def total_of(mr, url):
    try:
        return int(mr["total"])
    except (KeyError, TypeError, ValueError):
        raise FetchFailed(f"missing result total from {url}")


def read_optional_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"WARN: ignoring unreadable {path}: {e}", file=sys.stderr)
        return None


def index_existing(doc, season):
    """Map round -> race for the same season only. A new season must not
    reuse last year's winners."""
    if not isinstance(doc, dict) or season is None or parse_season(doc.get("season")) != season:
        return {}
    out = {}
    for race in doc.get("races") or []:
        if not isinstance(race, dict):
            continue
        try:
            out[int(race["round"])] = race
        except (KeyError, TypeError, ValueError):
            continue
    return out


def has_cached_results(entry):
    if not isinstance(entry, dict) or not entry.get("winner"):
        return False
    top10 = entry.get("top10") or []
    if not top10:
        return False
    for row in top10:
        if not isinstance(row, dict):
            return False
        if row.get("driver_code") and row.get("points_after") is None:
            return False
    return True


def copy_cached_results(entry, cached):
    entry["winner"] = cached.get("winner")
    entry["winner_constructor"] = cached.get("winner_constructor")
    entry["verstappen_finish"] = cached.get("verstappen_finish")
    entry["top10"] = copy.deepcopy(cached.get("top10") or [])


def race_started(entry, now):
    race_dt = entry.get("race_date_utc")
    if not race_dt:
        return False
    try:
        race_time = datetime.datetime.fromisoformat(race_dt.replace("Z", "+00:00"))
    except ValueError:
        return False
    return race_time < now


def calendar_entry(race):
    circuit = race.get("Circuit") or {}
    location = circuit.get("Location") or {}
    qual = race.get("Qualifying") or {}
    sprint = race.get("Sprint") or {}
    return {
        "round": int(race["round"]),
        "race_name": race.get("raceName", "Unknown"),
        "circuit": circuit.get("circuitName", ""),
        "locality": location.get("locality", ""),
        "country": location.get("country", ""),
        "race_date_utc": combine_date_time(race.get("date"), race.get("time")),
        "qualifying_date_utc": combine_date_time(qual.get("date"), qual.get("time")),
        "sprint_date_utc": combine_date_time(sprint.get("date"), sprint.get("time")) if sprint else None,
        "status": "scheduled",
        "winner": None,
        "winner_constructor": None,
        "verstappen_finish": None,
        "top10": [],
    }


def build_calendar(races_raw, existing_by_round, now):
    races = []
    for race in races_raw:
        try:
            entry = calendar_entry(race)
        except (KeyError, TypeError, ValueError) as e:
            print(f"WARN: skipping malformed race entry: {e}", file=sys.stderr)
            continue
        if race_started(entry, now):
            entry["status"] = "completed"
            cached = existing_by_round.get(entry["round"])
            if has_cached_results(cached):
                copy_cached_results(entry, cached)
        races.append(entry)
    return races


def result_points(row):
    try:
        return float(row.get("points") or 0)
    except (TypeError, ValueError):
        return 0.0


def driver_code(row):
    return ((row.get("Driver") or {}).get("code")) or ""


def driver_name(driver):
    driver = driver or {}
    return f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip()


def summarize_results(results, round_num):
    if not results:
        return None
    winner = results[0]
    name = driver_name(winner.get("Driver"))
    constructor = (winner.get("Constructor") or {}).get("name")
    if not name:
        return None

    verstappen_finish = None
    for result in results:
        if driver_code(result) == VERSTAPPEN_CODE:
            verstappen_finish = result.get("positionText") or result.get("position")
            break

    top10 = []
    for result in results[:10]:
        try:
            driver = result.get("Driver") or {}
            code = driver.get("code") or ""
            d_constructor = (result.get("Constructor") or {}).get("name")
            top10.append({
                "position": result.get("positionText") or result.get("position"),
                "driver": driver_name(driver),
                "driver_code": code,
                "constructor": "Red Bull" if is_red_bull(d_constructor) else d_constructor,
                "points": result_points(result),
                "points_after": None,
            })
        except (KeyError, TypeError, ValueError) as e:
            print(f"WARN: skipping malformed top10 entry for round {round_num}: {e}", file=sys.stderr)
    return name, constructor, verstappen_finish, top10


def apply_summary(entry, summary, points_after):
    name, constructor, verstappen_finish, top10 = summary
    if points_after is not None:
        for row in top10:
            code = row.get("driver_code")
            row["points_after"] = points_after.get(code) if code else None
    entry["winner"] = name
    entry["winner_constructor"] = "Red Bull" if is_red_bull(constructor) else constructor
    entry["verstappen_finish"] = verstappen_finish
    entry["top10"] = top10


def accumulate_points(race_rows, sprint_rows):
    """Championship points after each round, from race results plus the
    sprint earlier that weekend. Does not call the standings endpoint."""
    cumulative = {}
    after = {}
    for rnd in sorted(set(race_rows) | set(sprint_rows)):
        for row in sprint_rows.get(rnd) or []:
            code = driver_code(row)
            if code:
                cumulative[code] = cumulative.get(code, 0.0) + result_points(row)
        for row in race_rows.get(rnd) or []:
            code = driver_code(row)
            if code:
                cumulative[code] = cumulative.get(code, 0.0) + result_points(row)
        after[rnd] = dict(cumulative)
    return after


def points_after_from_standings(top10, points_map, previous_entry):
    """Use the current driver standings as points-after the latest round.

    Reject the map when a finisher's standing is lower than the points they
    already had plus the points they scored in this race — the standings
    response has not caught up, and caching it would stick until the cache
    was thrown away. Returns None so the round stays uncached and the next
    run tries again. This is not a retry loop.
    """
    previous = {}
    if previous_entry:
        for row in previous_entry.get("top10") or []:
            code = row.get("driver_code")
            if code and row.get("points_after") is not None:
                try:
                    previous[code] = float(row["points_after"])
                except (TypeError, ValueError):
                    pass

    resolved = {}
    for row in top10:
        code = row.get("driver_code") or ""
        if not code:
            continue
        if code not in points_map:
            return None
        got = float(points_map[code])
        race_pts = float(row.get("points") or 0)
        floor = race_pts
        if code in previous:
            floor = max(floor, previous[code] + race_pts)
        if got + 1e-6 < floor:
            return None
        resolved[code] = got
    return resolved or None


def previous_cached(races, round_num):
    prior = [e for e in races if e["round"] < round_num and has_cached_results(e)]
    if not prior:
        return None
    return max(prior, key=lambda e: e["round"])


def fetch_pages(get, url):
    pages = []
    offset = 0
    for _ in range(MAX_PAGES):
        raw = get(paged(url, offset))
        mr = mrdata(raw, url)
        total = total_of(mr, url)
        pages.append((mr, offset, total))
        offset += PAGE_LIMIT
        if offset >= total:
            return pages
    raise FetchFailed(f"paging {url} exceeded {MAX_PAGES} requests; not writing partial data")


def fetch_rows_by_round(get, url, results_key):
    merged = {}
    for mr, offset, total in fetch_pages(get, url):
        races = ((mr.get("RaceTable") or {}).get("Races")) or []
        if not races and offset < total:
            raise FetchFailed(f"empty page at offset {offset} for {url} (total {total})")
        for race in races:
            try:
                rnd = int(race["round"])
            except (KeyError, TypeError, ValueError):
                print(f"WARN: skipping race row without a round from {url}", file=sys.stderr)
                continue
            merged.setdefault(rnd, []).extend(race.get(results_key) or [])
    return merged


def fetch_calendar(get):
    url = f"{BASE}/current.json"
    races = []
    season = None
    for mr, offset, total in fetch_pages(get, url):
        table = mr.get("RaceTable") or {}
        if season is None:
            season = parse_season(table.get("season"))
        page = table.get("Races") or []
        if not page and offset < total:
            raise FetchFailed(f"empty calendar page at offset {offset} (total {total})")
        races.extend(page)
    if not races:
        raise FetchFailed("ZERO races parsed from Jolpica-F1 schedule — refusing to overwrite existing data")
    return races, season


def fetch_standing_rows(get, url, row_key):
    rows = []
    for mr, offset, total in fetch_pages(get, url):
        try:
            lists = mr["StandingsTable"]["StandingsLists"]
        except (KeyError, TypeError):
            keys = list(mr.keys())
            raise FetchFailed(f"UNEXPECTED F1 STANDINGS SHAPE from {url}: MRData keys={keys}")
        if not lists:
            if offset < total:
                raise FetchFailed(f"empty standings page at offset {offset} for {url}")
            return rows
        page_rows = lists[0].get(row_key) or []
        if not page_rows and offset < total:
            raise FetchFailed(f"empty standings page at offset {offset} for {url}")
        rows.extend(page_rows)
    return rows


def parse_driver_standings(rows):
    drivers = []
    points_by_code = {}
    for d in rows:
        try:
            driver = d.get("Driver") or {}
            constructors = d.get("Constructors") or [{}]
            constructor_name = constructors[0].get("name") if constructors else None
            code = driver.get("code") or ""
            points = float(d.get("points") or 0)
            if code:
                points_by_code[code] = points
            drivers.append({
                "position": int(d.get("position") or 0),
                "driver": driver_name(driver),
                "driver_code": code,
                "constructor": constructor_name or "Unknown",
                "points": points,
                "wins": int(d.get("wins") or 0),
                "is_red_bull": is_red_bull(constructor_name),
            })
        except (KeyError, TypeError, ValueError) as e:
            print(f"WARN: skipping malformed driver standing entry: {e}", file=sys.stderr)
    return drivers, points_by_code


def parse_constructor_standings(rows):
    constructors = []
    for c in rows:
        try:
            constructor = c.get("Constructor") or {}
            name = constructor.get("name", "Unknown")
            constructors.append({
                "position": int(c.get("position") or 0),
                "constructor": name,
                "points": float(c.get("points") or 0),
                "wins": int(c.get("wins") or 0),
                "is_red_bull": is_red_bull(name),
            })
        except (KeyError, TypeError, ValueError) as e:
            print(f"WARN: skipping malformed constructor standing entry: {e}", file=sys.stderr)
    return constructors


def missing_completed_rounds(races):
    return [e["round"] for e in races if e["status"] == "completed" and not has_cached_results(e)]


def build_f1_payloads(existing_schedule, fetcher, now, sleep=time.sleep):
    """Build schedule and standings documents. Raises FetchFailed on any
    HTTP or payload error and does not touch the filesystem."""
    get, state = make_getter(fetcher, sleep)
    races_raw, season = fetch_calendar(get)
    existing_by_round = index_existing(existing_schedule, season)
    races = build_calendar(races_raw, existing_by_round, now)
    if not races:
        raise FetchFailed("ZERO races parsed from Jolpica-F1 schedule — refusing to overwrite existing data")

    missing = missing_completed_rounds(races)
    completed = [e["round"] for e in races if e["status"] == "completed"]
    latest = max(completed) if completed else None
    cached_count = sum(1 for e in races if has_cached_results(e))

    if not missing:
        strategy = "cache"
    elif len(missing) == 1 and missing[0] == latest:
        strategy = "single"
    else:
        strategy = "paged"
    print(f"F1 plan: {strategy}; cached_results={cached_count}; missing_rounds={missing}")

    single_rows = {}
    if strategy == "single":
        rnd = missing[0]
        single_rows = fetch_rows_by_round(get, f"{BASE}/current/{rnd}/results.json", "Results")
    elif strategy == "paged":
        race_rows = fetch_rows_by_round(get, f"{BASE}/current/results.json", "Results")
        if any(e.get("sprint_date_utc") for e in races):
            sprint_rows = fetch_rows_by_round(get, f"{BASE}/current/sprint.json", "SprintResults")
        else:
            sprint_rows = {}
        points_after = accumulate_points(race_rows, sprint_rows)
        missing_set = set(missing)
        for entry in races:
            if entry["round"] not in missing_set:
                continue
            summary = summarize_results(race_rows.get(entry["round"]) or [], entry["round"])
            if not summary:
                continue
            apply_summary(entry, summary, points_after.get(entry["round"]) or {})

    driver_rows = fetch_standing_rows(
        get, f"{BASE}/current/driverStandings.json", "DriverStandings"
    )
    drivers, points_map = parse_driver_standings(driver_rows)

    if strategy == "single":
        rnd = missing[0]
        entry = next(e for e in races if e["round"] == rnd)
        summary = summarize_results(single_rows.get(rnd) or [], rnd)
        if summary:
            resolved = points_after_from_standings(summary[3], points_map, previous_cached(races, rnd))
            if resolved is None:
                print(
                    f"WARN: driver standings do not yet include round {rnd}; "
                    "leaving points_after unset so this round is fetched again next run",
                    file=sys.stderr,
                )
            apply_summary(entry, summary, resolved)

    constructor_rows = fetch_standing_rows(
        get, f"{BASE}/current/constructorStandings.json", "ConstructorStandings"
    )
    constructors = parse_constructor_standings(constructor_rows)

    updated = now.isoformat()
    schedule_doc = {"season": season, "last_updated": updated, "races": races}
    if len(drivers) == 0 and len(constructors) == 0:
        print(
            "WARN: zero standings entries — refusing to overwrite existing standings",
            file=sys.stderr,
        )
        standings_doc = None
    else:
        standings_doc = {
            "season": season,
            "last_updated": updated,
            "drivers": drivers,
            "constructors": constructors,
        }
    print(f"F1 requests: {len(state['urls'])}")
    for url in state["urls"]:
        print(f"F1 GET {url}")
    return F1Update(schedule_doc, standings_doc, state["urls"])


def main(fetcher=None, sleep=time.sleep, now=None, schedule_path=SCHEDULE_OUT, standings_path=STANDINGS_OUT):
    fetcher = fetcher or fetch_json
    now = now or datetime.datetime.now(datetime.timezone.utc)
    existing = read_optional_json(schedule_path)
    try:
        update = build_f1_payloads(existing, fetcher, now, sleep=sleep)
    except FetchFailed as e:
        warn(str(e))
        print(f"F1 fetch failed; existing data was left unchanged. {e}", file=sys.stderr)
        return 1

    # FetchFailed returned above, before any write. Empty standings skip that file only.
    write_json_atomic(schedule_path, update.schedule)
    print(f"Wrote {len(update.schedule['races'])} F1 races to {schedule_path}")
    if update.standings is None:
        warn("ZERO standings entries parsed from Jolpica-F1 — refusing to overwrite existing standings data")
        return 1
    write_json_atomic(standings_path, update.standings)
    print(
        f"Wrote F1 standings ({len(update.standings['drivers'])} drivers, "
        f"{len(update.standings['constructors'])} constructors) to {standings_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
