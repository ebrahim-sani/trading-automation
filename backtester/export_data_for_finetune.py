import MetaTrader5 as mt5
import pandas as pd
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

def export_symbol_data(symbol, days=365):
    print(f"Exporting {symbol} for last {days} days...")
    
    # Initialize MT5
    if not mt5.initialize():
        print("MT5 Init Failed")
        return
        
    utc_to = datetime.now(timezone.utc)
    utc_from = utc_to - timedelta(days=days)
    
    # Fetch M5 bars (standard for Kronos)
    bars = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, utc_from, utc_to)
    
    if bars is None or len(bars) == 0:
        print(f"No data found for {symbol}")
        return
        
    df = pd.DataFrame(bars)
    
    # Format according to Kronos requirements: timestamps,open,close,high,low,volume,amount
    # Kronos usually expects timestamps in 'YYYY/MM/DD HH:MM' format based on the example
    df['timestamps'] = pd.to_datetime(df['time'], unit='s').dt.strftime('%Y/%m/%d %H:%M')
    
    export_df = df[['timestamps', 'open', 'close', 'high', 'low', 'tick_volume']]
    export_df.columns = ['timestamps', 'open', 'close', 'high', 'low', 'volume']
    export_df['amount'] = 0  # Placeholder
    
    # Save to Kronos finetune directory
    output_path = Path(__file__).parent.parent / "Kronos" / "finetune_csv" / "data" / f"EXNESS_{symbol}_M5_all.csv"
    export_df.to_csv(output_path, index=False)
    
    print(f"Successfully exported {len(df)} bars to {output_path}")
    mt5.shutdown()

if __name__ == "__main__":
    # Export the main pairs
    pairs = [
        "EURUSDm", "GBPUSDm", "XAUUSDm",
        "USDJPYm", "USDCADm",
        "EURJPYm", "GBPJPYm",
        "XAGUSDm",
        "NAS100m", "US500m", "US30m", "GER40m"
    ]
    for p in pairs:
        try:
            export_symbol_data(p, days=180) # 6 months of data for fine-tuning
        except Exception as e:
            print(f"Skipping {p}: {e}")
