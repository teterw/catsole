"""Two global shortcuts, standing in for the buttons the board doesn't have.

Windows only: RegisterHotKey on a thread of its own with a message loop.
A registered combo belongs to catsole across the whole desktop. Windows
refuses a combo another program registered the same way ("taken"), but it
cannot see shortcuts that live inside an app, so those are simply taken
over -- the panel says as much.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Callable

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
MODIFIERS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")

WM_HOTKEY, WM_QUIT, WM_APP = 0x0312, 0x0012, 0x8000

# Keys that may stand alone; letters and digits need a modifier, or the
# shortcut would swallow ordinary typing.
_SPECIAL = {
    "pause": 0x13, "scrolllock": 0x91, "home": 0x24, "end": 0x23,
    "insert": 0x2D, "delete": 0x2E, "pageup": 0x21, "pagedown": 0x22,
}
_SPECIAL.update({f"f{n}": 0x70 + n - 1 for n in range(1, 25)})
_SPECIAL.update({f"numpad{n}": 0x60 + n for n in range(10)})
_PLAIN = {chr(c): ord(chr(c).upper()) for c in range(ord("a"), ord("z") + 1)}
_PLAIN.update({str(n): ord(str(n)) for n in range(10)})
_NAMES = {vk: name for name, vk in {**_SPECIAL, **_PLAIN}.items()}


def parse_combo(text: str) -> tuple[int, int]:
    """"ctrl+f11" -> (MOD_CONTROL, VK_F11). Raises ValueError."""
    parts = [p.strip().lower() for p in (text or "").split("+")]
    if not parts or any(not p for p in parts):
        raise ValueError(f"not a shortcut: {text!r}")
    *mods, key = parts
    flags = 0
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(f"unknown modifier: {m}")
        flags |= MODIFIERS[m]
    if key in _SPECIAL:
        return flags, _SPECIAL[key]
    if key in _PLAIN and flags:
        return flags, _PLAIN[key]
    raise ValueError(f"not a key catsole can use: {key}")


def format_combo(mods: int, vk: int) -> str:
    names = [m for m in MODIFIER_ORDER if mods & MODIFIERS[m]]
    return "+".join(names + [_NAMES[vk]])


class Hotkeys:
    def __init__(self, on_press: Callable[[str], None]):
        self.on_press = on_press
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._requests: queue.Queue = queue.Queue()
        self._ids: dict[int, str] = {}

    @property
    def supported(self) -> bool:
        return os.name == "nt"

    def start(self) -> None:
        if not self.supported or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)

    def stop(self) -> None:
        if self._thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def bind(self, bindings: dict[str, str]) -> dict[str, str]:
        """Swap in new combos; the status of each, by name."""
        if not self.supported or not self._thread_id:
            return {name: "unsupported" for name in bindings}
        import ctypes

        done = threading.Event()
        result: dict[str, str] = {}
        self._requests.put((dict(bindings), result, done))
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_APP, 0, 0)
        if not done.wait(2.0):
            return {name: "unsupported" for name in bindings}
        return result

    # ---- the hotkey thread ------------------------------------------------

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        # A thread has no message queue until it asks for one; PostThreadMessage
        # to it fails before then.
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                name = self._ids.get(int(msg.wParam))
                if name:
                    try:
                        self.on_press(name)
                    except Exception:
                        log.exception("shortcut %s failed", name)
            elif msg.message == WM_APP:
                self._apply_requests(user32)
        for hotkey_id in list(self._ids):
            user32.UnregisterHotKey(None, hotkey_id)

    def _apply_requests(self, user32) -> None:
        while True:
            try:
                bindings, result, done = self._requests.get_nowait()
            except queue.Empty:
                return
            for hotkey_id in list(self._ids):
                user32.UnregisterHotKey(None, hotkey_id)
            self._ids.clear()
            for hotkey_id, (name, combo) in enumerate(bindings.items(), start=1):
                try:
                    mods, vk = parse_combo(combo)
                except ValueError:
                    result[name] = "invalid"
                    continue
                if user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
                    self._ids[hotkey_id] = name
                    result[name] = "ok"
                else:
                    result[name] = "taken"
            log.info("shortcuts: %s", ", ".join(f"{n} {bindings[n]} {s}" for n, s in result.items()))
            done.set()
