"""Shared helpers for the nightly NFL/F1 data-fetch scripts. Stdlib only."""
import json
import os
import urllib.request
import urllib.error

USER_AGENT = "nfl-f1-schedules-bot/1.0 (+https://github.com/ferico55/pages)"


def fetch_json(url, timeout=15):
    """GET `url` once and parse the body as JSON.

    HTTP errors, including 429, propagate immediately. Callers must not retry
    or back off: a failed run should exit so the next scheduled run can try
    again, instead of burning Actions minutes inside one job.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:200].strip()
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"HTTP {e.code} for {url}{suffix}") from e


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
