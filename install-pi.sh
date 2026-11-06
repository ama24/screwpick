#!/usr/bin/env bash
# Install sortering-pnp on a Raspberry Pi 4 or 5 (64-bit Raspberry Pi OS, Bookworm or Trixie).
#
#   git clone https://github.com/ama24/sortering-pnp.git ~/sortering-pnp
#   ~/sortering-pnp/install-pi.sh            # add --service to start sortering-pnp at every boot
#
# sortering-pnp needs nothing else from GitHub: the trained model is uploaded in the web page
# (Models tab). The Python packages are pinned (requirements-pi.lock.txt: exact versions + file
# hashes), so running this again later installs exactly the same.
# To upgrade on purpose: python scripts/lock_pi.py, test on the Pi, commit.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$HERE/.venv}"
SERVICE=0
[ "${1:-}" = "--service" ] && SERVICE=1

say() { printf '\n== %s\n' "$*"; }

# ---------------------------------------------------------------- checks
arch=$(uname -m)
if [ "$arch" != aarch64 ]; then
  echo "This needs 64-bit Raspberry Pi OS (aarch64); this system is $arch." >&2
  exit 1
fi
py=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
case "$py" in
  3.11 | 3.13) ;;
  *) echo "Python $py: the lock is made for 3.11 (Bookworm) and 3.13 (Trixie)." >&2; exit 1 ;;
esac
mem=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)
if [ "$mem" -lt 1800 ]; then
  echo "Warning: ${mem} MB of memory. Klipper, the camera and the model need a Pi with 4 GB or more."
fi

# ---------------------------------------------------------------- system packages
say "system packages (camera, the Pi's pins, git)"
sudo apt-get update
sudo apt-get install -y python3-venv python3-picamera2 rpicam-apps git libgl1 \
  python3-gpiozero python3-lgpio

# ---------------------------------------------------------------- the picker's pins
# sortering-pnp switches the picker on the Pi's own pins (no Klipper set-up for it). Servos need
# hardware PWM on GPIO12/13: one line in config.txt, read at boot. Added once, never twice.
REBOOT=0
CFGTXT=/boot/firmware/config.txt
[ -f "$CFGTXT" ] || CFGTXT=/boot/config.txt
PWM_LINE="dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4"
if [ -f "$CFGTXT" ] && ! grep -q "^dtoverlay=pwm-2chan" "$CFGTXT"; then
  say "hardware PWM for the picker's servos ($CFGTXT)"
  echo "$PWM_LINE" | sudo tee -a "$CFGTXT" >/dev/null
  REBOOT=1
fi
if ! id -nG "$USER" | grep -qw gpio; then
  sudo usermod -aG gpio "$USER"   # the pins without sudo
  REBOOT=1
fi

# ---------------------------------------------------------------- Python packages
say "Python environment $VENV"
[ -d "$VENV" ] || python3 -m venv --system-site-packages "$VENV"   # sees Picamera2 from apt
"$VENV/bin/pip" install --no-deps --require-hashes -r "$HERE/requirements-pi.lock.txt"
"$VENV/bin/pip" install --no-deps --no-build-isolation -e "$HERE"

say "checking the installation"
"$VENV/bin/python" - <<'EOF'
import importlib
for mod in ("cv2", "torch", "ultralytics", "ncnn", "manifold3d", "websocket",
            "gpiozero", "sortering_pnp"):
    importlib.import_module(mod)
import cv2, torch
assert hasattr(cv2, "aruco"), "OpenCV without ArUco: opencv-python overwrote the contrib build"
assert "+cpu" in torch.__version__, f"torch {torch.__version__} is not the CPU build"
torch.ones(2) + 1  # the CUDA build would stop here with "Illegal instruction"
print(f"ok: OpenCV {cv2.__version__}, torch {torch.__version__}")
EOF

# ---------------------------------------------------------------- start at boot (optional)
if [ "$SERVICE" = 1 ]; then
  say "sortering-pnp service (starts at boot; automatic mode resumes if it was on)"
  sudo tee /etc/systemd/system/sortering-pnp.service >/dev/null <<EOF
[Unit]
Description=sortering-pnp screw sorter
After=network-online.target moonraker.service
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HERE
ExecStart=$VENV/bin/sortering-pnp serve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable sortering-pnp.service
  sudo systemctl restart sortering-pnp.service   # runs the code just installed
  echo "Logs: journalctl -u sortering-pnp -f"
fi

cat <<EOF

Done. Next:
  1. Klipper: cp $HERE/klipper/sortering_pnp.cfg ~/printer_data/config/
     and add [include sortering_pnp.cfg] to printer.cfg (once), then FIRMWARE_RESTART.
     The picker needs nothing in Klipper: choose it in the web page (Setup tab, Picker).
  2. source $VENV/bin/activate && cd $HERE && sortering-pnp check
  3. sortering-pnp serve   (or run this script again with --service to start it at every boot)
EOF
if [ "$REBOOT" = 1 ]; then
  echo
  echo "Reboot once (sudo reboot): the picker's pins are ready after it."
fi
