#!/usr/bin/env python3
"""
TTFM Strategy Engine — Local, no TradingView needed.
Run this on the same Windows machine as MT5.

Install: pip install MetaTrader5 numpy requests
"""
import logging
from engine import StrategyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

if __name__ == "__main__":
    StrategyEngine(
        symbols               = ["EURUSDm", "GBPUSDm", "XAUUSDm", "BTCUSDm", "ETHUSDm", "XAGUSDm", "USDJPYm", "USDCADm", "EURJPYm", "GBPJPYm"],
        timeframe_entry       = "M5",    # Your scalping chart
        left_bars             = 5,       # Must match your Pine Script input
        right_bars            = 5,       # Must match your Pine Script input
        min_rr                = 2.0,     # Must match your Pine Script minRR
        risk_usd              = 5.0,
        trade_timeout_minutes = 60,
        max_open_trades       = 6,
        max_pivot_bars        = 80,
    ).run()