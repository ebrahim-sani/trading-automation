"""
Live Trade Diagnostic — Fetches all v8.x trades from MT5 and analyzes
why live results diverge from backtests.

Usage:
    python diagnose_live.py                    # last 90 days
    python diagnose_live.py --days 180        # last 180 days
    python diagnose_live.py --symbol EURUSDm   # single symbol
"""
import sys
import os
import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))
log = logging.getLogger("Diagnostics")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

MAGIC = 20260101


def fetch_all_trades(days: int):
    """Fetch all deals with our magic number."""
    if not mt5.initialize():
        log.error(f"MT5 init failed: {mt5.last_error()}")
        return []

    info = mt5.terminal_info()
    log.info(f"Connected to: {info.name if info else 'Unknown'}")

    from_date = datetime.now(timezone.utc) - timedelta(days=days)
    to_date   = datetime.now(timezone.utc)

    deals = mt5.history_deals_get(from_date, to_date)
    if not deals:
        log.warning("No deals found in period.")
        return []

    # Filter by our magic number
    our_deals = [d for d in deals if d.magic == MAGIC]
    log.info(f"Found {len(our_deals)} deals with magic={MAGIC} in last {days} days")
    return our_deals


def reconstruct_trades(deals):
    """
    Reconstruct full trades from deals.
    Each trade: entry deal + exit deal (SL/TP/timeout/manual).
    """
    positions = {}
    trades = []

    # Sort by time
    deals_sorted = sorted(deals, key=lambda d: d.time)

    for deal in deals_sorted:
        pos_id = deal.position_id

        if pos_id not in positions:
            positions[pos_id] = {
                "position_id": pos_id,
                "symbol":      deal.symbol,
                "entry_time":  datetime.fromtimestamp(deal.time, tz=timezone.utc),
                "entry_price": deal.price,
                "volume":     deal.volume,
                "deal_type":  deal.type,
                "deals":      [],
            }

        positions[pos_id]["deals"].append(deal)

        # Deal type OUT means position closed
        if deal.entry == mt5.DEAL_ENTRY_OUT:
            pos = positions[pos_id]
            exit_time   = datetime.fromtimestamp(deal.time, tz=timezone.utc)
            exit_price  = deal.price
            profit      = sum(d.profit + d.swap + d.commission for d in pos["deals"])

            # Determine exit reason from comment
            comment = deal.comment.lower()
            if "tp" in comment or "take" in comment:
                exit_reason = "TP"
            elif "sl" in comment or "stop" in comment:
                exit_reason = "SL"
            elif "timeout" in comment or "time" in comment:
                exit_reason = "TIMEOUT"
            elif "adverse" in comment:
                exit_reason = "ADVERSE"
            elif "breakeven" in comment:
                exit_reason = "BE"
            else:
                exit_reason = comment.upper() or "UNKNOWN"

            trades.append({
                "symbol":      pos["symbol"],
                "entry_time":  pos["entry_time"],
                "exit_time":   exit_time,
                "entry_price": pos["entry_price"],
                "exit_price":  exit_price,
                "volume":      pos["volume"],
                "profit":      profit,
                "exit_reason": exit_reason,
                "duration_h":  (exit_time - pos["entry_time"]).total_seconds() / 3600,
            })

            del positions[pos_id]

    return trades


