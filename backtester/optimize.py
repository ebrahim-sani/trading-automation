import optuna
import logging
import sys
import os
from pathlib import Path
from datetime import datetime
import MetaTrader5 as mt5

# --- Setup Paths ---
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))

from run_backtest import backtest_symbol

# --- Logging ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("Optimizer")

# Suppress Optuna's verbose per-trial logging during setup
optuna.logging.set_verbosity(optuna.logging.WARNING)

def objective(trial, symbol, days):
    # Suggest parameters - Floor 60 for Gold safety, Ceiling 80 for Action
    min_score = trial.suggest_int("min_score", 60, 80)
    min_rr    = trial.suggest_float("min_rr", 1.5, 3.5, step=0.1)
    
    # Run backtest
    # NOTE: use_ai=False here for speed — AI scores (Kronos/Vibe) cannot be
    # reliably replayed from historical bars and make each trial take hours.
    # The optimizer tunes the structural parameters (min_score, min_rr) only.
    result = backtest_symbol(
        symbol=symbol,
        days=days,
        min_score=min_score,
        min_rr=min_rr,
        risk_usd=10,
        session_start=7.5,
        session_end=22.0,
        use_ai=False
    )
    
    if result is None or result.total == 0:
        return -50.0  # Massive penalty for no action
    
    # --- Institutional Frequency Scoring ---
    # Goal: At least 1 trade every 2 days (48 hours)
    target_frequency = days / 2 
    frequency_multiplier = min(1.0, result.total / target_frequency)
    
    # Penalty: If trading less than target, slash the PnL value
    # If trading more than target, we don't give extra bonus (prevents overtrading)
    scaled_pnl = result.total_pnl_r * frequency_multiplier
    
    # Also penalize massive drawdowns (> 10R in 90 days)
    if result.max_drawdown_r > 10.0:
        scaled_pnl *= 0.5

    return scaled_pnl

def run_optimization(symbol, days=30, n_trials=50):
    log.info(f"Starting Genetic Optimization for {symbol} ({days} days)...")
    
    study = optuna.create_study(direction="maximize")
    study.optimize(lambda trial: objective(trial, symbol, days), n_trials=n_trials)
    
    log.info(f"\nOptimization results for {symbol}:")
    log.info(f"  Best PnL: {study.best_value:.2f}R")
    log.info(f"  Best Params: {study.best_params}")
    
    return study.best_params

if __name__ == "__main__":
    import argparse, json

    # ── Full symbol list — mirrors strategy/main.py exactly ─────────────────
    ALL_SYMBOLS = [
        # Forex & Metals
        "EURUSDm", "GBPUSDm", "XAUUSDm",
        "USDJPYm", "USDCADm",
        "EURJPYm", "GBPJPYm",
        "XAGUSDm",
        # Crypto
        "BTCUSDm", "ETHUSDm",
        # Indices (Nasdaq, S&P, Dow, DAX)
        "USTECm", "US500m", "US30m", "DE30m",
        # Stocks
        "AAPLm", "TSLAm", "NVDAm", "MSFTm", "AMZNm",
    ]

    # ── Quick-mode subset — used by npm run setup for speed ─────────────────
    # Covers the 6 highest-liquidity assets; good enough to seed the DNA file.
    QUICK_SYMBOLS = ["EURUSDm", "GBPUSDm", "XAUUSDm", "USDJPYm", "BTCUSDm", "NAS100m"]

    parser = argparse.ArgumentParser(description="TTFM Genetic Parameter Optimizer")
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Override symbol list (e.g. --symbols EURUSDm XAUUSDm). Default: all 19 from main.py",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="Fast setup mode: only optimise the 6 core liquid assets (used by npm run setup)",
    )
    parser.add_argument("--days",   type=int, default=60, help="Days of history per backtest (default: 60)")
    parser.add_argument("--trials", type=int, default=20, help="Optuna trials per symbol (default: 20, use 50-100 for deep search)")
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip symbols already present in optimized_params.json and continue from where you left off",
    )
    args = parser.parse_args()

    # Resolve final symbol list
    if args.symbols:
        symbols = args.symbols
        log.info(f"Using custom symbol list ({len(symbols)}): {symbols}")
    elif args.quick:
        symbols = QUICK_SYMBOLS
        log.info(f"Quick mode — optimising {len(symbols)} core symbols: {symbols}")
    else:
        symbols = ALL_SYMBOLS
        log.info(f"Full optimisation — {len(symbols)} symbols from main.py")

    if not mt5.initialize():
        print("MT5 Init Failed")
        sys.exit(1)

    # ── Load existing params (used for merge + resume logic) ─────────────────
    out_path = Path(__file__).parent / "optimized_params.json"
    existing = {}
    if out_path.exists():
        with open(out_path, "r") as f:
            existing = json.load(f)

    if args.resume and existing:
        already_done = list(existing.keys())
        symbols = [s for s in symbols if s not in already_done]
        log.info(f"Resume mode — skipping {len(already_done)} already-done symbols: {already_done}")
        log.info(f"Remaining: {symbols}")

    for sym in symbols:
        # Skip symbols not available in Market Watch to avoid wasting trials
        if not mt5.symbol_select(sym, True):
            log.warning(f"  {sym} not available in Market Watch — skipping")
            continue
        try:
            params = run_optimization(sym, days=args.days, n_trials=args.trials)
            existing[sym] = params
            # ── Save after every symbol so a crash never loses progress ──────
            with open(out_path, "w") as f:
                json.dump(existing, f, indent=4)
            log.info(f"  [{sym}] Saved. ({len(existing)} symbols in file so far)")
        except Exception as e:
            log.error(f"  [{sym}] Optimization failed — {e}. Skipping and continuing.")

    print("\n" + "="*50)
    print("FINAL OPTIMIZED PARAMETERS")
    print("="*50)
    for sym, params in existing.items():
        print(f"  {sym}: {params}")
    print("="*50)
    print(f"Results saved to {out_path}  ({len(existing)} total symbols)")

    mt5.shutdown()
