# 🛡️ KAVACH-07 — Nuclear-Grade Crypto Futures Signal Bot

> **⚠️ DISCLAIMER:** KAVACH-07 is a **signal generation system only**. It does **NOT** execute trades automatically. All signals require manual confirmation via Telegram. Crypto futures trading carries extreme risk of loss, including total loss of capital. Use at your own risk. Past performance does not guarantee future results.

---

## Architecture Overview

```
Binance Futures ──► DataEngine ──► MarketData
Hyperliquid     ──►     │          (19 strategies per symbol)
External APIs   ──►     │               │
                        ▼               ▼
                   [ADX/ATR/VWAP/CVD]  MetaStrategy
                                        │
                                   RiskManager
                                        │
                                   AlertManager ──► Telegram
                                        │
                                    DBManager ──► SQLite
                                        │
                                    Dashboard ──► Terminal
```

**Key numbers:**
- 10 active trading pairs (2 Tier-S + 6 Tier-A + 2 Tier-B)
- 19 strategy modules per symbol
- Asyncio-based, single-process, zero-blocking
- Oracle Cloud Free Tier compatible (1 OCPU, 1 GB RAM)

---

## Installation

### Prerequisites
- Ubuntu 22.04 LTS ARM64 (Oracle Cloud Free Tier)
- Python 3.11+
- Binance Futures account with API key
- Telegram Bot (via @BotFather)

### One-command setup

```bash
# Clone repo
git clone https://github.com/yourrepo/kavach-07.git /home/ubuntu/kavach
cd /home/ubuntu/kavach

# Run setup script
sudo bash setup.sh
```

### Manual setup (if needed)

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
nano .env
```

---

## Configuration

### `.env` (secrets — never commit)

```env
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
TELEGRAM_BOT_TOKEN=...   # from @BotFather
TELEGRAM_CHAT_ID=...     # your personal chat ID
```

Get your `TELEGRAM_CHAT_ID` by messaging `@userinfobot`.

### `config.yaml` (non-secret parameters)

Key sections:

| Section | Purpose |
|---|---|
| `risk` | Account size, max loss%, position limits |
| `trading_hours` | IST window (default 9:30–23:30) |
| `pairs` | Tier S/A/B symbol lists |
| `strategies` | Per-strategy enable/weight/thresholds |
| `meta_strategy` | Consensus threshold, regime modifiers |
| `data_engine` | Polling intervals, kline settings |

---

## Running

```bash
# Start
sudo systemctl start kavach07

# Status
sudo systemctl status kavach07

# Live logs
sudo journalctl -u kavach07 -f

# Stop
sudo systemctl stop kavach07

# Restart after config change
sudo systemctl restart kavach07
```

### Manual (foreground) run for testing:

```bash
source venv/bin/activate
python -m kavach.main
```

---

## Telegram Commands

| Command | Action |
|---|---|
| `/pause` | Stop processing new signals |
| `/resume` | Resume signal processing |
| `/status` | Bot status, daily PnL, open trades |
| `/trades` | List open trades |
| `/signals` | Last 5 generated signals |

When a signal arrives, tap **✅ YES** to confirm (logs trade to DB) or **❌ NO** to skip.

---

## Strategy Modules (19 total)

| Strategy | Logic | Weight |
|---|---|---|
| RegimeFilter | ADX/ATR regime classifier (filter only) | 0 |
| OiBreakout | OI spike + price breakout | 1.2 |
| FundingSqueeze | Extreme funding → contrarian | 1.0 |
| HyperliquidLeadlag | HL-Binance price divergence | 0.9 |
| EtfFlow | BTC ETF net flow proxy | 0.8 |
| StablecoinFlow | On-chain stablecoin movements | 0.8 |
| OnchainLiq | Liquidation cluster proximity | 1.1 |
| SectorRotation | Relative sector strength | 0.7 |
| CvdDivergence | Price vs CVD divergence | 1.1 |
| AbsorptionDetection | High vol + narrow bar = absorption | 1.0 |
| VwapReversion | Price vs VWAP deviation | 0.9 |
| SpotAccumulation | Spot volume spike + price action | 0.8 |
| LiquidationFade | Fade post-liquidation snap | 1.0 |
| SocialFade | Fear & Greed contrarian | 0.7 |
| DexCexArb | Binance vs Hyperliquid funding arb | 0.6 |
| PostSettlement | Post-quarterly-settlement reversion | 0.7 |
| TokenizedSecurity | TradFi risk-on/off macro bias | 0.5 |
| LiquidationCascade | Ride liquidation cascade momentum | 1.2 |
| MarketMakerPnl | Inferred MM PnL squeeze | 0.6 |

Weights are multiplied by regime modifiers in `config.yaml → meta_strategy.regime_weight_modifiers`.

---

## Database

SQLite at path `DB_PATH` in `.env` (default `kavach.db`).

Tables: `market_data`, `signals`, `trades`, `strategy_perf`, `bot_events`.

Query example:
```bash
sqlite3 kavach.db "SELECT symbol, side, confidence, entry, stop_loss, take_profit FROM signals ORDER BY timestamp DESC LIMIT 10;"
```

---

## Risk Controls

1. **Trading hours** — signals only between 9:30–23:30 IST (configurable)
2. **Regulatory FUD** — XMR, ZEC, DASH and others auto-rejected
3. **Daily loss limit** — bot pauses at 5% daily loss (configurable)
4. **Max open trades** — 5 concurrent (configurable)
5. **Correlation groups** — only 1 meme coin, 1 L2, etc. active at a time
6. **Position sizing** — risk-adjusted by confidence, SL distance, account size
7. **Minimum confidence** — 62% threshold before alert is sent

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `TELEGRAM_BOT_TOKEN not set` | Edit `.env`, check token from @BotFather |
| WebSocket disconnect loops | Check internet, Binance API status |
| `aiosqlite` errors | Check disk space, file permissions |
| Bot paused automatically | Daily loss limit hit — use `/resume` after review |
| No signals generated | Check trading hours, strategy thresholds in `config.yaml` |
| High RAM usage | Reduce `historical_kline_limit` and `maxlen` in `kline_data` |

---

## File Structure

```
kavach/
├── core/
│   ├── data_engine.py     # WebSocket + REST data ingestion
│   ├── indicators.py      # Pure-numpy ADX/ATR/VWAP/CVD/RSI
│   ├── meta_strategy.py   # Signal aggregation engine
│   ├── risk_manager.py    # All risk controls
│   └── alert_manager.py   # Telegram bot
├── db/
│   ├── manager.py         # Async SQLite CRUD
│   └── schema.sql         # Database schema
├── strategies/            # 19 strategy modules
├── ui/
│   └── dashboard.py       # Rich terminal dashboard
├── main.py                # Entry point
├── config.yaml            # All configuration
├── kavach.service         # systemd unit
├── setup.sh               # VPS setup automation
└── requirements.txt
```

---

## ⚠️ Risk Disclaimer

Cryptocurrency futures trading involves substantial risk of loss and is not suitable for all investors. Leveraged positions can result in losses exceeding your initial deposit. KAVACH-07 is provided as-is for educational and research purposes. The developers accept no responsibility for any financial losses incurred through use of this software. Always trade with money you can afford to lose entirely. Past signal accuracy does not guarantee future profitability.