def analyze_trades(trades: list[dict], symbol_filter=None):
    """Print diagnostic analysis."""
    if symbol_filter:
        trades = [t for t in trades if t["symbol"] == symbol_filter]

    if not trades:
        print("No trades to analyze.")
        return

    df = pd.DataFrame(trades)
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["date"]      = df["entry_time"].dt.date

    wins   = df[df["profit"] > 0]
    losses = df[df["profit"] <= 0]

    print("\n" + "="*70)
    print("  LIVE TRADE DIAGNOSTICS")
    print("="*70)
    print(f"  Total Trades : {len(df)}")
    print(f"  Symbols      : {', '.join(sorted(df['symbol'].unique()))}")
    print(f"  Date Range   : {df['date'].min()} -> {df['date'].max()}")
    print(f"  Win Rate     : {len(wins)/len(df)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total PnL    : ${df['profit'].sum():.2f}")
    print(f"  Avg Win      : ${wins['profit'].mean():.2f}" if len(wins) else "  Avg Win      : N/A")
    print(f"  Avg Loss     : ${losses['profit'].mean():.2f}" if len(losses) else "  Avg Loss     : N/A")
    print("="*70)

    # -- Exit reason breakdown --------------------------------------
    print("\n  +--- Exit Reason Breakdown ------------------------------")
    reason_counts = df["exit_reason"].value_counts()
    for reason, cnt in reason_counts.items():
        pnl = df[df["exit_reason"] == reason]["profit"].sum()
        print(f"  |   {reason:<12} : {cnt:3d} trades  |  PnL: ${pnl:+.2f}")
    print("  |_---------------------------------------------------------")

    # -- Loss analysis -----------------------------------------------
    if len(losses) > 0:
        print("\n  +--- LOSS Analysis ---------------------------------------")
        print(f"  |   Loss Rate     : {len(losses)/len(df)*100:.1f}%")
        print(f"  |   Avg Loss     : ${-losses['profit'].mean():.2f}")
        print(f"  |   Max Loss     : ${-losses['profit'].min():.2f}")
        print(f"  |   Avg Duration : {losses['duration_h'].mean():.1f}h")
        print("\n  |   Losses by Symbol:")
        for sym in sorted(losses["symbol"].unique()):
            sym_losses = losses[losses["symbol"] == sym]
            print(f"  |     {sym:<12}: {len(sym_losses)} losses, ${sym_losses['profit'].sum():.2f}")
        print("\n  |   Losses by Exit Reason:")
        for reason in sorted(losses["exit_reason"].unique()):
            r_losses = losses[losses["exit_reason"] == reason]
            print(f"  |     {reason:<12}: {len(r_losses)} losses, ${r_losses['profit'].sum():.2f}")
        print("  |_---------------------------------------------------------")

    # -- Win analysis ------------------------------------------------
    if len(wins) > 0:
        print("\n  +--- WIN Analysis ----------------------------------------")
        print(f"  |   Win Rate     : {len(wins)/len(df)*100:.1f}%")
        print(f"  |   Avg Win      : ${wins['profit'].mean():.2f}")
        print(f"  |   Max Win      : ${wins['profit'].max():.2f}")
        print(f"  |   Avg Duration : {wins['duration_h'].mean():.1f}h")
        print("  |_---------------------------------------------------------")

    # -- Daily PnL --------------------------------------------------
    print("\n  +--- Daily PnL -------------------------------------------")
    daily = df.groupby("date")["profit"].sum().sort_index()
    for date, pnl in daily.items():
        bar = "█" * int(pnl / 2) if pnl > 0 else "░" * int(-pnl / 2)
        print(f"  |   {str(date):>10} : ${pnl:+.2f}  {bar}")
    print("  |_---------------------------------------------------------")

    # -- Session analysis --------------------------------------------
    df["hour"] = df["entry_time"].dt.hour + df["entry_time"].dt.minute / 60.0
    print("\n  +--- Performance by Session (UTC) ----------------------")
    bins = [(0, 7.5, "Pre-Session"), (7.5, 19.0, "Session"), (19.0, 24, "Post-Session")]
    for lo, hi, label in bins:
        subset = df[(df["hour"] >= lo) & (df["hour"] < hi)]
        if len(subset) > 0:
            wr = len(subset[subset["profit"] > 0]) / len(subset) * 100
            print(f"  |   {label:<15}: {len(subset):3d} trades, {wr:.1f}% WR, ${subset['profit'].sum():+.2f}")
        else:
            print(f"  |   {label:<15}: No trades")
    print("  |_---------------------------------------------------------")

    return df


def check_spread_at_entry(trades: list[dict]):
    """Check spread conditions at time of entry for recent trades."""
    print("\n  +--- Spread Check (last 10 trades) ----------------------")
    for t in sorted(trades, key=lambda x: x["entry_time"], reverse=True)[:10]:
        tick = mt5.copy_ticks_range(
            t["symbol"],
            t["entry_time"] - timedelta(seconds=10),
            t["entry_time"] + timedelta(seconds=10),
            mt5.COPY_TICKS_ALL,
        )
        if tick is not None and len(tick) > 0:
            avg_spread = sum(tick["ask"] - tick["bid"]) / len(tick)
            print(f"  |   {t['symbol']:<10} {str(t['entry_time'])[:16]} | "
                  f"Spread: {avg_spread:.5f} | PnL: ${t['profit']:+.2f} | {t['exit_reason']}")
        else:
            print(f"  |   {t['symbol']:<10} {str(t['entry_time'])[:16]} | Spread: N/A | PnL: ${t['profit']:+.2f}")
    print("  |_---------------------------------------------------------")


def main():
    parser = argparse.ArgumentParser(description="Diagnose live trade performance")
    parser.add_argument("--days",    type=int, default=90,  help="Days of history to scan (default: 90)")
    parser.add_argument("--symbol", type=str, default=None, help="Filter by symbol")
    args = parser.parse_args()

    deals = fetch_all_trades(args.days)
    if not deals:
        return

    trades = reconstruct_trades(deals)
    if not trades:
        print("No completed trades found.")
        return

    df = analyze_trades(trades, args.symbol)
    check_spread_at_entry(trades)

    # Save to CSV for further analysis
    out_path = Path(__file__).parent / "live_trades.csv"
    pd.DataFrame(trades).to_csv(out_path, index=False)
    print(f"\n  [OK] Full trade log saved to {out_path}")


if __name__ == "__main__":
    main()
