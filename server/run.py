"""desk-console entry point.

    python run.py                  normal operation
    python run.py --no-serial      run the whole pipeline with no board
    python run.py --once           print one frame and exit
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from desk_console.app import MODES, DeskConsole
from desk_console.config import Config
from desk_console.link import NullLink, SerialLink, find_port

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "desk-console"
LOG_FILE = LOG_DIR / "desk-console.log"


def setup_logging(verbose: bool) -> None:
    """Log to the console when there is one, and always to a rotating file.

    Under autostart the process runs via pythonw with no console at all, so
    the file handler is the only way to find out what happened.
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=512_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        print(f"could not open log file: {exc}", file=sys.stderr)

    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="desk-console PC-side service")
    parser.add_argument("--port", help="serial port (default: autodetect by USB VID/PID)")
    parser.add_argument("--no-serial", action="store_true", help="run without a board")
    parser.add_argument("--no-web", action="store_true", help="skip the control panel")
    parser.add_argument("--once", action="store_true", help="print one frame and exit")
    parser.add_argument("--mode", choices=MODES, help="starting mode")
    parser.add_argument("--offset-ms", type=int, help="lyric timing offset")
    parser.add_argument("--web-port", type=int, help="control panel port")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def build_console(args) -> DeskConsole:
    config = Config.load(HERE / "config.json")

    if args.port:
        config.serial_port = args.port
    if args.mode:
        config.start_mode = args.mode
    if args.offset_ms is not None:
        config.lyric_offset_ms = args.offset_ms
    if args.web_port:
        config.web_port = args.web_port

    # DeskConsole builds its own SerialLink when link is None, because the
    # link needs the console's event handler to exist first.
    link = NullLink(echo=args.once) if args.no_serial else None
    return DeskConsole(config, link=link)


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    log = logging.getLogger("desk-console")

    if not args.no_serial:
        detected = find_port(args.port)
        if detected:
            log.info("device port: %s", detected)
        else:
            log.warning("no device port found yet; will keep retrying")

    console = build_console(args)

    if args.once:
        console.tick()
        console.stop()
        return 0

    if not args.no_web:
        from desk_console.web import serve

        threading.Thread(
            target=serve, args=(console,), name="web", daemon=True
        ).start()
        log.info(
            "control panel on http://%s:%s",
            console.config.web_host,
            console.config.web_port,
        )

    console.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
