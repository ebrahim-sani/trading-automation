"""
v8.2 vs Live Simulation — Mathematical Comparison
==================================================
For each of the 86 live trades, replay with v8.2 logic:
- ATR SL buffer 0.25x (instead of wick-only)
- TP1 at 1R, TP2 at 2R
- Momentum risk reducer

Then compare:
- Live PnL: $-153.44
- v8.2 Simulated PnL: ?
- Win Rate: 29.1% vs ?
"""

import sys
import os
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import MetaTrader5 as mt5
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))
sys.path.insert(0, str(Path(__file__).parent.parent / "Kronos"))
sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("Sim_v8_2")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

MAGIC = 20260101
ATR_BUFFER = 0.25  # v8.2 ATR SL buffer


def fetch_live_trades(days=90):
    """Fetch all completed live trades."""
    if not mt5.initialize():
        log.error(f"MT5 init failed: {mt5.last_error()}")
        return []

    from_date = datetime.now(timezone.utc) - timedelta(days=days)
    to_date = datetime.now(timezone.utc)

    deals = mt5.history_deals_get(from_date, to_date)
    if not deals:
        return []

    our_deals = [d for d in deals if d.magic == MAGIC]

    positions = {}
    trades = []

    for deal in sorted(our_deals, key=lambda d: d.time):
        pos_id = deal.position_id

        if pos_id not in positions:
            order = None
            if deal.order > 0:
                orders = mt5.history_orders_get(ticket=deal.order)
                if orders:
                    order = orders[0]

            positions[pos_id] = {
                "symbol": deal.symbol,
                "entry_time": datetime.fromtimestamp(deal.time, tz=timezone.utc),
                "entry_price": deal.price,
                "volume": deal.volume,
                "sl": order.sl if order else 0.0,
                "tp": order.tp if order else 0.0,
                "deals": [],
            }

        positions[pos_id]["deals"].append(deal)

        if deal.entry == mt5.DEAL_ENTRY_OUT:
            pos = positions[pos_id]
            exit_time = datetime.fromtimestamp(deal.time, tz=timezone.utc)
            profit = sum(d.profit + d.swap + d.commission for d in pos["deals"])

            trades.append({
                "symbol": pos["symbol"],
                "entry_time": pos["entry_time"],
                "exit_time": exit_time,
                "entry_price": pos["entry_price"],
                "exit_price": deal.price,
                "sl": pos["sl"],
                "tp": pos["tp"],
                "volume": pos["volume"],
                "profit": profit,
                "exit_reason": deal.comment,
            })
            del positions[pos_id]

    return trades


def fetch_bars(symbol: str, from_time: datetime, to_time: datetime, timeframe=mt5.TIMEFRAME_M5):
    """Fetch bars for replay."""
    rates = mt5.copy_rates_range(symbol, timeframe, from_time, to_time)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df


def calculate_atr(df: pd.DataFrame, period=14):
    """Calculate ATR from DataFrame."""
    if len(df) < period + 1:
        return 0.0
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    tr_list = []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period if len(tr_list) >= period else 0.0


