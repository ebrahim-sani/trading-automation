import MetaTrader5 as mt5
import sys
import os
sys.path.append('strategy')
from engine import StrategyEngine
import numpy as np

mt5.initialize()
symbols = [
    "EURUSDm", "GBPUSDm", "XAUUSDm",
    "USDJPYm", "USDCADm",
    "EURJPYm", "GBPJPYm",
    "XAGUSDm",
    "BTCUSDm", "ETHUSDm",
]
e = StrategyEngine(symbols=symbols)

print(f"Checking {len(symbols)} symbols for sweeps in the last 60 minutes...")

for symbol in symbols:
    bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 150)
    if bars is None: continue
    
    # Check last 12 bars (60 mins)
    for i in range(len(bars)-12, len(bars)-1):
        sub_bars = bars[:i+1] # All bars up to current candle being checked
        highs = np.array([b['high'] for b in sub_bars])
        lows = np.array([b['low'] for b in sub_bars])
        
        # Look for the last pivot in the window before this candle
        nh, nh_idx = e._last_pivot_high_with_idx(highs[:-1])
        nl, nl_idx = e._last_pivot_low_with_idx(lows[:-1])
        
        if nh is None or nl is None: continue
        
        low_c = sub_bars[-1]['low']
        high_c = sub_bars[-1]['high']
        close_c = sub_bars[-1]['close']
        
        bull_sweep = (low_c < nl and close_c > nl)
        bear_sweep = (high_c > nh and close_c < nh)
        
        if bull_sweep or bear_sweep:
            time_str = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 1)[0]['time']
            # Convert bar time to readable string
            import datetime
            bt = datetime.datetime.fromtimestamp(sub_bars[-1]['time'], tz=datetime.timezone.utc)
            print(f"DETECTED: {symbol} at {bt.strftime('%H:%M')} UTC | Direction: {'LONG' if bull_sweep else 'SHORT'}")

print("Check finished.")
mt5.shutdown()
