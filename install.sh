#!/usr/bin/env bash
# Install catsole on Linux: the service, the firmware, and autostart.
#
#   curl -fsSL https://raw.githubusercontent.com/teterw/catsole/main/install.sh | bash
#
# It installs what is missing (Python, venv support, parec), puts the app and
# its own virtual environment in ~/.local/share/catsole, lets your user reach
# the board, flashes it if it is plugged in, and runs catsole as a systemd
# user service so it starts when you log in. Running it again updates in
# place, keeping config.json and the lyrics cache.
#
# To remove it:
#
#   curl -fsSL https://raw.githubusercontent.com/teterw/catsole/main/install.sh | bash -s -- --uninstall
#
# Environment overrides: CATSOLE_DIR, CATSOLE_BRANCH, CATSOLE_NO_FLASH=1,
# CATSOLE_NO_SERVICE=1, and CATSOLE_SOURCE=<checkout> to install from a local
# copy instead of downloading.
set -euo pipefail

# Everything runs from main(), called on the last line: piped into bash, the
# script is read as it runs, and a command reading stdin would eat the rest.
main() {

REPO="teterw/catsole"
BRANCH="${CATSOLE_BRANCH:-main}"
DIR="${CATSOLE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/catsole}"
SOURCE="${CATSOLE_SOURCE:-}"
NO_FLASH="${CATSOLE_NO_FLASH:-}"
NO_SERVICE="${CATSOLE_NO_SERVICE:-}"
UNINSTALL=""
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=1 ;;
        --no-flash) NO_FLASH=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

APP="$DIR/app"
VENV="$DIR/venv"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/catsole.service"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/catsole.desktop"
LOG_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/catsole/catsole.log"
UDEV_RULE="/etc/udev/rules.d/60-catsole.rules"
FQBN="arduino:renesas_uno:unor4wifi"
CORE="arduino:renesas_uno@1.6.0"
LIBRARIES=("U8g2@2.35.30" "ArduinoJson@7.4.2")

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

has_user_systemd() { systemctl --user show-environment >/dev/null 2>&1; }

stop_catsole() {
    if has_user_systemd; then systemctl --user stop catsole 2>/dev/null || true; fi
    pkill -f "$APP/server/run.py" 2>/dev/null || true
}

if [ -n "$UNINSTALL" ]; then
    say "Removing catsole"
    stop_catsole
    if has_user_systemd && [ -f "$UNIT" ]; then
        systemctl --user disable catsole 2>/dev/null || true
    fi
    rm -f "$UNIT" "$AUTOSTART"
    if has_user_systemd; then systemctl --user daemon-reload 2>/dev/null || true; fi
    rm -rf "$DIR"
    if [ -f "$UDEV_RULE" ]; then
        $SUDO rm -f "$UDEV_RULE"
        $SUDO udevadm control --reload-rules 2>/dev/null || true
    fi
    say "Removed. The board keeps its firmware; it just waits for a PC again."
    exit 0
fi

# ---- system packages ---------------------------------------------------------

python_ok() {
    command -v python3 >/dev/null 2>&1 &&
        python3 -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 10))' 2>/dev/null
}

missing=()
python_ok || missing+=(python)
command -v parec >/dev/null 2>&1 || missing+=(parec)
command -v curl >/dev/null 2>&1 || missing+=(curl)

if [ "${#missing[@]}" -gt 0 ]; then
    say "Installing ${missing[*]}"
    if command -v apt-get >/dev/null 2>&1; then
        $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq </dev/null
        $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip pulseaudio-utils curl </dev/null
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y -q python3 python3-pip pulseaudio-utils curl
    elif command -v pacman >/dev/null 2>&1; then
        $SUDO pacman -S --needed --noconfirm python python-pip libpulse curl
    elif command -v zypper >/dev/null 2>&1; then
        $SUDO zypper --non-interactive install python3 python3-pip pulseaudio-utils curl
    else
        echo "Please install Python 3.10+ (with venv), parec (pulseaudio-utils) and curl, then run this again." >&2
        exit 1
    fi
    python_ok || { echo "Python 3.10+ with venv is still missing." >&2; exit 1; }
fi

# ---- the app -----------------------------------------------------------------

say "Stopping any running copy"
stop_catsole

say "Fetching catsole ($BRANCH)"
mkdir -p "$DIR"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
if [ -n "$SOURCE" ]; then
    fresh="$staging/src"
    mkdir -p "$fresh"
    for part in arduino server README.md install.ps1 install.sh; do
        [ -e "$SOURCE/$part" ] && cp -r "$SOURCE/$part" "$fresh/"
    done
    rm -rf "$fresh/server/cache" "$fresh/server/config.json" "$fresh/server/.venv"
else
    curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$staging"
    fresh="$(find "$staging" -mindepth 1 -maxdepth 1 -type d -name 'catsole-*' | head -n 1)"