def simulate_v8_2_for_trade(trade: dict):
    """
    Simulate what v8.2 would have done for this trade.
    Returns dict with simulated outcome.
    """
    symbol = trade["symbol"]
    entry_time = trade["entry_time"]
    entry_price = trade["entry_price"]
    live_sl = trade["sl"]
    live_tp = trade["tp"]
    is_buy = entry_price > live_sl if live_sl > 0 else True

    # Fetch bars: 30 before entry (for ATR), 100 after (for replay)
    from_time = entry_time - timedelta(minutes=30 * 5)
    to_time = entry_time + timedelta(minutes=100 * 5)

    df = fetch_bars(symbol, from_time, to_time)
    if df is None or len(df) == 0:
        return None

    # Find entry index
    entry_idx = None
    for i, t in enumerate(df.index):
        if t >= entry_time:
            entry_idx = i
            break

    if entry_idx is None or entry_idx < 20:
        return None

    # Calculate ATR at entry
    atr = calculate_atr(df.iloc[entry_idx-20:entry_idx+1])
    if atr == 0.0:
        return None

    # v8.2 SL: wick ± 0.25× ATR
    # For buys: SL = low - 0.25× ATR (if we had the candle)
    # We'll use the live SL as the "wick" and add buffer
    if live_sl > 0:
        if is_buy:
            v8_2_sl = live_sl - (atr * ATR_BUFFER)
            sl_distance = entry_price - v8_2_sl
            v8_2_tp_1r = entry_price + sl_distance  # TP at 1R
            v8_2_tp_2r = entry_price + (sl_distance * 2)  # TP at 2R
        else:
            v8_2_sl = live_sl + (atr * ATR_BUFFER)
            sl_distance = v8_2_sl - entry_price
            v8_2_tp_1r = entry_price - sl_distance  # TP at 1R
            v8_2_tp_2r = entry_price - (sl_distance * 2)  # TP at 2R
    else:
        # No SL set in live — use ATR-based
        if is_buy:
            v8_2_sl = entry_price - (atr * 1.0)  # 1× ATR below entry
            sl_distance = entry_price - v8_2_sl
            v8_2_tp_1r = entry_price + sl_distance
            v8_2_tp_2r = entry_price + (sl_distance * 2)
        else:
            v8_2_sl = entry_price + (atr * 1.0)
            sl_distance = v8_2_sl - entry_price
            v8_2_tp_1r = entry_price - sl_distance
            v8_2_tp_2r = entry_price - (sl_distance * 2)

    # Now replay bars after entry to see what happens first: SL, TP1, TP2, or timeout
    bars_to_check = min(96, len(df) - entry_idx - 1)  # Max 96 bars = 8 hours
    
    result = {
        "symbol": symbol,
        "entry_time": entry_time,
        "live_profit": trade["profit"],
        "v8_2_sl": v8_2_sl,
        "v8_2_tp_1r": v8_2_tp_1r,
        "v8_2_tp_2r": v8_2_tp_2r,
        "atr": atr,
        "sl_distance": sl_distance,
        "sl_hit": False,
        "tp1_hit": False,
        "tp2_hit": False,
        "v8_2_profit": 0.0,
        "outcome": "UNKNOWN",
    }

    for i in range(entry_idx + 1, entry_idx + bars_to_check + 1):
        if i >= len(df):
            break

        high = df["high"].iloc[i]
        low = df["low"].iloc[i]
        close = df["close"].iloc[i]

        if is_buy:
            # Check TP2 first (highest priority)
            if not result["tp2_hit"] and high >= v8_2_tp_2r:
                result["tp2_hit"] = True
                result["v8_2_profit"] = sl_distance * 2 * 0.3  # 30% at 2R
                result["outcome"] = "TP2_PARTIAL"

            # Check TP1
            if not result["tp1_hit"] and high >= v8_2_tp_1r:
                result["tp1_hit"] = True
                result["v8_2_profit"] += sl_distance * 1 * 0.5  # 50% at 1R
                result["outcome"] = "TP1_PARTIAL"

            # Check SL
            if low <= v8_2_sl:
                result["sl_hit"] = True
                # If TP1 was hit, we got 50% at 1R, rest lost
                if result["tp1_hit"]:
                    result["v8_2_profit"] += (-sl_distance) * 0.5  # Lose 50% of position
                    result["outcome"] = "TP1_THEN_SL"
                elif result["tp2_hit"]:
                    result["v8_2_profit"] += sl_distance * 2 * 0.3  # Keep TP2 profit
                    result["outcome"] = "TP2_THEN_SL"
                else:
                    result["v8_2_profit"] = -sl_distance
                    result["outcome"] = "SL"
                break

            # If both TP1 and TP2 hit, we're done
            if result["tp1_hit"] and result["tp2_hit"]:
                result["v8_2_profit"] += sl_distance * 2 * 0.2  # Remaining 20% at 2R
                result["outcome"] = "WIN"
                break
        else:
            # Sell logic
            if not result["tp2_hit"] and low <= v8_2_tp_2r:
                result["tp2_hit"] = True
                result["v8_2_profit"] = sl_distance * 2 * 0.3
                result["outcome"] = "TP2_PARTIAL"

            if not result["tp1_hit"] and low <= v8_2_tp_1r:
                result["tp1_hit"] = True
                result["v8_2_profit"] += sl_distance * 1 * 0.5
                result["outcome"] = "TP1_PARTIAL"

            if high >= v8_2_sl:
                result["sl_hit"] = True
                if result["tp1_hit"]:
                    result["v8_2_profit"] += (-sl_distance) * 0.5
                    result["outcome"] = "TP1_THEN_SL"
                elif result["tp2_hit"]:
                    result["v8_2_profit"] += sl_distance * 2 * 0.3
                    result["outcome"] = "TP2_THEN_SL"
                else:
                    result["v8_2_profit"] = -sl_distance
                    result["outcome"] = "SL"
                break

            if result["tp1_hit"] and result["tp2_hit"]:
                result["v8_2_profit"] += sl_distance * 2 * 0.2
                result["outcome"] = "WIN"
                break

    # Timeout (no SL or TP hit in 96 bars)
    if not result["sl_hit"] and not (result["tp1_hit"] and result["tp2_hit"]):
        result["outcome"] = "TIMEOUT"
        # Partial profits if any
        if not result["tp1_hit"] and not result["tp2_hit"]:
            result["v8_2_profit"] = 0.0

    return result


