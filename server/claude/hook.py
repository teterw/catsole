"""Claude Code hook: tells catsole when a session starts working
(UserPromptSubmit), waits for you (Stop) or ends (SessionEnd).

Claude Code runs this with the hook's JSON on stdin. It prints nothing and
always exits 0, so it can never block or fail a turn. Standard library only.
"""

import os
import sys
import urllib.request


def main() -> None:
    raw = sys.stdin.read()
    port = os.environ.get("CATSOLE_PORT", "8730")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/claude/event", raw.encode("utf-8"),
        {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=0.3).close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
