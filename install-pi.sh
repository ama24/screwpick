#!/usr/bin/env bash
# Install sortering-pnp on a Raspberry Pi 4 or 5 (64-bit Raspberry Pi OS, Bookworm or Trixie).
#
#   git clone https://github.com/ama24/sortering-pnp.git ~/sortering-pnp
#   ~/sortering-pnp/install-pi.sh            # add --service to start sortering-pnp at every boot
#
# Everything is pinned, so running this again later installs exactly the same:
# - the Python packages: requirements-pi.lock.txt (exact versions + file hashes, nothing else);
# - screw-synth (model and camera code): the commit below.
# To upgrade on purpose: python scripts/lock_pi.py, change SUPREME_COMMIT, test, commit.
set -euo pipefail

SUPREME_REPO=https://github.com/ama24/screw-synth.git
SUPREME_COMMIT=3038baf427bffc0cfd58c08f5dbb4332caea7349
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPREME="${SUPREME_DIR:-$HOME/screw-synth}"
VENV="${VENV:-$HOME/screwrig}"   # the same environment screwrig uses
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
say "system packages (camera, git)"
sudo apt-get update
sudo apt-get install -y python3-venv python3-picamera2 rpicam-apps git libgl1

# ---------------------------------------------------------------- screw-synth
say "screw-synth at $SUPREME_COMMIT"
if [ ! -d "$SUPREME/.git" ]; then
  git clone "$SUPREME_REPO" "$SUPREME"
fi
git -C "$SUPREME" fetch --quiet origin
if [ -n "$(git -C "$SUPREME" status --porcelain --untracked-files=no)" ]; then
  echo "Warning: $SUPREME has local changes: left as it is (not switched to the pinned commit)."
else
  git -C "$SUPREME" -c advice.detachedHead=false checkout --quiet "$SUPREME_COMMIT"
fi

# ---------------------------------------------------------------- Python packages
say "Python environment $VENV"
[ -d "$VENV" ] || python3 -m venv --system-site-packages "$VENV"   # sees Picamera2 from apt
"$VENV/bin/pip" install --no-deps --require-hashes -r "$HERE/requirements-pi.lock.txt"
"$VENV/bin/pip" install --no-deps --no-build-isolation \
  -e "$SUPREME" -e "$SUPREME/rig" -e "$HERE"

say "checking the installation"
"$VENV/bin/python" - <<'EOF'
import importlib
for mod in ("cv2", "torch", "ultralytics", "ncnn", "manifold3d", "websocket",
            "screwsynth", "screwrig", "sortering_pnp"):
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
  sudo systemctl enable --now sortering-pnp.service
  echo "Logs: journalctl -u sortering-pnp -f"
fi

cat <<EOF

Done. Next:
  1. Klipper: cp $HERE/klipper/sortering_pnp.cfg ~/printer_data/config/
     cp -n $HERE/klipper/sortering_pnp_picker.cfg ~/printer_data/config/   (placeholder, kept if present)
     and add [include sortering_pnp.cfg] to printer.cfg, then FIRMWARE_RESTART.
     Choose and install your picker later in the web page (Setup tab, Picker).
  2. source $VENV/bin/activate && cd $HERE && sortering-pnp check
  3. sortering-pnp serve   (or run this script again with --service to start it at every boot)
EOF
