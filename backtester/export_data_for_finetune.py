import MetaTrader5 as mt5
import pandas as pd
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Full symbol list — mirrors strategy/main.py exactly ─────────────────────
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

def export_symbol_data(symbol: str, days: int = 365, output_dir: Path = None):
    """Fetch M5 bars from MT5 and save a Kronos-formatted CSV.

    MT5 must already be initialised before calling this function.
    """
    utc_to   = datetime.now(timezone.utc)
    utc_from = utc_to - timedelta(days=days)

    # Ensure symbol is visible in Market Watch
    if not mt5.symbol_select(symbol, True):
        print(f"  [SKIP] {symbol} — not available in Market Watch")
        return

    bars = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, utc_from, utc_to)

    if bars is None or len(bars) == 0:
        print(f"  [SKIP] {symbol} — no data returned (error: {mt5.last_error()})")
        return

    df = pd.DataFrame(bars)

    # Format for Kronos: timestamps, open, close, high, low, volume, amount
    df["timestamps"] = pd.to_datetime(df["time"], unit="s").dt.strftime("%Y/%m/%d %H:%M")
    export_df = df[["timestamps", "open", "close", "high", "low", "tick_volume"]].copy()
    export_df.columns = ["timestamps", "open", "close", "high", "low", "volume"]
    export_df["amount"] = 0  # Placeholder — not available from MT5

    out_dir = output_dir or Path(__file__).parent.parent / "Kronos" / "finetune_csv" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"EXNESS_{symbol}_M5_all.csv"
    export_df.to_csv(out_path, index=False)

    print(f"  [OK]   {symbol} — {len(df):,} bars → {out_path}")


if __name__ == "__main__":
    print(f"Exporting {len(ALL_SYMBOLS)} symbols — last 180 days of M5 data for Kronos fine-tuning...\n")

    # ── Init MT5 once for the whole batch ───────────────────────────────────
    if not mt5.initialize():
        print(f"MT5 Init Failed: {mt5.last_error()}")
        sys.exit(1)

    info = mt5.terminal_info()
    print(f"Connected to: {info.name if info else 'Unknown'}\n")

    ok, skipped = 0, 0
    for symbol in ALL_SYMBOLS:
        try:
            before = ok
            export_symbol_data(symbol, days=180)
            # crude check: if the file now exists the export succeeded
            out = Path(__file__).parent.parent / "Kronos" / "finetune_csv" / "data" / f"EXNESS_{symbol}_M5_all.csv"
            if out.exists():
                ok += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  [ERR]  {symbol} — {e}")
            skipped += 1

    # ── Shutdown once at the end ─────────────────────────────────────────────
    mt5.shutdown()

    print(f"\nDone. {ok} exported, {skipped} skipped.")