def main():
    print("\n" + "="*70)
    print("  v8.2 vs LIVE SIMULATION — Satoshi Nakamoto Precision")
    print("="*70)

    trades = fetch_live_trades(90)
    if not trades:
        print("  No trades found.")
        return

    print(f"\n  Simulating {len(trades)} trades...\n")

    results = []
    for i, trade in enumerate(trades):
        sim = simulate_v8_2_for_trade(trade)
        if sim:
            results.append(sim)

            # Print high-level comparison
            if sim["sl_hit"]:
                outcome_symbol = "SL"
            elif sim["tp1_hit"] or sim["tp2_hit"]:
                outcome_symbol = "TP"
            else:
                outcome_symbol = "TO"

            print(f"  [{i+1:3d}/{len(trades)}] {trade['symbol']:<10} "
                  f"| Live: ${trade['profit']:+.2f} "
                  f"| v8.2: ${sim['v8_2_profit']:+.2f} [{outcome_symbol}] "
                  f"| ATR={sim['atr']:.5f}")

    # Summary statistics
    print("\n" + "="*70)
    print("  SUMMARY COMPARISON")
    print("="*70)

    live_pnl = sum(t["profit"] for t in trades)
    v8_2_pnl = sum(r["v8_2_profit"] for r in results)

    live_wins = sum(1 for t in trades if t["profit"] > 0)
    v8_2_wins = sum(1 for r in results if r["v8_2_profit"] > 0)
    v8_2_be = sum(1 for r in results if r["v8_2_profit"] == 0.0)  # Timeout/breakeven

    print(f"  Total Trades        : {len(results)}")
    print(f"  Live PnL            : ${live_pnl:+.2f}")
    print(f"  v8.2 Simulated PnL : ${v8_2_pnl:+.2f}")
    print(f"  Live Win Rate       : {live_wins/len(trades)*100:.1f}% ({live_wins}W)")
    print(f"  v8.2 Win Rate      : {v8_2_wins/len(results)*100:.1f}% ({v8_2_wins}W)")
    print(f"  v8.2 Breakeven     : {v8_2_be} trades")

    # Outcome breakdown
    print(f"\n  v8.2 Outcome Breakdown:")
    outcome_counts = {}
    for r in results:
        outcome = r["outcome"]
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    for outcome, cnt in sorted(outcome_counts.items()):
        print(f"    {outcome:<20}: {cnt:3d} trades")

    # SL survival improvement
    live_sl_hits = sum(1 for t in trades if "sl" in t.get("exit_reason", "").lower() or t["profit"] <= 0)
    v8_2_sl_hits = sum(1 for r in results if r["sl_hit"])
    print(f"\n  SL Hit Reduction:")
    print(f"    Live     : {live_sl_hits} SL hits ({live_sl_hits/len(trades)*100:.1f}%)")
    print(f"    v8.2 Sim: {v8_2_sl_hits} SL hits ({v8_2_sl_hits/len(results)*100:.1f}%)")
    print(f"    Improvement: {live_sl_hits - v8_2_sl_hits} fewer SL hits")

    print("\n" + "="*70)

    # Save detailed results
    df = pd.DataFrame(results)
    out_path = Path(__file__).parent / "v8_2_simulation.csv"
    df.to_csv(out_path, index=False)
    print(f"  [OK] Detailed results saved to {out_path}")


if __name__ == "__main__":
    main()
