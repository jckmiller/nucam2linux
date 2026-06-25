#!/usr/bin/env bash
# =============================================================================
#  nucam2linux – setup.sh
#  Prepares an Ubuntu desktop to receive an Android camera over USB via ADB/scrcpy
#  Run as a regular user (sudo will be invoked where needed).
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/nucam2linux"

# ---------------------------------------------------------------------------
# 1. Check we are on a Debian/Ubuntu system
# ---------------------------------------------------------------------------
if ! command -v apt-get &>/dev/null; then
    die "This setup script requires apt-get (Ubuntu/Debian). Exiting."
fi

# ---------------------------------------------------------------------------
# 2. Update package list
# ---------------------------------------------------------------------------
info "Updating package lists…"
sudo apt-get update -qq

# ---------------------------------------------------------------------------
# 3. Install runtime dependencies
# ---------------------------------------------------------------------------
info "Installing dependencies: adb, ffmpeg, scrcpy, v4l2loopback-dkms, v4l2loopback-utils, v4l-utils, python3-pip, python3-venv, curl, wget…"
sudo apt-get install -y \
    adb \
    ffmpeg \
    v4l2loopback-dkms \
    v4l2loopback-utils \
    v4l-utils \
    python3-pip \
    python3-venv \
    curl \
    wget

# ---------------------------------------------------------------------------
# 4. Install scrcpy via apt (snap version causes GPU interface errors)
# ---------------------------------------------------------------------------
# Remove the snap version if it exists — it shadows the apt binary and triggers
# "gpu-2404-provider-wrapper not found: ensure slot is connected" errors.
if snap list scrcpy &>/dev/null 2>&1; then
    info "Removing snap version of scrcpy (replacing with apt)…"
    sudo snap remove scrcpy
fi

if command -v scrcpy &>/dev/null; then
    SCRCPY_VER=$(scrcpy --version 2>&1 | head -1)
    info "scrcpy already installed via apt: $SCRCPY_VER"
else
    info "Installing scrcpy via apt…"
    sudo apt-get install -y scrcpy
fi

# Verify scrcpy supports --video-source=camera (requires v2.0+)
SCRCPY_MAJOR=$(scrcpy --version 2>&1 | grep -oP '\d+\.\d+' | head -1 | cut -d. -f1 || echo "0")
if [[ "$SCRCPY_MAJOR" -lt 2 ]]; then
    warn "scrcpy < 2.0 detected. The --video-source=camera flag may not be available."
    warn "Consider upgrading: https://github.com/Genymobile/scrcpy/releases"
fi

# ---------------------------------------------------------------------------
# 5. Load v4l2loopback kernel module
# ---------------------------------------------------------------------------
info "Loading v4l2loopback kernel module…"
if lsmod | grep -q v4l2loopback; then
    info "v4l2loopback already loaded."
else
    sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="nucam2linux" exclusive_caps=1
    success "v4l2loopback loaded as /dev/video10"
fi

# Make the module load automatically on boot
MODCONF=/etc/modules-load.d/v4l2loopback.conf
if [[ ! -f "$MODCONF" ]]; then
    info "Configuring v4l2loopback to load on boot…"
    echo "v4l2loopback" | sudo tee "$MODCONF" > /dev/null
fi

MODOPTCONF=/etc/modprobe.d/v4l2loopback.conf
if [[ ! -f "$MODOPTCONF" ]]; then
    info "Writing v4l2loopback module options…"
    echo 'options v4l2loopback devices=1 video_nr=10 card_label="nucam2linux" exclusive_caps=1' \
        | sudo tee "$MODOPTCONF" > /dev/null
fi

# ---------------------------------------------------------------------------
# 6. Add user to the 'video' group (needed to write to /dev/videoX)
# ---------------------------------------------------------------------------
if groups "$USER" | grep -q '\bvideo\b'; then
    info "User '$USER' already in 'video' group."
else
    info "Adding '$USER' to the 'video' group…"
    sudo usermod -aG video "$USER"
    warn "Group change will take effect on next login."
fi

# ---------------------------------------------------------------------------
# 7. Install udev rule so the virtual device is accessible without root
# ---------------------------------------------------------------------------
UDEV_RULE=/etc/udev/rules.d/99-nucam2linux.rules
if [[ ! -f "$UDEV_RULE" ]]; then
    info "Installing udev rule for v4l2loopback device…"
    echo 'KERNEL=="video[0-9]*", SUBSYSTEM=="video4linux", GROUP="video", MODE="0660"' \
        | sudo tee "$UDEV_RULE" > /dev/null
    sudo udevadm control --reload-rules && sudo udevadm trigger
fi

# ---------------------------------------------------------------------------
# 8. Install Python dependencies
# ---------------------------------------------------------------------------
info "Installing Python dependencies (rich)…"
# Ubuntu 23.04+ enforces PEP 668 (externally-managed-environment).
# Prefer the system apt package; fall back to pip with --break-system-packages.
if apt-cache show python3-rich &>/dev/null 2>&1; then
    sudo apt-get install -y python3-rich
else
    pip3 install --quiet --upgrade --break-system-packages rich \
        || pip3 install --quiet --upgrade rich
fi

# ---------------------------------------------------------------------------
# 9. Copy project files to ~/nucam2linux
# ---------------------------------------------------------------------------
if [[ "$SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
    info "Copying project files to $INSTALL_DIR…"
    mkdir -p "$INSTALL_DIR"
    for f in nucam.py nucam.conf nucam.service README.md; do
        [[ -f "$SCRIPT_DIR/$f" ]] && cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
    done
fi

# ---------------------------------------------------------------------------
# 10. Install and enable the systemd user service
# ---------------------------------------------------------------------------
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

info "Installing systemd user service…"
cp "$INSTALL_DIR/nucam.service" "$SYSTEMD_USER_DIR/nucam.service"

systemctl --user daemon-reload
systemctl --user enable nucam.service
success "systemd service enabled. It will start automatically on next login."
info  "To start it right now: systemctl --user start nucam.service"
info  "To watch its logs:     journalctl --user -fu nucam.service"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  nucam2linux setup complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  Next steps:"
echo "  1. On your Android phone:"
echo "     • Go to Settings → About Phone → tap 'Build number' 7 times"
echo "     • Go to Settings → Developer Options → enable 'USB Debugging'"
echo "  2. Connect the phone via USB and accept the ADB authorization dialog"
echo "  3. Run:  python3 ~/nucam2linux/nucam.py"
echo "  4. Open your browser and select 'nucam2linux' as the camera input"
echo ""
