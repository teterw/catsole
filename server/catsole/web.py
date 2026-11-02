"""Local control panel.

Bound to the loopback interface only. This exposes media metadata and
hardware telemetry with no authentication, which is fine for 127.0.0.1 and
would not be fine on 0.0.0.0.
"""

from __future__ import annotations

import atexit
import logging
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .app import MODES, STUDY_ACTIONS
from . import weather as weather_module
from .hotkeys import format_combo, parse_combo

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
PORT_FILE = "catsole.port"


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

    # ---- Claude Code's status line and hooks --------------------------------

    @app.post("/api/claude/status")
    def claude_status():
        # JSON only: a web page can send text/plain here without asking.
        body = request.get_json(silent=True) or {}
        console.claude.on_status(body if isinstance(body, dict) else {})
        return jsonify({"ok": True})

    @app.post("/api/claude/event")
    def claude_event():
        body = request.get_json(silent=True) or {}
        console.claude.on_event(body if isinstance(body, dict) else {})
        return jsonify({"ok": True})

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
        if "weather_city" in body:
            city = str(body["weather_city"] or "").strip()
            place = None
            if city:
                place = weather_module.geocode(city, console.config.user_agent)
                if place is None:
                    return jsonify({"error": f"city not found: {city}"}), 400
            s.weather = place
            console.weather.set_location(place)
        rebind = False
        for key in ("hotkey_menu", "hotkey_pick"):
            if key not in body:
                continue
            try:
                combo = format_combo(*parse_combo(str(body[key])))
            except ValueError:
                return jsonify({"error": "not a key catsole can use"}), 400
            other = s.hotkey_pick if key == "hotkey_menu" else s.hotkey_menu
            if combo == other:
                return jsonify({"error": "the two shortcuts must differ"}), 400
            setattr(s, key, combo)
            rebind = True
        console.save_settings()
        status = console.rebind_hotkeys() if rebind else console.hotkey_status
        return jsonify({"settings": asdict(s), "hotkeys": status})

    return app


def write_port_file(console) -> None:
    """Tell Claude Code's scripts where the panel is, and that it is up.

    They skip the network when the file is missing, so a closed catsole
    costs a prompt nothing rather than a refused connection's timeout."""
    data_dir = console.config.data_dir
    if data_dir is None:
        return
    path = Path(data_dir) / PORT_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(console.config.web_port), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        return
    atexit.register(lambda: path.unlink(missing_ok=True))


def serve(console) -> None:
    """Run the panel. Called on a daemon thread from run.py."""
    app = create_app(console)
    write_port_file(console)
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
