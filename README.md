# 🛡️ KAVACH-07 v7.0.0
**Nuclear-Grade Crypto Futures Signal & Execution Bot**

KAVACH-07 is a high-performance, asynchronous trading system designed for the Oracle Cloud Free Tier. It integrates real-time data from Binance Futures with automated execution on the Hyperliquid L1 DEX, governed by a multi-layered risk management engine and an AI-driven news sentinel.

## 🚀 Core Features
- **Primary Exchange**: Hyperliquid (REST + WebSocket).
- **Data Source**: Binance Futures (Full stream aggregation).
- **Consensus Engine**: Meta-Strategy aggregating 21 distinct strategy modules.
- **Risk Controls**: 3% Risk/Trade, 5% Daily Loss Limit, Pearson Correlation filter.
- **Intelligence**: OpenAI GPT-4o-mini news sentiment analysis + Whale Alert on-chain tracking.
- **UI**: Rich-based terminal dashboard for real-time monitoring.

## 🛠️ Installation

### 1. Requirements
- Ubuntu 22.04 LTS (Optimized for ARM64/OCPU).
- Python 3.10+.
- Binance API (Public data access).
- Hyperliquid Private Key (Execution).
- Telegram Bot Token & Chat ID.
- OpenAI API Key.

### 2. Setup
```bash
git clone https://github.com/your-repo/kavach-07.git
cd kavach-07
chmod +x setup.sh
./setup.sh