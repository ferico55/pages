"""Shared helpers for the nightly NFL/F1 data-fetch scripts. Stdlib only."""
import json
import os
import urllib.request
import urllib.error

USER_AGENT = "nfl-f1-schedules-bot/1.0 (+https://github.com/ferico55/pages)"


def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def write_json_atomic(path, data):
    """Write JSON to `path` via a temp file + os.replace so a crash mid-write
    never leaves a corrupt/partial file, and a failed run never touches the
    previously committed good data unless this is actually called."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def die(msg):
    import sys
    print(msg, file=sys.stderr)
    sys.exit(1)
