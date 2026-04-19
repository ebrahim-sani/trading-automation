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

def objective(trial, symbol, days):
    # Suggest parameters - Floor 60 for Gold safety, Ceiling 80 for Action
    min_score = trial.suggest_int("min_score", 60, 80)
    min_rr    = trial.suggest_float("min_rr", 1.5, 3.5, step=0.1)
    
    # Run backtest
    result = backtest_symbol(
        symbol=symbol,
        days=days,
        min_score=min_score,
        min_rr=min_rr,
        risk_usd=10,
        session_start=3.0,
        session_end=20.0,
        use_ai=True
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
    if not mt5.initialize():
        print("MT5 Init Failed")
        sys.exit(1)
        
    symbols = ["EURUSDm", "XAUUSDm"]
    final_params = {}
    
    for sym in symbols:
        final_params[sym] = run_optimization(sym, days=60, n_trials=50)
        
    print("\n" + "="*50)
    print("FINAL OPTIMIZED PARAMETERS")
    print("="*50)
    for sym, params in final_params.items():
        print(f"{sym}: {params}")
    print("="*50)
    
    # Save to file for Live Engine to consume
    import json
    with open("backtester/optimized_params.json", "w") as f:
        json.dump(final_params, f, indent=4)
    print(f"Results saved to backtester/optimized_params.json")
    
    mt5.shutdown()
