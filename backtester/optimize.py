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
    # Suggest parameters
    min_score = trial.suggest_int("min_score", 40, 90)
    min_rr    = trial.suggest_float("min_rr", 1.0, 3.5, step=0.1)
    
    # Run backtest
    result = backtest_symbol(
        symbol=symbol,
        days=days,
        min_score=min_score,
        min_rr=min_rr,
        risk_usd=10,  # Fixed risk for optimization
        session_start=3.0,
        session_end=20.0,
        use_ai=True
    )
    
    if result is None or result.total == 0:
        return -10.0  # Penalty for no trades
    
    # Reward based on PnL but favor systems with consistent samples
    # We want at least 1 trade every 2 days on average
    expected_min_trades = days / 2 
    if result.total < expected_min_trades:
        return result.total_pnl_r * (result.total / expected_min_trades)
        
    return result.total_pnl_r

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
        
    symbols = ["EURUSDm", "XAUUSDm", "GBPUSDm", "USDJPYm", "XAGUSDm"]
    final_params = {}
    
    for sym in symbols:
        final_params[sym] = run_optimization(sym, days=30, n_trials=50)
        
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
