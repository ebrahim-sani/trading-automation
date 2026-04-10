<div align="center">
  <h1>📈 TTFM Auto-Trade System</h1>
  <p><strong>A 100% Local, Automated Trading Engine for MetaTrader 5</strong></p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python Version" />
    <img src="https://img.shields.io/badge/NestJS-10.0-E0234E.svg" alt="NestJS Version" />
    <img src="https://img.shields.io/badge/MetaTrader-5-black.svg" alt="MT5" />
    <img src="https://img.shields.io/badge/Database-SQLite-003B57.svg" alt="SQLite" />
  </p>
</div>

<br />

The **TTFM Auto-Trade System** is a robust, lightweight, and fully automated algorithmic trading bot. It replaces expensive cloud webhook integrations (like TradingView) by running a completely local Python Strategy Engine that talks directly to your MetaTrader 5 (MT5) terminal.

Coupled with a lightning-fast **NestJS Bridge** and an embedded **SQLite Database**, it tracks, journals, and manages all your trades entirely on your own machine.

---

## ✨ Features

- 🐍 **Native MT5 Integration:** Direct connection via the official `MetaTrader5` Python library. No lag, no missed webhooks.
- 📐 **Fractal Pivot Strategy:** Built-in logic using multi-timeframe 200 EMA trend alignment, exact fractal pivots, and risk-to-reward (RR) filtering.
- 💰 **Dynamic Lot Sizing:** Automatically calculates the exact lot size needed to risk a flat dollar amount (e.g., $5) based on the asset's tick value and your Stop Loss distance.
- 🛡️ **Advanced Trade Management:** Features real-time timeout closures and automatic Breakeven triggers when a trade hits 1R in profit.
- 📊 **Local Trade Journal:** The NestJS Bridge silently journals every signal (even skipped ones) and every trade result into a local database for deep analytical insights.

---

## 🏗️ Architecture

1. **Python Strategy Engine (`/strategy`)**: Queries MT5 for live candle data, processes trend / pivot logic, and executes orders directly in the MT5 terminal.
2. **NestJS Bridge (`/bridge`)**: A local API server that receives event payloads from the Python engine and logs them to a Prisma-managed SQLite database.

---

## 🚀 Quick Start

### Prerequisites
- **Windows OS** (Required by MetaTrader 5)
- **Node.js** (v18+)
- **Python** (v3.10+)
- **MetaTrader 5** (Terminal open, logged in, and "Algo Trading" enabled)

### 1. Installation

Clone the repository and run the setup script. This installs all Node.js and Python dependencies and initializes your local database.

```bash
git clone https://github.com/yourusername/auto-trade-bot.git
cd auto-trade-bot
npm run setup
```

### 2. Configuration

Edit `strategy/main.py` to customize your trading parameters:
- **Symbols**: Add any asset you want to trade (e.g., `"BTCUSD"`, `"USDJPY"`).
- **Risk per trade**: Flat USD amount to risk (e.g., `risk_usd = 5.0`).
- **Session times**: Restrict trading strictly to your active visual hours (e.g., `"08:00"` to `"17:00"`).

### 3. Launch

Start the system using our one-click Windows batch launcher:

```bash
npm start
# OR double-click start.bat
```
*(This commands launches both the NestJS background listener and your Python engine in separate, cleanly formatted terminal windows.)*

---

## 📈 Analyzing Your Trades

The system natively exposes local JSON dashboards via your browser:

- **Performance Stats:** `http://localhost:4000/journal/stats`
  *(View Win Rate, Total PnL, Profit Factor, Average Win/Loss, and recent trades).*
- **Signal Log:** `http://localhost:4000/journal/signals`
  *(View every raw signal detected by the strategy).*
- **Filter Impact:** `http://localhost:4000/journal/filter-impact`
  *(See exactly how many bad trades the 4H Higher Timeframe filter saved you from).*

---

## 🛠️ Built With

* [Python](https://www.python.org/) - The Strategy Execution Engine
* [NestJS](https://nestjs.com/) - The Analytics Bridge
* [MetaTrader 5 Python API](https://www.mql5.com/en/docs/integration/python_metatrader5) - Broker Integration
* [Prisma ORM](https://www.prisma.io/) - Database mapping
* [SQLite](https://www.sqlite.org/index.html) - Zero-config local database

---

<div align="center">
  <p><i>Trade responsibly. This software is provided for educational and automation purposes only.</i></p>
</div>
