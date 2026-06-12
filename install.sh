#!/bin/bash
# ============================================================
# terminator-session-restore — One-click installer
# Installs dependencies, configures autostart, optional cron
# Supports: Ubuntu/Debian, Fedora, Arch, openSUSE
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESTORE_SCRIPT="$SCRIPT_DIR/session_restore.py"
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!!]${NC} $1"; }
error() { echo -e "${RED}[ERR]${NC} $1"; }

echo "=========================================="
echo " terminator-session-restore — installer"
echo "=========================================="
echo

# ------- Detect distro -------
if [ -f /etc/os-release ]; then
    . /etc/os-release
    DISTRO="$ID"
else
    DISTRO="unknown"
fi

# ------- Package manager wrapper -------
install_pkg() {
    case "$DISTRO" in
        ubuntu|debian|linuxmint|pop)
            sudo apt-get install -y "$@"
            ;;
        fedora)
            sudo dnf install -y "$@"
            ;;
        centos|rhel|rocky|alma)
            sudo yum install -y "$@"
            ;;
        arch|manjaro)
            sudo pacman -S --noconfirm "$@"
            ;;
        opensuse*|sles)
            sudo zypper install -y "$@"
            ;;
        *)
            error "Unsupported distro: $DISTRO. Please install manually: $*"
            exit 1
            ;;
    esac
}

# ------- [1/4] Python3 -------
echo "[1/4] Checking Python3..."
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        info "Python3 installed ($(python3 --version))"
    else
        warn "Python3 too old ($PY_VERSION), need 3.10+. Upgrading..."
        install_pkg python3
    fi
else
    warn "Python3 not found, installing..."
    install_pkg python3
fi

if ! python3 -c "import sys; assert sys.version_info >= (3, 10)" 2>/dev/null; then
    error "Python3.10+ required. Please install manually."
    exit 1
fi
info "Python3 $(python3 --version | awk '{print $2}')"

# ------- [2/4] Terminator -------
echo
echo "[2/4] Checking Terminator..."
if command -v terminator &>/dev/null; then
    info "Terminator installed"
else
    warn "Terminator not found, installing..."
    install_pkg terminator
fi

# ------- [3/4] Helper tools -------
echo
echo "[3/4] Checking helper tools (pstree, xdotool, xwininfo)..."

NEED_INSTALL=()

if ! command -v pstree &>/dev/null; then
    NEED_INSTALL+=("psmisc")
fi

if ! command -v xdotool &>/dev/null; then
    NEED_INSTALL+=("xdotool")
fi

if ! command -v xwininfo &>/dev/null; then
    case "$DISTRO" in
        arch|manjaro) NEED_INSTALL+=("xorg-xwininfo") ;;
        fedora|centos|rhel) NEED_INSTALL+=("xorg-x11-utils") ;;
        *) NEED_INSTALL+=("x11-utils") ;;
    esac
fi

if [ ${#NEED_INSTALL[@]} -gt 0 ]; then
    warn "Installing: ${NEED_INSTALL[*]}"
    install_pkg "${NEED_INSTALL[@]}"
else
    info "All helper tools present"
fi

# ------- [4/4] Autostart -------
echo
echo "[4/4] Configuring autostart..."

AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/terminator-session-restore.desktop"

mkdir -p "$AUTOSTART_DIR"
mkdir -p "$SCRIPT_DIR/data"

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=TerminatorSessionRestore
Comment=Restore Terminator layout and Claude/Codex sessions on boot
Exec=/usr/bin/python3 $RESTORE_SCRIPT restore
Terminal=false
Categories=Development;Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
EOF

chmod +x "$RESTORE_SCRIPT"
info "Autostart configured: $DESKTOP_FILE"

# ------- Optional: cron snapshot -------
echo
CRON_LINE="* * * * * /usr/bin/python3 $RESTORE_SCRIPT snapshot >/dev/null 2>&1"

if crontab -l 2>/dev/null | grep -qF "$RESTORE_SCRIPT"; then
    info "Cron snapshot already configured"
else
    read -p "Enable automatic snapshot every minute? (recommended) [Y/n] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
        info "Cron snapshot enabled (every minute)"
    else
        warn "Skipped. Remember to run: python3 session_restore.py snapshot"
    fi
fi

# ------- Done -------
echo
echo "=========================================="
echo -e "${GREEN} Installation complete!${NC}"
echo "=========================================="
echo
echo "Usage:"
echo "  Save snapshot:    python3 $RESTORE_SCRIPT snapshot"
echo "  Restore sessions: python3 $RESTORE_SCRIPT restore"
echo "  Show status:      python3 $RESTORE_SCRIPT status"
echo "  Remove autostart: python3 $RESTORE_SCRIPT install --uninstall"
echo
echo "Workflow:"
echo "  1. Cron saves terminal state every minute while you work"
echo "  2. After reboot, autostart restores all panes + Claude sessions"
echo
if command -v terminator &>/dev/null && ! pgrep -x terminator &>/dev/null; then
    warn "Terminator is not running. Start it first, then run:"
    echo "  python3 $RESTORE_SCRIPT snapshot"
elif pgrep -x terminator &>/dev/null; then
    echo "Terminator detected, taking initial snapshot..."
    python3 "$RESTORE_SCRIPT" snapshot
fi
