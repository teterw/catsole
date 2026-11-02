"""Claude Code status line: passes the plan's usage limits to catsole, and
prints them for the bottom of the terminal.

Claude Code runs this with a JSON on stdin after each reply. It must stay
fast and quiet: catsole leaves a port file in its data folder while its
panel is up, and without one the network is not touched at all. Standard
library only, so it runs with any Python.

    "statusLine": {"type": "command",
                   "command": "python \"<repo>/server/claude/statusline.py\""}
"""

import json
import sys

from catsole_link import post


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except ValueError:
        return
    post("/api/claude/status", raw)

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
