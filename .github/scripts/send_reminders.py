#!/usr/bin/env python3
"""Emails a reminder when an Eagles game or F1 race (GP/Sprint only, no
FP/Qualifying) is coming up soon. Reads the schedule data already written by
fetch_nfl.py / fetch_f1.py; sends nothing of its own from the network besides
the email itself.

Two reminder tiers per event, each fired at most once:
  - "3day": first run where the event is <=72h away
  - "24h":  first run where the event is <=24h away
A dedup state file (state/reminders_sent.json) tracks which (event, tier)
pairs have already been emailed so re-running nightly doesn't re-send. If
several tiers/events are newly due on the same run, they're combined into a
single email.
"""
import argparse
import datetime
import json
import os
import smtplib
import sys
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import write_json_atomic, die  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NFL_SCHEDULE = os.path.join(REPO_ROOT, "nfl-schedule", "data", "schedule.json")
F1_SCHEDULE = os.path.join(REPO_ROOT, "f1-schedule", "data", "schedule.json")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "reminders_sent.json")

TIERS = (
    ("3day", 72),
    ("24h", 24),
)

SPORT_ICONS = {"nfl": "🏈", "f1": "🏁"}

# Reminder emails show local times in GMT+7 (no DST) rather than raw UTC.
GMT7 = datetime.timezone(datetime.timedelta(hours=7))


def parse_utc(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_json(path):
    if not os.path.exists(path):
        print(f"WARN: {path} not found, skipping", file=sys.stderr)
        return None
    with open(path) as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARN: couldn't read state file, starting fresh: {e}", file=sys.stderr)
        return {}


def collect_candidates(now):
    """Returns a list of {sport, kind, key, label, when_utc} for every future
    Eagles game / F1 GP / F1 Sprint, regardless of tier eligibility."""
    candidates = []

    nfl = load_json(NFL_SCHEDULE)
    if nfl:
        for g in nfl.get("games", []):
            if not g.get("is_favorite"):
                continue
            when = parse_utc(g.get("date_utc"))
            if not when or when <= now:
                continue
            candidates.append({
                "sport": "nfl",
                "kind": "game",
                "key": str(g["id"]),
                "label": f"{g.get('away_team', 'Unknown')} @ {g.get('home_team', 'Unknown')}",
                "when_utc": when,
            })

    f1 = load_json(F1_SCHEDULE)
    if f1:
        for r in f1.get("races", []):
            round_num = r.get("round")
            race_name = r.get("race_name", "Unknown")

            race_when = parse_utc(r.get("race_date_utc"))
            if race_when and race_when > now:
                candidates.append({
                    "sport": "f1",
                    "kind": "gp",
                    "key": f"{round_num}-gp",
                    "label": race_name,
                    "when_utc": race_when,
                })

            sprint_when = parse_utc(r.get("sprint_date_utc"))
            if sprint_when and sprint_when > now:
                candidates.append({
                    "sport": "f1",
                    "kind": "sprint",
                    "key": f"{round_num}-sprint",
                    "label": f"{race_name} Sprint",
                    "when_utc": sprint_when,
                })

    return candidates


def format_countdown(now, when):
    delta = when - now
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"in {int(delta.total_seconds() // 60)} min"
    if hours < 48:
        return f"in {round(hours)} hours"
    return f"in {round(hours / 24)} days"


def build_email_body(now, new_reminders):
    # An event can have more than one newly-eligible tier in the same run
    # (e.g. the job skipped a day and it's already inside 24h); it should
    # still only show up once in the email even though both tiers get
    # marked "sent" in state.
    by_key = {}
    for r in new_reminders:
        by_key.setdefault(r["key"], r)

    lines = ["Heads up! Here's what's coming up:", ""]
    for r in sorted(by_key.values(), key=lambda x: x["when_utc"]):
        icon = SPORT_ICONS.get(r["sport"], "")
        when_str = r["when_utc"].astimezone(GMT7).strftime("%a %b %-d, %Y %H:%M GMT+7")
        lines.append(f"{icon} {r['label']} — {format_countdown(now, r['when_utc'])} ({when_str})")
    lines.append("")
    return "\n".join(lines)


def build_email(now, new_reminders):
    subject_parts = []
    if any(r["sport"] == "nfl" for r in new_reminders):
        subject_parts.append("Eagles game")
    if any(r["sport"] == "f1" for r in new_reminders):
        subject_parts.append("F1 race")
    subject = "Reminder: upcoming " + " & ".join(subject_parts)
    body = build_email_body(now, new_reminders)
    return subject, body


def send_email(subject, body, to_addr, from_addr, app_password):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(from_addr, app_password)
        smtp.sendmail(from_addr, [to_addr], msg.as_string())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would be sent instead of emailing / writing state")
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = collect_candidates(now)
    state = load_state()

    new_reminders = []
    for c in candidates:
        hours_until = (c["when_utc"] - now).total_seconds() / 3600
        entry = state.setdefault(c["key"], {})
        for tier, max_hours in TIERS:
            if hours_until <= max_hours and not entry.get(tier):
                new_reminders.append({**c, "tier": tier})

    # Prune state entries for events no longer among today's future
    # candidates (they've either happened or dropped off the schedule).
    live_keys = {c["key"] for c in candidates}
    state = {k: v for k, v in state.items() if k in live_keys}

    if args.dry_run:
        if not new_reminders:
            print("No new reminders due.")
            return
        subject, body = build_email(now, new_reminders)
        print(f"Subject: {subject}\n")
        print(body)
        print(f"({len(new_reminders)} new reminder(s), dry run — not sent, state not updated)")
        return

    # Persist pruning (and create the state file on first-ever run) even if
    # there's nothing new to send, so the workflow's `git add` of this path
    # never hits a missing file, and every later exit path just adds to it.
    write_json_atomic(STATE_PATH, state)

    if not new_reminders:
        print("No new reminders due.")
        return

    subject, body = build_email(now, new_reminders)

    to_addr = os.environ.get("REMINDER_EMAIL_TO")
    from_addr = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not (to_addr and from_addr and app_password):
        die("Missing REMINDER_EMAIL_TO / GMAIL_ADDRESS / GMAIL_APP_PASSWORD env vars — can't send reminder email")

    try:
        send_email(subject, body, to_addr, from_addr, app_password)
    except Exception as e:
        die(f"FAILED to send reminder email: {e}")

    for r in new_reminders:
        state.setdefault(r["key"], {})[r["tier"]] = True
    write_json_atomic(STATE_PATH, state)
    print(f"Sent reminder email with {len(new_reminders)} item(s).")


if __name__ == "__main__":
    main()
