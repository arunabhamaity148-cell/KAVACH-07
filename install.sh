#!/usr/bin/env bash
# ============================================================
# KAVACH-07 — Installation Script
# Target: Oracle VPS Ubuntu 22.04 (aarch64 or x86_64)
# Run as root: sudo bash install.sh
# ============================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

INSTALL_DIR="/opt/kavach-07"
SERVICE_USER="kavach"
LOG_DIR="/var/log"
PYTHON_MIN="3.11"

# ── Check root ────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root: sudo bash install.sh"

info "=== KAVACH-07 Installation ==="

# ── System update ─────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get install -y -qq \
    build-essential \
    software-properties-common \
    python3-pip \
    python3-venv \
    python3.11 \
    python3.11-dev \
    python3.11-venv \
    git \
    curl \
    htop \
    logrotate \
    ufw 2>&1 | tail -5

# ── Verify Python version ─────────────────────────────────────
PYTHON_CMD=$(which python3.11 || which python3 || error "Python 3.11 not found")
PY_VER=$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python version: $PY_VER"

# ── Create service user ───────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --shell /bin/bash --home "$INSTALL_DIR" --create-home "$SERVICE_USER"
    info "Created user: $SERVICE_USER"
else
    info "User $SERVICE_USER already exists"
fi

# ── Create install directory ──────────────────────────────────
mkdir -p "$INSTALL_DIR"

# ── Copy files ────────────────────────────────────────────────
info "Copying application files..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rsync -a --exclude='.git' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='.env' \
    "$SCRIPT_DIR/" "$INSTALL_DIR/"

# ── Virtual environment ───────────────────────────────────────
info "Creating virtual environment..."
"$PYTHON_CMD" -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q

info "Installing dependencies..."
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# ── Log directory & rotation ──────────────────────────────────
touch "$LOG_DIR/kavach-07.log"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR/kavach-07.log"

# Logrotate config
cat > /etc/logrotate.d/kavach-07 << 'LOGROTATE'
/var/log/kavach-07.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 kavach kavach
}
LOGROTATE
info "Log rotation configured"

# ── Environment file ──────────────────────────────────────────
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    warn "Created $INSTALL_DIR/.env — EDIT IT before starting!"
    warn "Required: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
    warn "Optional: BINANCE_API_KEY, BINANCE_SECRET_KEY"
else
    info ".env already exists — not overwritten"
fi

# ── Permissions ───────────────────────────────────────────────
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
chmod 600 "$INSTALL_DIR/.env" 2>/dev/null || true

# ── Systemd service ───────────────────────────────────────────
info "Installing systemd service..."
cp "$INSTALL_DIR/kavach-07.service" /etc/systemd/system/kavach-07.service
systemctl daemon-reload
systemctl enable kavach-07
info "Service enabled (not started — configure .env first)"

# ── Firewall ──────────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    ufw allow ssh
    ufw --force enable 2>/dev/null || true
    info "UFW firewall: SSH allowed, outbound open"
fi

# ── Aliases for convenience ───────────────────────────────────
cat >> /etc/bash.bashrc << 'ALIASES'

# KAVACH-07 shortcuts
alias k7-start="systemctl start kavach-07"
alias k7-stop="systemctl stop kavach-07"
alias k7-restart="systemctl restart kavach-07"
alias k7-status="systemctl status kavach-07"
alias k7-logs="journalctl -u kavach-07 -f"
alias k7-tail="tail -f /var/log/kavach-07.log"
ALIASES

# ── Final instructions ────────────────────────────────────────
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       KAVACH-07 Installation Complete!            ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════╝${NC}"
echo ""
echo "Next steps:"
echo "  1. Edit config:   nano $INSTALL_DIR/.env"
echo "  2. Test config:   cd $INSTALL_DIR && venv/bin/python -c 'from config import get_config; get_config()'"
echo "  3. Start service: systemctl start kavach-07"
echo "  4. View logs:     journalctl -u kavach-07 -f"
echo "     (or alias):    k7-logs"
echo ""
echo "Telegram commands (after start): /start /status /balance"
echo ""
