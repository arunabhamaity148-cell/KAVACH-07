#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# KAVACH-07 Setup Script
# Ubuntu 22.04 LTS ARM64 (Oracle Cloud Free Tier)
# Run as: sudo bash setup.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail
KAVACH_DIR="/home/ubuntu/kavach"
VENV_DIR="$KAVACH_DIR/venv"
SERVICE_FILE="$KAVACH_DIR/kavach.service"
SYSTEMD_PATH="/etc/systemd/system/kavach07.service"

echo "================================================"
echo "  KAVACH-07 Setup — $(date)"
echo "================================================"

# ── 1. System update & dependencies ───────────────────────────────────────────
echo "[1/7] Updating system packages…"
apt-get update -qq
apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    python3-pip \
    git curl wget \
    build-essential \
    libssl-dev libffi-dev \
    tzdata \
    logrotate \
    htop

# Set timezone to IST
timedatectl set-timezone Asia/Kolkata || true

echo "✓ System packages installed"

# ── 2. Python virtual environment ─────────────────────────────────────────────
echo "[2/7] Creating Python virtual environment…"
if [ ! -d "$VENV_DIR" ]; then
    python3.11 -m venv "$VENV_DIR"
    echo "✓ Venv created at $VENV_DIR"
else
    echo "  Venv already exists — skipping"
fi

# ── 3. Install Python dependencies ────────────────────────────────────────────
echo "[3/7] Installing Python dependencies…"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$KAVACH_DIR/requirements.txt" --quiet
echo "✓ Python dependencies installed"

# ── 4. .env file check ────────────────────────────────────────────────────────
echo "[4/7] Checking .env configuration…"
if [ ! -f "$KAVACH_DIR/.env" ]; then
    cp "$KAVACH_DIR/.env.example" "$KAVACH_DIR/.env"
    echo ""
    echo "  ⚠️  .env file created from template."
    echo "  ⚠️  EDIT $KAVACH_DIR/.env and fill in your API keys before starting!"
    echo ""
else
    echo "  .env already exists — skipping"
fi

# ── 5. Create directories ─────────────────────────────────────────────────────
echo "[5/7] Creating log and data directories…"
mkdir -p "$KAVACH_DIR/logs"
chown -R ubuntu:ubuntu "$KAVACH_DIR"
echo "✓ Directories created"

# ── 6. Install systemd service ────────────────────────────────────────────────
echo "[6/7] Installing systemd service…"
cp "$SERVICE_FILE" "$SYSTEMD_PATH"
systemctl daemon-reload
systemctl enable kavach07.service
echo "✓ systemd service installed (kavach07)"

# ── 7. Log rotation ───────────────────────────────────────────────────────────
echo "[7/7] Configuring log rotation…"
cat > /etc/logrotate.d/kavach07 << 'EOF'
/home/ubuntu/kavach/logs/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su ubuntu ubuntu
}
EOF
echo "✓ Log rotation configured"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════"
echo "  KAVACH-07 Setup Complete"
echo "════════════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "  1. Edit .env:    nano $KAVACH_DIR/.env"
echo "  2. Start bot:    sudo systemctl start kavach07"
echo "  3. Check status: sudo systemctl status kavach07"
echo "  4. View logs:    sudo journalctl -u kavach07 -f"
echo "  5. Stop bot:     sudo systemctl stop kavach07"
echo ""
echo "  ⚠️  DISCLAIMER: This bot is for SIGNAL GENERATION ONLY."
echo "  ⚠️  Manual confirmation required before any trade execution."
echo "  ⚠️  Crypto trading carries substantial risk of loss."
echo ""
