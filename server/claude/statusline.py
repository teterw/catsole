"""Claude Code status line: passes the plan's usage limits to catsole, and
prints them for the bottom of the terminal.

Claude Code runs this with a JSON on stdin after each reply. It must stay
fast and quiet: a catsole that isn't running costs a refused connection,
nothing more. Standard library only, so it runs with any Python.

    "statusLine": {"type": "command",
                   "command": "python \"<repo>/server/claude/statusline.py\""}
"""

import json
import os
import sys
import urllib.request


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except ValueError:
        return
    port = os.environ.get("CATSOLE_PORT", "8730")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/claude/status", raw.encode("utf-8"),
            {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=0.3).close()
    except Exception:
        pass

    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "wk")):
        block = (limits or {}).get(key) or {}
        pct = block.get("used_percentage")
        if isinstance(pct, (int, float)):
            parts.append(f"{label} {pct:.0f}%")
    if parts:
        sys.stdout.reconfigure(encoding="utf-8")
        print(" · ".join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
