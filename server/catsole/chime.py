"""A short sound on the PC when a study countdown runs out.

The board has no buzzer, and a flashing screen is easy to miss when the
reason for the countdown is that you are looking at something else.
"""

from __future__ import annotations

import os
import subprocess

LINUX_SOUND = "/usr/share/sounds/freedesktop/stereo/complete.oga"


def play() -> None:
    try:
        if os.name == "nt":
            import winsound

            winsound.PlaySound("SystemNotification",
                               winsound.SND_ALIAS | winsound.SND_ASYNC)
        elif os.path.exists(LINUX_SOUND):
            subprocess.Popen(["paplay", LINUX_SOUND],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
