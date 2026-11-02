"""How Claude Code's scripts reach catsole: only when it is up, never via a
proxy, and never for longer than TIMEOUT_S."""

import os
import urllib.request
from pathlib import Path

TIMEOUT_S = 0.3
PORT_FILE = "catsole.port"


def data_dir() -> Path:
    override = os.environ.get("CATSOLE_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "catsole"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "catsole"


def panel_port() -> int | None:
    """The panel's port while catsole runs; None when it is not up."""
    try:
        return int((data_dir() / PORT_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def post(path: str, body: str) -> None:
    port = panel_port()
    if port is None:
        return
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", body.encode("utf-8"),
                                 {"Content-Type": "application/json"})
    # No proxy for 127.0.0.1, whatever the environment or the registry says.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.open(req, timeout=TIMEOUT_S).close()