fi
# Keep what is yours across an update: settings and fetched lyrics.
for keep in server/config.json server/cache; do
    if [ -e "$APP/$keep" ]; then rm -rf "$fresh/$keep"; mv "$APP/$keep" "$fresh/$keep"; fi
done
rm -rf "$APP"
mv "$fresh" "$APP"

say "Installing Python packages (a minute or two the first time)"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$APP/server/requirements.txt"

# ---- the board -----------------------------------------------------------------

# Arduino's USB ids, for the serial port and for uploading. Without this only
# root may open the board.
if [ ! -f "$UDEV_RULE" ]; then
    say "Letting your user reach the board"
    printf '%s\n' \
        '# catsole: Arduino boards, readable and writable by the logged-in user' \
        'SUBSYSTEMS=="usb", ATTRS{idVendor}=="2341", MODE="0666", TAG+="uaccess"' \
        | $SUDO tee "$UDEV_RULE" >/dev/null
    $SUDO udevadm control --reload-rules 2>/dev/null || true
    $SUDO udevadm trigger --subsystem-match=tty 2>/dev/null || true
    sleep 1
fi

find_board() {
    "$VENV/bin/python" - <<'PY'
from serial.tools import list_ports
print(next((p.device for p in list_ports.comports() if p.vid == 0x2341 and p.pid == 0x1002), ""))
PY
}

if [ -n "$NO_FLASH" ]; then
    say "Skipping the firmware, as asked"
else
    port="$(find_board)"
    if [ -z "$port" ]; then
        say "No board found, so the firmware was not flashed"
        note "Plug it in and run this command again to flash it."
    else
        say "Flashing the board on $port"
        cli="$DIR/tools/arduino-cli"
        if [ ! -x "$cli" ]; then
            case "$(uname -m)" in
                x86_64) flavour="Linux_64bit" ;;
                aarch64 | arm64) flavour="Linux_ARM64" ;;
                armv7l) flavour="Linux_ARMv7" ;;
                *) echo "No arduino-cli build for $(uname -m); flash from another PC." >&2; exit 1 ;;
            esac
            mkdir -p "$DIR/tools"
            curl -fsSL "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_${flavour}.tar.gz" |
                tar -xz -C "$DIR/tools" arduino-cli
        fi
        # A private Arduino setup, so nothing here touches an Arduino IDE you
        # may already have, and its library versions stay pinned.
        ard="$DIR/ard"
        mkdir -p "$ard"
        cfg="$ard/cli.yaml"
        printf 'directories:\n  data: %s\n  downloads: %s\n  user: %s\n' \
            "$ard/d" "$ard/dl" "$ard/u" >"$cfg"
        note "Board support and libraries (large the first time)"
        "$cli" --config-file "$cfg" core update-index >/dev/null
        "$cli" --config-file "$cfg" core install "$CORE" >/dev/null
        "$cli" --config-file "$cfg" lib install "${LIBRARIES[@]}" >/dev/null
        note "Compiling and uploading"
        if ! "$cli" --config-file "$cfg" compile --upload -p "$port" --fqbn "$FQBN" "$APP/arduino/catsole" >/dev/null; then
            echo "Flashing failed. Unplug the board, plug it back in, and run this again." >&2
            exit 1
        fi
        note "Flashed."
    fi
fi

# ---- autostart -----------------------------------------------------------------

if [ -n "$NO_SERVICE" ]; then
    say "Installed. Not starting it, as asked."
    exit 0
fi

say "Setting catsole to start when you log in"
if has_user_systemd; then
    # A user service, not a system one: what is playing (MPRIS) and the sound
    # output both live in your login session, which a system service never sees.
    mkdir -p "$UNIT_DIR"
    cat >"$UNIT" <<EOF
[Unit]
Description=catsole desk display
After=default.target

[Service]
ExecStart=$VENV/bin/python $APP/server/run.py
WorkingDirectory=$APP/server
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable catsole >/dev/null 2>&1
    say "Starting it now"
    systemctl --user restart catsole
else
    # No systemd user session: fall back to the desktop's own autostart.
    mkdir -p "$(dirname "$AUTOSTART")"
    cat >"$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=catsole
Exec=$VENV/bin/python $APP/server/run.py
Path=$APP/server
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
    say "Starting it now"
    (cd "$APP/server" && nohup "$VENV/bin/python" run.py >/dev/null 2>&1 &)
fi

ok=""
for _ in $(seq 1 30); do
    if curl -fs --max-time 2 http://127.0.0.1:8730/api/state >/dev/null 2>&1; then ok=1; break; fi
    sleep 0.7
done
echo
if [ -n "$ok" ]; then
    say "catsole is running."
    note "Control panel: http://127.0.0.1:8730"
else
    say "Installed, but it has not answered yet. The log will say why:"
    note "$LOG_FILE"
fi
note "It starts by itself whenever you log in."
}

main "$@"
