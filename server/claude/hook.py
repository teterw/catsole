"""Claude Code hook: tells catsole when a session starts working
(UserPromptSubmit), waits for you (Stop, Notification) or ends (SessionEnd).

Claude Code runs this with the hook's JSON on stdin. It prints nothing and
always exits 0, so it can never block or fail a turn. Standard library only.
"""

import sys

from catsole_link import post

if __name__ == "__main__":
    try:
        post("/api/claude/event", sys.stdin.read())
    except Exception:
        pass
