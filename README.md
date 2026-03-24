# Kalshi Energy Arb Bot 🛢️

An automated, event-driven trading bot designed to trade Energy prediction markets on [Kalshi](https://kalshi.com) (WTI Crude Oil & Natural Gas) by exploiting edge-case volatility events and weekly EIA stockpiles reports.

## Architecture & Strategies
- **EIA Inventory Sniper (High Frequency)**: Triggers precisely on Wednesdays at 10:30 AM EST to fetch the US Energy Information Administration's stockpile data the millisecond it publishes, automatically sweeping Kalshi L2 order books on crude inventory markets.
- **WTI Spot Tracer (Mean Reversion)**: Continuously evaluates accurate probability thresholds against Kalshi implied options pricing, using an algorithmic Ornstein-Uhlenbeck stochastic model derived from `yfinance` 30-day historical data.

## Getting Started

### 1. Requirements
- Python 3.11+
- Kalshi Account & API Key + RSA Private Key
- EIA Open Data API Key ([Free Registration here](https://www.eia.gov/opendata/register.php))
- Telegram Bot Token & Chat ID (for live alerts)

### 2. Local Setup
```bash
git clone https://github.com/prayingperceptions/oil-and-gas-bot.git
cd oil-and-gas-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuration
Copy `.env.example` to `.env` (or create `.env`) and populate:
```env
KALSHI_API_KEY=your_kalshi_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
EIA_API_KEY=your_eia_api_key
```
Save your Kalshi RSA Private Key specifically in a new file named `kalshi.key` in the root directory.

### 4. Running
```bash
python main.py
```

## Security & Infrastructure
- Fully `.gitignore`-enforced to protect `kalshi.key` and `.env` credentials.
- Containerized (`Dockerfile`) for easy continuous deploy to AWS or Railway.
- SQLite local database tracking all order histories and EIA ingestion metrics autonomously.
