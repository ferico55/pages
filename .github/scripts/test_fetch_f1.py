#!/usr/bin/env python3
"""Unit tests for the Jolpica fetch. No network. Run from the repo root:

    python3 .github/scripts/test_fetch_f1.py
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_f1  # noqa: E402
from _util import fetch_json  # noqa: E402

NOW = fetch_f1.datetime.datetime(2026, 10, 12, 12, 0, tzinfo=fetch_f1.datetime.timezone.utc)
PAST = "2026-03-08"
PAST_2 = "2026-09-06"
FUTURE = "2026-12-01"


def read_text(path):
    with open(path) as f:
        return f.read()


def offset_of(url):
    return int(parse_qs(urlparse(url).query).get("offset", ["0"])[0])


def race_shell(rnd, name, date, sprint=False):
    race = {
        "round": str(rnd),
        "raceName": name,
        "Circuit": {
            "circuitName": "Circuit",
            "Location": {"locality": "City", "country": "Country"},
        },
        "date": date,
        "time": "12:00:00Z",
        "Qualifying": {"date": date, "time": "08:00:00Z"},
    }
    if sprint:
        race["Sprint"] = {"date": date, "time": "04:00:00Z"}
    return race


def calendar_response(races, season="2026", total=None):
    return {
        "MRData": {
            "total": str(len(races) if total is None else total),
            "RaceTable": {"season": str(season), "Races": races},
        }
    }


def results_page(races, total):
    return {"MRData": {"total": str(total), "RaceTable": {"season": "2026", "Races": races}}}


def result_row(code, points, position, given, family, constructor="Mercedes", position_text=None):
    return {
        "position": str(position),
        "positionText": position_text if position_text is not None else str(position),
        "points": str(points),
        "Driver": {"givenName": given, "familyName": family, "code": code},
        "Constructor": {"name": constructor},
    }


def driver_row(code, points, position="1", given="A", family="Driver", constructor="Mercedes", wins="1"):
    return {
        "position": str(position),
        "points": str(points),
        "wins": str(wins),
        "Driver": {"givenName": given, "familyName": family, "code": code},
        "Constructors": [{"name": constructor}],
    }


def driver_standings(rows, total=None):
    return {
        "MRData": {
            "total": str(len(rows) if total is None else total),
            "StandingsTable": {"StandingsLists": [{"DriverStandings": rows}]},
        }
    }


def constructor_standings(rows=None, total=None):
    rows = rows if rows is not None else [{
        "position": "1",
        "points": "25",
        "wins": "1",
        "Constructor": {"name": "Mercedes"},
    }]
    return {
        "MRData": {
            "total": str(len(rows) if total is None else total),
            "StandingsTable": {"StandingsLists": [{"ConstructorStandings": rows}]},
        }
    }


def cached_round(rnd, winner="George Russell", code="RUS", points=25.0, points_after=25.0,
                 constructor="Mercedes", verstappen_finish="6"):
    return {
        "round": rnd,
        "race_name": "Old Name",
        "status": "completed",
        "winner": winner,
        "winner_constructor": constructor,
        "verstappen_finish": verstappen_finish,
        "top10": [{
            "position": "1",
            "driver": winner,
            "driver_code": code,
            "constructor": constructor,
            "points": points,
            "points_after": points_after,
        }],
    }


class Router:
    def __init__(self):
        self.calls = []
        self.fail_if = None
        self.calendar = calendar_response([])
        self.drivers = driver_standings([driver_row("RUS", 25, given="George", family="Russell")])
        self.constructors = constructor_standings()
        self.season_results = {}  # offset -> response
        self.sprint_results = {}
        self.round_results = {}  # round -> response

    def __call__(self, url):
        self.calls.append(url)
        if self.fail_if and self.fail_if(url):
            raise RuntimeError(f"HTTP 429 for {url}")
        if "/current.json?" in url:
            return self.calendar
        if "driverStandings.json" in url:
            return self.drivers
        if "constructorStandings.json" in url:
            return self.constructors
        if "/sprint.json?" in url:
            return self.sprint_results[offset_of(url)]
        if "/current/results.json?" in url:
            return self.season_results[offset_of(url)]
        for rnd, payload in self.round_results.items():
            if f"/current/{rnd}/results.json?" in url:
                return payload
        raise AssertionError(f"unexpected URL {url}")


class FetchF1Tests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.router = Router()

    def build(self, existing=None):
        return fetch_f1.build_f1_payloads(
            existing, self.router, NOW, sleep=self.sleeps.append
        )

    def test_cached_rounds_skip_result_calls_and_refresh_calendar_fields(self):
        self.router.calendar = calendar_response([
            race_shell(1, "Australian Grand Prix", PAST),
            race_shell(2, "United States Grand Prix", FUTURE),
        ])
        existing = {"season": 2026, "races": [cached_round(1)]}
        update = self.build(existing)

        self.assertEqual(len(self.router.calls), 3)
        self.assertTrue(all("results.json" not in url for url in self.router.calls))
        self.assertEqual(self.sleeps, [fetch_f1.REQUEST_PAUSE_SECONDS, fetch_f1.REQUEST_PAUSE_SECONDS])
        race = update.schedule["races"][0]
        self.assertEqual(race["race_name"], "Australian Grand Prix")
        self.assertEqual(race["winner"], "George Russell")
        self.assertEqual(race["top10"][0]["points_after"], 25.0)
        self.assertEqual(update.schedule["races"][1]["status"], "scheduled")
        self.assertIsNone(update.schedule["races"][1]["winner"])

    def test_single_new_round_uses_standings_not_a_paged_dump(self):
        self.router.calendar = calendar_response([
            race_shell(1, "Australian Grand Prix", PAST),
            race_shell(2, "Chinese Grand Prix", PAST_2, sprint=True),
        ])
        self.router.round_results[2] = results_page([{
            "round": "2",
            "Results": [
                result_row("VER", 25, 1, "Max", "Verstappen", constructor="Oracle Red Bull Racing"),
                result_row("RUS", 18, 2, "George", "Russell", position_text="2"),
            ],
        }], total=2)
        self.router.drivers = driver_standings([
            driver_row("VER", 51, position="1", given="Max", family="Verstappen", constructor="Red Bull"),
            driver_row("RUS", 43, position="2", given="George", family="Russell"),
        ], total=1)
        existing = {"season": 2026, "races": [{
            "round": 1,
            "winner": "George Russell",
            "winner_constructor": "Mercedes",
            "verstappen_finish": "2",
            "top10": [
                {"position": "1", "driver": "George Russell", "driver_code": "RUS",
                 "constructor": "Mercedes", "points": 25.0, "points_after": 25.0},
                {"position": "2", "driver": "Max Verstappen", "driver_code": "VER",
                 "constructor": "Red Bull", "points": 18.0, "points_after": 18.0},
            ],
        }]}

        update = self.build(existing)
        urls = self.router.calls
        self.assertEqual(len(urls), 4)
        self.assertTrue(any("/current/2/results.json?" in url for url in urls))
        self.assertFalse(any(url.endswith("/current/results.json") or "/current/results.json?" in url for url in urls))
        self.assertFalse(any("sprint.json" in url for url in urls))
        self.assertFalse(any("driverStandings" in url and "/2/" in url for url in urls))

        race = update.schedule["races"][1]
        self.assertEqual(race["winner"], "Max Verstappen")
        self.assertEqual(race["winner_constructor"], "Red Bull")
        self.assertEqual(race["top10"][0]["constructor"], "Red Bull")
        self.assertEqual(race["top10"][0]["points"], 25.0)
        # Standings include the sprint; we must not store race-only 18+25.
        self.assertEqual(race["top10"][0]["points_after"], 51.0)
        self.assertEqual(race["top10"][1]["points_after"], 43.0)
        self.assertEqual(update.schedule["races"][0]["winner"], "George Russell")

    def test_stale_standings_do_not_cache_points_after(self):
        self.router.calendar = calendar_response([
            race_shell(1, "Australian Grand Prix", PAST),
            race_shell(2, "Chinese Grand Prix", PAST_2),
        ])
        self.router.round_results[2] = results_page([{
            "round": "2",
            "Results": [result_row("VER", 25, 1, "Max", "Verstappen")],
        }], total=1)
        # Unchanged from before this race: 18 < 18 + 25.
        self.router.drivers = driver_standings([
            driver_row("VER", 18, given="Max", family="Verstappen"),
        ])
        existing = {"season": 2026, "races": [
            cached_round(1, winner="Max Verstappen", code="VER", points=18, points_after=18),
        ]}

        with tempfile.TemporaryDirectory() as tmp:
            schedule = os.path.join(tmp, "schedule.json")
            standings = os.path.join(tmp, "standings.json")
            with open(schedule, "w") as f:
                json.dump(existing, f)
            code = fetch_f1.main(
                fetcher=self.router, sleep=self.sleeps.append, now=NOW,
                schedule_path=schedule, standings_path=standings,
            )
            self.assertEqual(code, 0)
            with open(schedule) as f:
                saved = json.load(f)
            self.assertEqual(saved["races"][1]["winner"], "Max Verstappen")
            self.assertIsNone(saved["races"][1]["top10"][0]["points_after"])
            self.assertFalse(fetch_f1.has_cached_results(saved["races"][1]))

            self.router.calls.clear()
            again = fetch_f1.build_f1_payloads(saved, self.router, NOW, sleep=lambda _s: None)
            self.assertTrue(any("/current/2/results.json?" in url for url in again.urls))
            self.assertEqual(again.schedule["races"][0]["winner"], "Max Verstappen")

    def test_paged_results_merge_split_races_and_add_sprint_points(self):
        self.router.calendar = calendar_response([
            race_shell(1, "Australian Grand Prix", PAST),
            race_shell(2, "Chinese Grand Prix", PAST_2, sprint=True),
        ])
        # total > PAGE_LIMIT forces a second page. The same round is split
        # the way Jolpica splits a results page mid-race.
        self.router.season_results[0] = results_page([{
            "round": "1",
            "Results": [
                result_row("RUS", 25, 1, "George", "Russell"),
                result_row("VER", 18, 2, "Max", "Verstappen"),
            ],
        }], total=150)
        self.router.season_results[100] = results_page([{
            "round": "1",
            "Results": [result_row("NOR", 0, 11, "Lando", "Norris")],
        }, {
            "round": "2",
            "Results": [
                result_row("VER", 25, 1, "Max", "Verstappen"),
                result_row("RUS", 18, 2, "George", "Russell"),
            ],
        }], total=150)
        self.router.sprint_results[0] = results_page([{
            "round": "2",
            "SprintResults": [result_row("VER", 8, 1, "Max", "Verstappen")],
        }], total=1)
        self.router.drivers = driver_standings([
            driver_row("VER", 51, given="Max", family="Verstappen"),
            driver_row("RUS", 43, given="George", family="Russell"),
        ])

        update = self.build(None)
        urls = self.router.calls
        self.assertTrue(any("/current/results.json?" in url and "offset=0" in url for url in urls))
        self.assertTrue(any("/current/results.json?" in url and "offset=100" in url for url in urls))
        self.assertTrue(any("/sprint.json?" in url for url in urls))
        self.assertFalse(any("/current/1/results.json" in url or "/current/2/results.json" in url for url in urls))

        by_round = {r["round"]: r for r in update.schedule["races"]}
        self.assertEqual([row["driver_code"] for row in by_round[1]["top10"][:2]], ["RUS", "VER"])
        self.assertEqual(by_round[1]["top10"][0]["points_after"], 25.0)
        self.assertEqual(by_round[1]["top10"][1]["points_after"], 18.0)
        # 18 race + 8 sprint from round 2, on top of round 1's 18.
        self.assertEqual(by_round[2]["winner"], "Max Verstappen")
        self.assertEqual(by_round[2]["top10"][0]["points"], 25.0)
        self.assertEqual(by_round[2]["top10"][0]["points_after"], 51.0)
        self.assertEqual(by_round[2]["top10"][1]["points_after"], 43.0)

    def test_gap_uses_paged_results_and_keeps_cached_winner(self):
        self.router.calendar = calendar_response([
            race_shell(1, "Australian Grand Prix", PAST),
            race_shell(2, "Chinese Grand Prix", PAST_2),
        ])
        self.router.season_results[0] = results_page([{
            "round": "1",
            "Results": [result_row("NOR", 25, 1, "Lando", "Norris", constructor="McLaren")],
        }], total=1)
        existing = {"season": 2026, "races": [
            cached_round(2, winner="Keep Me", code="VER", points=25, points_after=40),
        ]}
        update = self.build(existing)
        urls = " ".join(self.router.calls)
        self.assertIn("/current/results.json?", urls)
        self.assertNotIn("/current/1/results.json", urls)
        self.assertNotIn("sprint.json", urls)
        by_round = {r["round"]: r for r in update.schedule["races"]}
        self.assertEqual(by_round[1]["winner"], "Lando Norris")
        self.assertEqual(by_round[1]["top10"][0]["points_after"], 25.0)
        self.assertEqual(by_round[2]["winner"], "Keep Me")
        self.assertEqual(by_round[2]["top10"][0]["points_after"], 40)

    def test_paged_payload_missing_a_round_does_not_null_cached_results(self):
        self.router.calendar = calendar_response([
            race_shell(1, "Australian Grand Prix", PAST),
            race_shell(2, "Chinese Grand Prix", PAST_2),
            race_shell(3, "Italian Grand Prix", "2026-10-01"),
        ])
        self.router.season_results[0] = results_page([{
            "round": "3",
            "Results": [result_row("HAM", 25, 1, "Lewis", "Hamilton", constructor="Ferrari")],
        }], total=1)
        existing = {"season": 2026, "races": [cached_round(1, winner="Keep Me")]}
        update = self.build(existing)
        by_round = {r["round"]: r for r in update.schedule["races"]}
        self.assertEqual(by_round[1]["winner"], "Keep Me")
        self.assertEqual(by_round[1]["top10"][0]["points_after"], 25.0)
        self.assertIsNone(by_round[2]["winner"])
        self.assertEqual(by_round[2]["top10"], [])
        self.assertEqual(by_round[3]["winner"], "Lewis Hamilton")

    def test_new_season_does_not_reuse_last_years_winners(self):
        self.router.calendar = calendar_response([
            race_shell(1, "Australian Grand Prix", PAST),
        ], season=2026)
        self.router.round_results[1] = results_page([{
            "round": "1",
            "Results": [result_row("NOR", 25, 1, "Lando", "Norris")],
        }], total=1)
        self.router.drivers = driver_standings([
            driver_row("NOR", 25, given="Lando", family="Norris"),
        ])
        existing = {"season": 2025, "races": [cached_round(1, winner="Old Champion")]}
        update = self.build(existing)
        self.assertEqual(update.schedule["races"][0]["winner"], "Lando Norris")
        self.assertNotEqual(update.schedule["races"][0]["winner"], "Old Champion")

    def test_http_429_mid_paging_leaves_files_untouched_and_stops(self):
        self.router.calendar = calendar_response([
            race_shell(1, "Australian Grand Prix", PAST),
            race_shell(2, "Chinese Grand Prix", PAST_2),
            race_shell(3, "Italian Grand Prix", "2026-10-01"),
        ])
        self.router.season_results[0] = results_page([{
            "round": "2",
            "Results": [result_row("NOR", 25, 1, "Lando", "Norris")],
        }], total=150)
        self.router.fail_if = lambda url: "results.json" in url and "offset=100" in url

        with tempfile.TemporaryDirectory() as tmp:
            schedule = os.path.join(tmp, "schedule.json")
            standings = os.path.join(tmp, "standings.json")
            original_schedule = json.dumps({"season": 2026, "races": [cached_round(1, winner="Keep Me")]})
            original_standings = json.dumps({"season": 2026, "drivers": [{"driver": "Keep"}], "constructors": []})
            with open(schedule, "w") as f:
                f.write(original_schedule)
            with open(standings, "w") as f:
                f.write(original_standings)

            def fetching(url):
                self.assertEqual(read_text(schedule), original_schedule)
                self.assertEqual(read_text(standings), original_standings)
                return self.router(url)

            code = fetch_f1.main(
                fetcher=fetching, sleep=self.sleeps.append, now=NOW,
                schedule_path=schedule, standings_path=standings,
            )
            self.assertEqual(code, 1)
            self.assertEqual(read_text(schedule), original_schedule)
            self.assertEqual(read_text(standings), original_standings)
            self.assertFalse(os.path.exists(schedule + ".tmp"))
            joined = " ".join(self.router.calls)
            self.assertNotIn("driverStandings", joined)
            self.assertNotIn("constructorStandings", joined)
            # Pause between the calendar call and each results page, then stop.
            # Not a backoff loop: the failing call is not repeated.
            self.assertEqual(self.sleeps, [fetch_f1.REQUEST_PAUSE_SECONDS, fetch_f1.REQUEST_PAUSE_SECONDS])
            self.assertEqual(len(self.router.calls), 3)

    def test_first_request_failure_writes_nothing(self):
        self.router.fail_if = lambda url: "/current.json?" in url
        with tempfile.TemporaryDirectory() as tmp:
            schedule = os.path.join(tmp, "schedule.json")
            standings = os.path.join(tmp, "standings.json")
            with open(schedule, "w") as f:
                f.write('{"season": 2026, "races": []}\n')
            before = read_text(schedule)
            code = fetch_f1.main(
                fetcher=self.router, sleep=self.sleeps.append, now=NOW,
                schedule_path=schedule, standings_path=standings,
            )
            self.assertEqual(code, 1)
            self.assertEqual(read_text(schedule), before)
            self.assertFalse(os.path.exists(standings))
            self.assertEqual(self.sleeps, [])
            self.assertEqual(len(self.router.calls), 1)

    def test_empty_standings_skip_that_file_only(self):
        self.router.calendar = calendar_response([race_shell(1, "Australian Grand Prix", FUTURE)])
        self.router.drivers = driver_standings([], total=0)
        self.router.drivers["MRData"]["StandingsTable"]["StandingsLists"] = []
        self.router.constructors = constructor_standings([], total=0)
        self.router.constructors["MRData"]["StandingsTable"]["StandingsLists"] = []
        with tempfile.TemporaryDirectory() as tmp:
            schedule = os.path.join(tmp, "schedule.json")
            standings = os.path.join(tmp, "standings.json")
            with open(standings, "w") as f:
                f.write('{"drivers": [{"driver": "keep"}]}\n')
            before = read_text(standings)
            code = fetch_f1.main(
                fetcher=self.router, sleep=self.sleeps.append, now=NOW,
                schedule_path=schedule, standings_path=standings,
            )
            self.assertEqual(code, 1)
            with open(schedule) as f:
                saved = json.load(f)
            self.assertEqual(saved["races"][0]["race_name"], "Australian Grand Prix")
            self.assertEqual(read_text(standings), before)

    def test_fetch_json_429_is_a_single_attempt(self):
        calls = {"n": 0}

        def boom(req, timeout=15):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"slow down")
            )

        with mock.patch("_util.urllib.request.urlopen", boom):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_json("https://api.jolpi.ca/ergast/f1/current.json")
        self.assertEqual(calls["n"], 1)
        self.assertIn("HTTP 429", str(ctx.exception))
        self.assertIn("slow down", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
