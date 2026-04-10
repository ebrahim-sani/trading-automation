import time
import logging
from datetime import datetime, time as dtime
from typing import Optional
import MetaTrader5 as mt5
import numpy as np
from journal_client import JournalClient
from executor import MT5Executor

log = logging.getLogger("Engine")

TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M3":  mt5.TIMEFRAME_M3,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
}

class StrategyEngine:
    def __init__(
        self,
        symbols:                list,
        timeframe_entry:        str   = "M5",
        left_bars:              int   = 5,       # Pine: leftBars
        right_bars:             int   = 5,       # Pine: rightBars
        min_rr:                 float = 2.0,     # Pine: minRR
        risk_usd:               float = 5.0,
        trade_timeout_minutes:  int   = 60,
        max_open_trades:        int   = 3,
        max_pivot_bars:         int   = 80,
    ):
        self.symbols            = symbols
        self.tf_entry           = TF_MAP[timeframe_entry]
        self.left_bars          = left_bars
        self.right_bars         = right_bars
        self.min_rr             = min_rr
        self.risk_usd           = risk_usd
        self.trade_timeout_min  = trade_timeout_minutes
        self.max_open_trades    = max_open_trades
        self.max_pivot_bars     = max_pivot_bars

        self.executor  = MT5Executor()
        self.journal   = JournalClient()

        # Tracks the last bar we processed per symbol
        # Prevents re-firing on the same candle (Pine runs once per bar close)
        self.last_bar_time: dict = {}

    # ─────────────────────────────────────────────────────────────────
    def run(self):
        log.info("TTFM Engine started")
        if not self.executor.init():
            return

        log.info(f"Symbols : {', '.join(self.symbols)}")
        log.info("Session : 24/7 Active")
        log.info(f"Min RR  : {self.min_rr}  |  Risk: ${self.risk_usd}")

        # Ensure all symbols are visible in Market Watch
        for symbol in self.symbols:
            if not mt5.symbol_select(symbol, True):
                log.error(f"Failed to select {symbol} in MT5 Market Watch.")

        while True:
            try:
                # Guard: Daily Loss Limit
                today_pnl = self.journal.get_today_pnl()
                if today_pnl <= -(self.risk_usd * 3):
                    log.warning(f"DAILY LIMIT HIT: PnL is {today_pnl:.2f}. See you tomorrow.")
                    time.sleep(300)
                    continue

                # Manage timeouts / breakeven on every loop tick
                self.executor.manage_open_trades(self.trade_timeout_min, self.journal)

                # 24/7 Trading (All sessions active)
                for symbol in self.symbols:
                    self._process_symbol(symbol)

                time.sleep(5)

            except KeyboardInterrupt:
                log.info("Shutdown requested")
                break
            except Exception as e:
                log.error(f"Loop error: {e}", exc_info=True)
                if mt5.terminal_info() is None:
                    log.warning("Connection lost. Attempting to re-initialize MT5...")
                    mt5.initialize()
                time.sleep(10)

        mt5.shutdown()

    # ─────────────────────────────────────────────────────────────────
    def _process_symbol(self, symbol: str):
        # ── Fetch bars ───
        bars = mt5.copy_rates_from_pos(symbol, self.tf_entry, 0, 300)
        if bars is None or len(bars) < self.left_bars + self.right_bars + 10:
            log.warning(f"{symbol}: not enough bars")
            return

        # ── Only process once per bar ────────
        closed_bar_time = int(bars[-2]["time"])
        if self.last_bar_time.get(symbol) == closed_bar_time:
            return
        self.last_bar_time[symbol] = closed_bar_time

        # Extract arrays
        closes = np.array([b["close"] for b in bars])
        highs  = np.array([b["high"]  for b in bars])
        lows   = np.array([b["low"]   for b in bars])

        close_c = closes[-2]
        high_c  = highs[-2]
        low_c   = lows[-2]
        prev_close = closes[-3]

        # ── HTF Bias: 200 EMA on H1 and H4 ───────────────────────────
        bias_1h, ema_1h_val = self._get_htf_bias(symbol, mt5.TIMEFRAME_H1)
        bias_4h, ema_4h_val = self._get_htf_bias(symbol, mt5.TIMEFRAME_H4)

        # ── Fractal Pivots ────────────────────────────────────────────
        last_high = self._last_pivot_high(highs[:-1])
        last_low  = self._last_pivot_low(lows[:-1])

        if last_high is None or last_low is None:
            return

        # ── ATR (Guard: Structural Safety) ───────────────────────
        # Using 1.2 ATR guarantees the SL survives normal M5 candle wicks 
        # (fixes the 1:11.4 tight-stop wipeouts).
        atr = self._get_atr(bars[:-1], 14)
        min_sl_dist = atr * 1.2

        info      = mt5.symbol_info(symbol)
        sl_offset = info.point * 10

        # ═════════════════════════════════════════════════════════════
        # BULL LOGIC
        # ═════════════════════════════════════════════════════════════
        potential_bull = low_c < last_low and close_c > last_low
        confirmed_bull = (
            prev_close  <= last_low and
            close_c     >  last_low and
            low_c       <  last_low
        )

        if potential_bull and not confirmed_bull:
            log.info(f"{symbol}: Anticipating BULL (Yellow Dot on TV)")

        bull_sl   = low_c - sl_offset
        bull_risk = close_c - bull_sl
        bull_tp   = last_high
        
        # Guard Check: Matching Pine's 'raw' stops more closely
        if confirmed_bull and bull_risk < min_sl_dist:
            log.info(f"  → Bull {symbol}: SL too tight for noise. Widening for safety.")
            bull_sl = close_c - min_sl_dist
            bull_risk = min_sl_dist

        bull_rr = (bull_tp - close_c) / bull_risk if bull_risk > 0 else 0
        valid_bull = confirmed_bull and bull_rr >= self.min_rr
        
        if confirmed_bull and not valid_bull:
             log.info(f"  → Bull {symbol}: Discarded (RR 1:{bull_rr:.2f} is below {self.min_rr})")

        # ═════════════════════════════════════════════════════════════
        # BEAR LOGIC
        # ═════════════════════════════════════════════════════════════
        potential_bear = high_c > last_high and close_c < last_high
        confirmed_bear = (
            prev_close >= last_high and
            close_c    <  last_high and
            high_c     >  last_high
        )

        if potential_bear and not confirmed_bear:
            log.info(f"{symbol}: Anticipating BEAR (Yellow Dot on TV)")

        bear_sl   = high_c + sl_offset
        bear_risk = bear_sl - close_c
        bear_tp   = last_low
        
        if confirmed_bear and bear_risk < min_sl_dist:
            log.info(f"  → Bear {symbol}: SL too tight for noise. Widening for safety.")
            bear_sl = close_c + min_sl_dist
            bear_risk = min_sl_dist

        bear_rr = (close_c - bear_tp) / bear_risk if bear_risk > 0 else 0
        valid_bear = confirmed_bear and bear_rr >= self.min_rr

        if confirmed_bear and not valid_bear:
             log.info(f"  → Bear {symbol}: Discarded (RR 1:{bear_rr:.2f} is below {self.min_rr})")

        # ── Fire signals ──────────────────────────────────────────────
        if valid_bull:
            self._handle_signal(symbol, "buy", close_c, bull_sl, bull_tp, bull_rr, bias_1h, bias_4h)

        if valid_bear:
            self._handle_signal(symbol, "sell", close_c, bear_sl, bear_tp, bear_rr, bias_1h, bias_4h)

    # ─────────────────────────────────────────────────────────────────
    def _handle_signal(
        self,
        symbol, action,
        entry, sl, tp, rr,
        bias_1h, bias_4h
    ):
        """
        Logs signal and executes trade based on alignment:
        - Full Trend (H1 + H4 align): 100% Risk
        - Partial Scalp (H1 or H4 align): 50% Risk
        - Messy (None align): Skip
        """
        is_h1_aligned = (action == "buy" and bias_1h == "BULLISH") or (action == "sell" and bias_1h == "BEARISH")
        is_h4_aligned = (action == "buy" and bias_4h == "BULLISH") or (action == "sell" and bias_4h == "BEARISH")

        # Risk Engine: Execute everything, just scale the size down for danger.
        if is_h1_aligned and is_h4_aligned:
            calculated_risk = self.risk_usd
            trade_label     = "FULL ALIGNMENT"
        else:
            calculated_risk = self.risk_usd * 0.5
            trade_label     = "PARTIAL / COUNTER"

        log.info(
            f"SIGNAL  {symbol} {action.upper()} | {trade_label} | "
            f"Risk: ${calculated_risk:.2f} | RR: 1:{rr:.2f} | 1H: {bias_1h} | 4H: {bias_4h}"
        )

        # Log to journal
        self.journal.log_signal(symbol, action, entry, sl, tp, rr, bias_1h, bias_4h, is_h4_aligned)

        open_count = len(mt5.positions_get() or [])
        if open_count >= self.max_open_trades:
            log.warning(f"  → Skipped: {open_count}/{self.max_open_trades} trades open")
            return

        self.executor.open_trade(
            symbol, action, entry, sl, tp,
            calculated_risk, self.journal
        )

    # ─────────────────────────────────────────────────────────────────
    def _get_htf_bias(self, symbol: str, timeframe) -> tuple:
        """
        Matches Pine: request.security(syminfo.tickerid, "60", ta.ema(close, 200))
        Returns ("BULLISH"|"BEARISH", ema_value)
        """
        bars = mt5.copy_rates_from_pos(symbol, timeframe, 0, 201)
        if bars is None or len(bars) < 201:
            log.warning(f"{symbol}: not enough HTF bars for bias")
            return "UNKNOWN", 0.0

        closes  = np.array([b["close"] for b in bars])
        ema_arr = self._ema(closes, 200)
        current = closes[-2]
        ema_val = ema_arr[-2]

        bias = "BULLISH" if current > ema_val else "BEARISH"
        return bias, ema_val

    def _get_atr(self, bars: np.ndarray, period: int = 14) -> float:
        """
        Calculates ATR (Average True Range).
        Matches Pine: ta.atr(14)
        """
        if len(bars) < period + 1:
            return 0.0

        tr_list = []
        for i in range(1, len(bars)):
            h = bars[i]['high']
            l = bars[i]['low']
            pc = bars[i-1]['close']
            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_list.append(tr)

        # Use simple moving average of TR (Matches most ATR implementations)
        return sum(tr_list[-period:]) / period

    def _ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """
        Matches Pine: ta.ema(close, 200)
        Pine EMA uses multiplier = 2/(length+1), same as standard EMA.
        """
        k   = 2.0 / (period + 1)
        out = np.zeros_like(data, dtype=float)
        out[0] = data[0]
        for i in range(1, len(data)):
            out[i] = data[i] * k + out[i - 1] * (1.0 - k)
        return out

    def _last_pivot_high(self, highs: np.ndarray) -> Optional[float]:
        """
        Improved: Returns the MOST RECENT confirmed pivot high
        that is NOT older than self.max_pivot_bars.
        Matches Pine Script ta.pivothigh() + freshness guard.
        """
        lb = self.left_bars
        rb = self.right_bars
        max_age = self.max_pivot_bars

        # Most recent bar index that can possibly be a confirmed pivot
        latest_possible = len(highs) - rb - 1

        for i in range(latest_possible, lb - 1, -1):
            # Age of this candidate pivot (0 = most recent possible)
            age = latest_possible - i
            if age > max_age:
                break  # No point checking older bars

            pivot = highs[i]
            left_ok  = all(highs[i - j] < pivot for j in range(1, lb + 1))
            right_ok = all(highs[i + j] < pivot for j in range(1, rb + 1))

            if left_ok and right_ok:
                return float(pivot)

        return None

    def _last_pivot_low(self, lows: np.ndarray) -> Optional[float]:
        """
        Improved: Same logic for pivot lows.
        """
        lb = self.left_bars
        rb = self.right_bars
        max_age = self.max_pivot_bars

        latest_possible = len(lows) - rb - 1

        for i in range(latest_possible, lb - 1, -1):
            age = latest_possible - i
            if age > max_age:
                break

            pivot = lows[i]
            left_ok  = all(lows[i - j] > pivot for j in range(1, lb + 1))
            right_ok = all(lows[i + j] > pivot for j in range(1, rb + 1))

            if left_ok and right_ok:
                return float(pivot)

        return None