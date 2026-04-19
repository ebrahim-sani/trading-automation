"""
TTFM Backtest Runner
─────────────────────────────────────────────────────────────────────────────
Usage:
    python run_backtest.py                         # default config
    python run_backtest.py --symbols EURUSDm GBPUSDm --days 90
    python run_backtest.py --symbols XAUUSDm --days 180 --min-score 75 --risk 10

Requirements:
    - MetaTrader5 terminal must be running with a logged-in account.
    - Run this from the backtester/ directory  OR  set PYTHONPATH.
"""

import sys
import os
import logging
import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Allow imports from project root ─────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))
sys.path.insert(0, str(Path(__file__).parent.parent / "Kronos"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import MetaTrader5 as mt5
import pandas as pd

from backtest_engine import BacktestEngine, BacktestResult
from report import generate_html_report

# ─────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("RunBacktest")

TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
}


# ─────────────────────────────────────────────────────────────────────────
#  MT5 DATA FETCH
# ─────────────────────────────────────────────────────────────────────────

def fetch_bars(symbol: str, tf, days: int) -> pd.DataFrame:
    """Fetch historical OHLCV from MT5 for the given symbol + timeframe."""
    utc_to   = datetime.now(timezone.utc)
    utc_from = utc_to - timedelta(days=days)
    bars = mt5.copy_rates_range(symbol, tf, utc_from, utc_to)
    if bars is None or len(bars) == 0:
        log.error(f"No data for {symbol} — error {mt5.last_error()}")
        return pd.DataFrame()

    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    
    # Convert spread from MT5 'points' integer (e.g. 5, 20) to price scale (e.g. 0.00005, 0.02)
    s_info = mt5.symbol_info(symbol)
    if s_info is not None and "spread" in df.columns:
        df["spread"] = df["spread"] * s_info.point
        
    log.info(f"  {symbol} [{tf}] → {len(df)} bars ({df.index[0]} → {df.index[-1]})")
    return df


# ─────────────────────────────────────────────────────────────────────────
#  PER-SYMBOL BACKTEST
# ─────────────────────────────────────────────────────────────────────────

def backtest_symbol(
    symbol:    str,
    days:      int,
    min_score: int,
    min_rr:    float,
    risk_usd:  float,
    session_start: float,
    session_end:   float,
    use_ai:        bool,
) -> BacktestResult | None:

    log.info(f"\n{'─'*60}")
    log.info(f"  Backtesting: {symbol}  |  {days} days  |  min_score={min_score}")
    log.info(f"{'─'*60}")

    df_m5 = fetch_bars(symbol, mt5.TIMEFRAME_M5, days)
    df_h1 = fetch_bars(symbol, mt5.TIMEFRAME_H1, days + 30)  # extra for EMA warm-up
    df_h4 = fetch_bars(symbol, mt5.TIMEFRAME_H4, days + 60)

    if df_m5.empty or df_h1.empty or df_h4.empty:
        log.error(f"  Skipping {symbol} — insufficient data")
        return None

    engine = BacktestEngine(
        df_m5=df_m5,
        df_h1=df_h1,
        df_h4=df_h4,
        symbol=symbol,
        min_score=min_score,
        min_rr=min_rr,
        risk_usd=risk_usd,
        session_start=session_start,
        session_end=session_end,
        use_ai=use_ai,
    )

    result = engine.run()

    # ── Console summary ──────────────────────────────────────────────────
    log.info(f"\n  ┌─ {symbol} Results ─────────────────────────────────")
    log.info(f"  │  Total Trades : {result.total}")
    log.info(f"  │  Win / Loss   : {result.wins}W / {result.losses}L / {result.timeouts}TO")
    log.info(f"  │  Win Rate     : {result.win_rate*100:.1f}%")
    log.info(f"  │  Expectancy   : {result.expectancy:+.3f}R")
    log.info(f"  │  Total PnL    : {result.total_pnl_r:+.2f}R  (${result.total_pnl_usd:+.2f})")
    log.info(f"  │  Max Drawdown : {result.max_drawdown_r:.2f}R")
    log.info(f"  │  Sharpe Ratio : {result.sharpe_ratio:.2f}")
    log.info(f"  └────────────────────────────────────────────────────")

    return result


# ─────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="TTFM Strategy Backtester")
    p.add_argument(
        "--symbols", nargs="+",
        default=["EURUSDm", "GBPUSDm", "XAUUSDm"],
        help="MT5 symbol names to backtest",
    )
    p.add_argument("--days",      type=int,   default=90,   help="Number of historical days to load")
    p.add_argument("--min-score", type=int,   default=80,   help="Minimum composite score (0-140)")
    p.add_argument("--min-rr",    type=float, default=2.5,  help="Minimum reward-to-risk ratio")
    p.add_argument("--risk",      type=float, default=5.0,  help="Risk per trade in USD (for PnL calc)")
    p.add_argument("--session-start", type=float, default=7.5,  help="Session start (decimal UTC hour)")
    p.add_argument("--session-end",   type=float, default=19.0, help="Session end (decimal UTC hour)")
    p.add_argument("--use-ai", action="store_true", help="Enable Kronos and Vibe during backtest (EXTREMELY SLOW)")
    p.add_argument("--output", type=str, default="backtest_report.html", help="Output HTML path")
    p.add_argument("--no-browser", action="store_true", help="Don't open browser after generating report")
    return p.parse_args()


def main():
    args = parse_args()

    log.info("Initialising MetaTrader5...")
    if not mt5.initialize():
        log.error(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)

    info = mt5.terminal_info()
    log.info(f"Connected to: {info.name if info else 'Unknown'}")

    results = []
    for symbol in args.symbols:
        if not mt5.symbol_select(symbol, True):
            log.warning(f"  Cannot select {symbol} in Market Watch — skipping")
            continue
        r = backtest_symbol(
            symbol        = symbol,
            days          = args.days,
            min_score     = args.min_score,
            min_rr        = args.min_rr,
            risk_usd      = args.risk,
            session_start = args.session_start,
            session_end   = args.session_end,
            use_ai        = args.use_ai,
        )
        if r is not None:
            results.append(r)

    mt5.shutdown()

    if not results:
        log.error("No results generated. Exiting.")
        sys.exit(1)

    # ── Aggregate across symbols ─────────────────────────────────────────
    total_trades = sum(r.total for r in results)
    total_wins   = sum(r.wins  for r in results)
    overall_wr   = (total_wins / total_trades * 100) if total_trades else 0.0

    log.info("\n" + "═"*60)
    log.info("  ██  OVERALL SUMMARY")
    log.info("═"*60)
    log.info(f"  Symbols     : {', '.join(r.symbol for r in results)}")
    log.info(f"  Total Trades: {total_trades}")
    log.info(f"  Win Rate    : {overall_wr:.1f}%")
    log.info(f"  Total PnL   : {sum(r.total_pnl_r for r in results):+.2f}R  "
             f"(${sum(r.total_pnl_usd for r in results):+.2f})")
    log.info("═"*60)

    # ── Generate HTML report ─────────────────────────────────────────────
    output_path = str(Path(__file__).parent / args.output)
    generate_html_report(results, output_path)

    if not args.no_browser:
        webbrowser.open(f"file:///{output_path.replace(os.sep, '/')}")


if __name__ == "__main__":
    main()
