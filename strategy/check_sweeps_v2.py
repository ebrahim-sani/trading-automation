import MetaTrader5 as mt5
import sys
import os
from engine import StrategyEngine
import numpy as np
import datetime

mt5.initialize()
symbols = [
    "EURUSDm", "GBPUSDm", "XAUUSDm",
    "USDJPYm", "USDCADm",
    "EURJPYm", "GBPJPYm",
    "XAGUSDm",
    "BTCUSDm", "ETHUSDm",
]
e = StrategyEngine(symbols=symbols)

print(f"Checking {len(symbols)} symbols for sweeps in the last 120 minutes...")

found_any = False
for symbol in symbols:
    bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 150)
    if bars is None: continue
    
    # Check last 24 bars (120 mins)
    for i in range(len(bars)-24, len(bars)-1):
        sub_bars = bars[:i+1] 
        highs = np.array([b['high'] for b in sub_bars])
        lows = np.array([b['low'] for b in sub_bars])
        
        nh, nh_idx = e._last_pivot_high_with_idx(highs[:-1])
        nl, nl_idx = e._last_pivot_low_with_idx(lows[:-1])
        
        if nh is None or nl is None: continue
        
        low_c = sub_bars[-1]['low']
        high_c = sub_bars[-1]['high']
        close_c = sub_bars[-1]['close']
        
        bull_sweep = (low_c < nl and close_c > nl)
        bear_sweep = (high_c > nh and close_c < nh)
        
        if bull_sweep or bear_sweep:
            found_any = True
            bt = datetime.datetime.fromtimestamp(sub_bars[-1]['time'], tz=datetime.timezone.utc)
            print(f"DETECTED: {symbol} at {bt.strftime('%H:%M')} UTC | Direction: {'LONG' if bull_sweep else 'SHORT'}")

if not found_any:
    print("Zero sweeps detected in the market for all 10 symbols in the last 2 hours. Market is likely ranging or moving smoothly.")

print("Check finished.")
mt5.shutdown()
