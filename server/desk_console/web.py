"""Local control panel.

Bound to the loopback interface only. This exposes media metadata and
hardware telemetry with no authentication, which is fine for 127.0.0.1 and
would not be fine on 0.0.0.0.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .app import MODES

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(console) -> Flask:
    app = Flask(__name__, static_folder=None)

    # Werkzeug logs every poll at INFO, which at 1Hz buries everything else.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "panel.html")

    @app.get("/api/state")
    def state():
        payload = console.snapshot()
        # The exact frame the device is being sent, so the panel's preview
        # renders from the same data rather than a parallel guess.
        payload["frame"] = console.build_frame()
        return jsonify(payload)

    @app.post("/api/mode")
    def set_mode():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode")
        if mode not in MODES:
            return jsonify({"error": f"unknown mode: {mode}"}), 400
        console.set_mode(mode)
        return jsonify({"mode": console.mode})

    @app.post("/api/refresh")
    def refresh():
        console.force_refresh()
        return jsonify({"refreshing": True})

    return app


def serve(console) -> None:
    """Run the panel. Called on a daemon thread from run.py."""
    app = create_app(console)
    try:
        app.run(
            host=console.config.web_host,
            port=console.config.web_port,
            threaded=True,
            debug=False,
            use_reloader=False,
        )
    except OSError as exc:
        log.warning("control panel could not start: %s", exc)
