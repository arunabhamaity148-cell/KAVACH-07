# KAVACH-07 — Deployment & Operations Guide

> **MODE: SIGNAL-ONLY** — No auto-execution. All trade decisions are yours.
> Paper-trade for ≥30 days before drawing any conclusions.

---

## Table of Contents
1. [Architecture Overview](#1-architecture-overview)
2. [Oracle VPS Setup](#2-oracle-vps-setup)
3. [Installation](#3-installation)
4. [Configuration](#4-configuration)
5. [Telegram Setup](#5-telegram-setup)
6. [Starting the Bot](#6-starting-the-bot)
7. [Telegram Commands](#7-telegram-commands)
8. [Monitoring & Logs](#8-monitoring--logs)
9. [Strategy Reference](#9-strategy-reference)
10. [Risk Controls](#10-risk-controls)
11. [ML Engine](#11-ml-engine)
12. [Paper Trading Workflow](#12-paper-trading-workflow)
13. [Troubleshooting](#13-troubleshooting)
14. [Security Hardening](#14-security-hardening)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                     KAVACH-07                           │
│                                                         │
│  DataEngine ──────────► SignalEngine                   │
│  (WS + REST)           (10 strategies)                 │
│      │                      │                          │
│      │                 MLEngine                        │
│      │                (river online ML)                │
│      │                      │                          │
│      └──────────► ExecutionEngine                      │
│                  (paper trade simulation)              │
│                       │                                │
│              RiskManager ◄──── CircuitBreakers        │
│                       │                                │
│            MonitoringEngine                            │
│                       │                                │
│              TelegramBot ─────────► You               │
└─────────────────────────────────────────────────────────┘
```

**Key design choices:**
- **Signal-only**: No API keys needed for signal generation (public Binance streams)
- **Async**: Single event loop, zero blocking calls
- **Persistent**: SQLite survives restarts; balance/positions/ML model preserved
- **Adaptive**: ML engine learns from outcomes, adjusts confidence
- **Protected**: 4-layer risk system (signal filter → regime → risk manager → circuit breaker)

---

## 2. Oracle VPS Setup

### Recommended spec
| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU      | 1 vCPU  | 2 vCPU      |
| RAM      | 1 GB    | 2 GB        |
| Storage  | 20 GB   | 50 GB       |
| OS       | Ubuntu 22.04 | Ubuntu 22.04 |
| Network  | 1 Gbps  | 1 Gbps      |

Oracle Free Tier (ARM Ampere) works perfectly.

### Initial server setup
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Set timezone to UTC
sudo timedatectl set-timezone UTC

# Sync clock (critical for WebSocket auth)
sudo apt install -y chrony
sudo systemctl enable chrony --now
chronyc tracking

# Verify Python 3.11
python3.11 --version
```

### Open firewall for outbound (Binance)
```bash
# Binance Futures uses TCP 443 (WSS) and 443 (HTTPS)
# Oracle VPS default iptables rules block outbound — fix:
sudo iptables -I OUTPUT -p tcp --dport 443 -j ACCEPT
sudo iptables -I OUTPUT -p tcp --dport 80  -j ACCEPT

# Persist rules
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

---

## 3. Installation

```bash
# Clone or upload the kavach-07 directory to /tmp/kavach-07
# Then run:
sudo bash /tmp/kavach-07/install.sh
```

The installer:
- Creates `/opt/kavach-07` with a Python virtualenv
- Creates a `kavach` system user (no login shell)
- Installs all Python dependencies
- Configures log rotation
- Registers the systemd service
- Creates `.env` from template (you edit it)

### Manual install (if automated script fails)
```bash
sudo mkdir -p /opt/kavach-07
sudo useradd --system --shell /bin/bash kavach
sudo cp -r kavach-07/* /opt/kavach-07/
cd /opt/kavach-07
python3.11 -m venv venv
venv/bin/pip install -r requirements.txt
sudo chown -R kavach:kavach /opt/kavach-07
```

---

## 4. Configuration

Edit `/opt/kavach-07/.env`:

```bash
sudo nano /opt/kavach-07/.env
```

### Required settings

```dotenv
# Telegram (REQUIRED — signals go nowhere without this)
TELEGRAM_BOT_TOKEN=1234567890:ABCDefGhijKLMNOpqrstUVWXYz
TELEGRAM_CHAT_ID=-1001234567890

# Set to false for live signals (still paper-trade only, no execution)
USE_TESTNET=true
```

### Optional Binance API (only needed for testnet position tracking)
```dotenv
BINANCE_API_KEY=your_key     # Leave blank for signal-only mode
BINANCE_SECRET_KEY=your_secret
```

### Risk tuning
```dotenv
INITIAL_BALANCE=1000.0        # Paper balance
MAX_RISK_PER_TRADE=0.005      # 0.5% per trade (start conservative)
MAX_TOTAL_EXPOSURE=0.02       # 2% max open risk
MAX_DAILY_LOSS=0.05           # Halt at -5% daily
DRAWDOWN_HALT_THRESHOLD=0.15  # Halt at -15% drawdown
```

### Validate config
```bash
cd /opt/kavach-07
venv/bin/python -c "from config import get_config; c = get_config(); print(c.summary())"
```

Expected output:
```
KAVACH-07 | Mode=TESTNET | Balance=$1000 | Pairs=25 | Strategies=10 | Risk=0.5%/trade
```

---

## 5. Telegram Setup

### Create your bot
1. Open Telegram, search for `@BotFather`
2. Send `/newbot`
3. Choose a name (e.g., `KAVACH07 Signal Bot`)
4. Copy the **bot token** → set as `TELEGRAM_BOT_TOKEN`

### Get your chat ID
1. Send any message to your bot
2. Open: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id":XXXXXXXXXX}` → set as `TELEGRAM_CHAT_ID`

> For group chats, the ID is negative (e.g., `-1001234567890`).

### Verify
```bash
curl "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=Test"
```

---

## 6. Starting the Bot

```bash
# Start
sudo systemctl start kavach-07

# Enable auto-start on reboot
sudo systemctl enable kavach-07

# Check status
sudo systemctl status kavach-07

# Live logs
sudo journalctl -u kavach-07 -f

# Or check log file
sudo tail -f /var/log/kavach-07.log
```

### First startup sequence
```
INFO | config | KAVACH-07 | Mode=TESTNET | Balance=$1000 | Pairs=25 ...
INFO | database | Database connected: kavach07.db
INFO | ml_engine | ML model initialised fresh (river)
INFO | risk_manager | No saved risk state — starting fresh: $1000.00
INFO | data_engine | Bootstrapping historical candles...
INFO | data_engine | Historical candles loaded
INFO | data_engine | DataEngine started — all streams launching
INFO | data_engine | WS[klines] connecting (100 streams)...
INFO | data_engine | WS[klines] connected
...
INFO | main | All components started — entering main scan loop
```

**First signal typically arrives within 5-10 minutes** (after enough market data accumulates).

---

## 7. Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Show command list |
| `/status` | System health overview |
| `/balance` | Balance, P&L, drawdown, win rate |
| `/signals` | Last 5 signals fired |
| `/trades` | Last 5 closed trades |
| `/positions` | Current open paper positions |
| `/report` | Force an hourly report now |
| `/config` | Show active configuration |
| `/pause` | Stop generating new signals |
| `/resume` | Resume signal generation + clear soft halts |
| `/halt` | Emergency halt + close all positions |

### Signal alert format
```
🚨 KAVACH-07 SIGNAL
━━━━━━━━━━━━━━━━━━━━
Pair: ETHUSDT
Strategy: LIQUIDATION_FADE
Direction: 🟢 LONG
Confidence: 68%

Entry: 3245.50 (LIMIT)
SL: 3210.00
TP1: 3310.00 (1.9R)
TP2: 3345.00

Rationale:
5min impulse: -3.2% (liquidation spike)
OI drop: -8.1% in 1h
CVD z-score: -3.1σ (extreme flow)
Reclaim: 62% of impulse recovered

Risk: 0.50% | ML: 62%
Time: 2024-11-15 14:32:07 UTC
```

---

## 8. Monitoring & Logs

### Hourly report (auto-sent to Telegram)
```
📊 KAVACH-07 HOURLY — 14:00 UTC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 Signals: 47 | Trades: 12 | Wins: 8
📈 Win Rate: 67% | PF: 2.1
📉 Drawdown: 1.8% | Balance: $1,018.40

🔥 Best: BTCUSDT +22.40 (OI_BREAKOUT)
❌ Worst: SOLUSDT -8.10 (FUNDING_SQUEEZE)

📊 Strategy Stats:
  LIQUIDATION_FADE: 5t | WR=80% | PnL=+31.20
  OI_BREAKOUT: 3t | WR=67% | PnL=+18.50
  FUNDING_SQUEEZE: 4t | WR=50% | PnL=+3.10

📡 WS: ✅ | Data: ✅ | ML: ✅
⚠️ Errors: 0 | Reconnects: 1 | Mem: 82MB
🤖 ML: 147 samples | ROC-AUC: 0.641 | Drift: No
```

### Log files
```bash
# Real-time logs
journalctl -u kavach-07 -f

# Last 100 lines
journalctl -u kavach-07 -n 100

# Filter by level
journalctl -u kavach-07 -f | grep -E "ERROR|CRITICAL"

# File log
tail -f /var/log/kavach-07.log

# Memory usage
journalctl -u kavach-07 -f | grep "Memory"
```

### Database inspection
```bash
cd /opt/kavach-07
sudo -u kavach venv/bin/python << 'EOF'
import asyncio
from database import Database

async def main():
    db = Database()
    await db.connect()
    
    sigs = await db.get_recent_signals(limit=5)
    print("Last 5 signals:")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} | {s['strategy']} | conf={s['confidence']:.2f}")
    
    trades = await db.get_recent_trades(limit=5)
    print("\nLast 5 trades:")
    for t in trades:
        print(f"  {t['symbol']} | PnL={t['pnl']:+.2f} | {t['exit_reason']}")
    
    stats = await db.get_strategy_stats()
    print("\nStrategy performance:")
    for s in stats:
        wr = s['wins']/s['trades']*100 if s['trades'] else 0
        print(f"  {s['strategy']}: {s['trades']}t | WR={wr:.0f}% | PnL={s['total_pnl']:+.2f}")
    
    await db.close()

asyncio.run(main())
EOF
```

---

## 9. Strategy Reference

| # | Name | Type | Min Bars | Typical R |
|---|------|------|----------|-----------|
| 1 | LIQUIDATION_FADE | Mean-reversion | 10 × 5m | 2.5R |
| 2 | FUNDING_SQUEEZE | Mean-reversion | 30 × funding | 2.0R |
| 3 | OB_IMBALANCE | Scalp | 21 × 1m | 1.2R |
| 4 | LIQUIDITY_SWEEP | ICT/SMC | 25 × 5m | 2.8R |
| 5 | VP_NODE | Volume profile | 20 × 1h | 2.0R |
| 6 | OI_BREAKOUT | Momentum | 25 × 1h | 3.5R |
| 7 | BASIS_REVERSION | Stat-arb | 50 × funding | 1.9R |
| 8 | REGIME_FILTER | Overlay | 5 × all | N/A |
| 9 | SOCIAL_FADE | Contrarian | 10 × 1h | 1.8R |
| 10 | EXCHANGE_ARB | Lead/lag | 2 × Bybit | 1.0R |

### Disable a strategy
In `.env`, set:
```dotenv
# Remove from the comma-separated list (or the default list in config.py)
```

Or in `config.py`, remove from `STRATEGIES` list.

---

## 10. Risk Controls

### 4-layer system

```
Layer 1: Pre-filter (spread, volume, ATR)
    ↓ passes
Layer 2: Signal validation (SL side, R-ratio ≥ 1.2, SL ≤ 8%)
    ↓ passes
Layer 3: ML confidence gate (> 0.55 after blending)
    ↓ passes
Layer 4: Risk Manager (size × drawdown multiplier × circuit state)
    ↓ approved
    Paper trade opened
```

### Circuit breaker states
| State | Trigger | Effect |
|-------|---------|--------|
| `OK` | Normal | Full size |
| `REDUCE` | DD ≥ 10% | 50% size |
| `HALT` | 3 losses / DD ≥ 15% / Daily loss ≥ 5% | No new trades |

HALT auto-expires:
- 3 consecutive losses → 1 hour
- Daily loss limit → resets at UTC midnight
- Deep drawdown → 7 days (requires `/resume`)

### Position sizing formula
```
size = (balance × risk_pct) / |entry - sl|
     × drawdown_multiplier
     × circuit_multiplier
```

With `MAX_RISK_PER_TRADE=0.005` and `balance=$1000`, `SL=$1`:
```
size = ($1000 × 0.005) / $1 = 5 units
```

---

## 11. ML Engine

### How it works
- **Algorithm**: `river.Pipeline(StandardScaler, LogisticRegression)`
- **Online learning**: Updates after every closed trade
- **Features** (18 total): price change, ATR%, OB imbalance, CVD z-score, funding, OI change, volume ratio, delta, F&G, confidence, R-ratio, SL distance
- **Output**: P(win) ∈ [0, 1]
- **Blending**: `confidence = 0.6 × raw_confidence + 0.4 × ml_score` (once trained)
- **Threshold**: Signal fires only if blended confidence ≥ 0.55
- **Drift detection**: ADWIN (δ=0.002) — resets model on detected regime change

### Training timeline
| Samples | Status |
|---------|--------|
| 0–99 | Predicts 0.5 (raw strategy confidence used) |
| 100+ | ML active, influences confidence |
| 200+ | Meaningful ROC-AUC available |
| 500+ | Drift detection becomes reliable |

### Monitor ML health
```bash
# In Telegram: /status shows ML sample count and ROC-AUC
# Or check logs:
journalctl -u kavach-07 | grep "ML updated"
```

---

## 12. Paper Trading Workflow

### Month 1 (Observation)
- Run with `USE_TESTNET=true`
- Do NOT change parameters
- Review hourly reports
- Track signals you would have taken manually

### Evaluation checklist after 30 days
- [ ] Win rate > 45%
- [ ] Profit factor > 1.3
- [ ] Max drawdown < 15%
- [ ] ML ROC-AUC > 0.55
- [ ] No more than 1 circuit-breaker halt

### Transition to live signals
1. Set `USE_TESTNET=false` in `.env`
2. Restart: `sudo systemctl restart kavach-07`
3. Data now comes from live Binance Futures
4. **Still no auto-execution** — signals are for your manual trading

### Manual execution workflow
When you receive a signal alert:
1. **Verify** price is still near entry level
2. **Check** current spread (must be < 0.1%)
3. **Size** based on your own account risk rules
4. **Set** SL and TP as shown in alert
5. **Note** the signal ID for tracking

---

## 13. Troubleshooting

### No signals appearing
```bash
# Check scan logs
journalctl -u kavach-07 | grep "Scan #"

# Typical output shows 0 signals — this is normal until:
# - ATR > 0.1% (dead market = no signals)
# - Spread < 0.1%
# - Volume > 1.2x average

# Force debug logging
sudo nano /opt/kavach-07/.env
# Set: LOG_LEVEL=DEBUG
sudo systemctl restart kavach-07
```

### WebSocket keeps reconnecting
```bash
# Check network
ping fstream.binance.com

# Check firewall
sudo iptables -L OUTPUT | grep ACCEPT

# Check system time (must be within 1s of real time)
timedatectl status
```

### Telegram messages not sending
```bash
# Test bot manually
curl "https://api.telegram.org/bot<TOKEN>/getMe"

# Check chat ID
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"

# Verify .env
grep TELEGRAM /opt/kavach-07/.env
```

### High memory usage (> 500 MB)
```bash
# Check memory
journalctl -u kavach-07 | grep "Memory"

# Reduce pair count in .env
# Remove slow-moving pairs from BASE_PAIRS in config.py
```

### Database corruption
```bash
sudo -u kavach /opt/kavach-07/venv/bin/python << 'EOF'
import sqlite3
conn = sqlite3.connect('/opt/kavach-07/kavach07.db')
result = conn.execute('PRAGMA integrity_check').fetchone()
print(result)  # Should print ('ok',)
conn.close()
EOF
```

### Reset ML model (start fresh)
```bash
sudo -u kavach rm /opt/kavach-07/ml_model.pkl
sudo systemctl restart kavach-07
```

### View circuit breaker status
```bash
sudo -u kavach /opt/kavach-07/venv/bin/python << 'EOF'
import asyncio
from database import Database

async def main():
    db = Database()
    await db.connect()
    row = await db.load_risk_metrics()
    if row:
        print(f"Balance: ${row['balance']:.2f}")
        print(f"Circuit: {row['circuit_state']} — {row['circuit_reason']}")
        print(f"Drawdown: {(row['peak_balance']-row['balance'])/row['peak_balance']*100:.1f}%")
    await db.close()

asyncio.run(main())
EOF
```

---

## 14. Security Hardening

### SSH hardening
```bash
sudo nano /etc/ssh/sshd_config
# Set:
# PasswordAuthentication no
# PermitRootLogin no
# Port 2222  (non-standard)
sudo systemctl restart sshd
```

### API key security (if used)
```bash
# Restrict .env permissions
chmod 600 /opt/kavach-07/.env
chown kavach:kavach /opt/kavach-07/.env

# Binance API restrictions:
# - Enable only Futures Trading
# - IP whitelist: your VPS IP only
# - Disable withdrawals
```

### Regular updates
```bash
# Monthly dependency update
cd /opt/kavach-07
sudo -u kavach venv/bin/pip install -r requirements.txt --upgrade
sudo systemctl restart kavach-07
```

### Backup
```bash
# Daily backup of database and ML model
cat > /etc/cron.daily/kavach-backup << 'EOF'
#!/bin/bash
BACKUP_DIR="/home/ubuntu/kavach-backups"
DATE=$(date +%Y%m%d)
mkdir -p "$BACKUP_DIR"
cp /opt/kavach-07/kavach07.db "$BACKUP_DIR/kavach07_${DATE}.db"
cp /opt/kavach-07/ml_model.pkl "$BACKUP_DIR/ml_model_${DATE}.pkl" 2>/dev/null || true
# Keep only last 7 days
find "$BACKUP_DIR" -name "*.db" -mtime +7 -delete
find "$BACKUP_DIR" -name "*.pkl" -mtime +7 -delete
EOF
chmod +x /etc/cron.daily/kavach-backup
```

---

## Quick Reference Card

```
START:    systemctl start kavach-07
STOP:     systemctl stop kavach-07
RESTART:  systemctl restart kavach-07
STATUS:   systemctl status kavach-07
LOGS:     journalctl -u kavach-07 -f
LOGS:     tail -f /var/log/kavach-07.log

TELEGRAM:
  /status    /balance    /signals
  /trades    /positions  /report
  /pause     /resume     /halt

DB:    /opt/kavach-07/kavach07.db
ML:    /opt/kavach-07/ml_model.pkl
LOG:   /var/log/kavach-07.log
ENV:   /opt/kavach-07/.env
```

---

*KAVACH-07 is a research tool. Past signal performance does not guarantee future results.
Always paper-trade for a statistically significant period before using signals to guide
real trading decisions. Never risk more than you can afford to lose.*
