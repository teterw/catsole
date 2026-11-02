"""Local control panel.

Bound to the loopback interface only. This exposes media metadata and
hardware telemetry with no authentication, which is fine for 127.0.0.1 and
would not be fine on 0.0.0.0.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .app import MODES, STUDY_ACTIONS

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(console) -> Flask:
    app = Flask(__name__, static_folder=None)

    # Werkzeug logs every poll at INFO, which at 1Hz buries everything else.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "panel.html")

    @app.get("/study")
    def study_page():
        return send_from_directory(STATIC_DIR, "study.html")

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

    # ---- study mode --------------------------------------------------------

    @app.post("/api/study/start")
    def study_start():
        body = request.get_json(silent=True) or {}
        subject = str(body.get("subject") or "").strip()
        timer = body.get("timer")
        minutes = body.get("minutes")
        if minutes is not None:
            try:
                minutes = int(minutes)
            except (TypeError, ValueError):
                return jsonify({"error": "minutes must be a whole number"}), 400
        if not subject:
            return jsonify({"error": "what are you studying?"}), 400
        if not console.study_start(subject, timer, minutes):
            return jsonify({"error": "could not start that session"}), 400
        return jsonify({"ok": True})

    @app.post("/api/study/control")
    def study_control():
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in STUDY_ACTIONS:
            return jsonify({"error": f"unknown action: {action}"}), 400
        return jsonify({"ok": console.study_control(action)})

    @app.post("/api/study/note")
    def study_note():
        body = request.get_json(silent=True) or {}
        return jsonify({"ok": console.study_note(str(body.get("note") or ""))})

    @app.get("/api/study/summary")
    def study_summary():
        return jsonify(console.study_summary())

    @app.post("/api/settings")
    def settings():
        body = request.get_json(silent=True) or {}
        s = console.settings
        if "daily_goal_min" in body:
            try:
                goal = int(body["daily_goal_min"])
            except (TypeError, ValueError):
                goal = 0
            if not 5 <= goal <= 1440:
                return jsonify({"error": "daily goal must be 5 to 1440 minutes"}), 400
            s.daily_goal_min = goal
        if "chime" in body:
            s.chime = bool(body["chime"])
        console.save_settings()
        return jsonify({"settings": asdict(s)})

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
