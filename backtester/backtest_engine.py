"""
TTFM Backtesting Engine v2.1
─────────────────────────────────────────────────────────────────────────────
Mirrors the live engine.py [v8.1] CMP strategy with surgical execution fixes:

  Fix A — H4 Displacement Filter   (body ≥ 40% of range — was 60%)
  Fix B — H4 Zone Freshness        (last 30 H4 candles = ~5 days — was 12)
  Fix C — Momentum Extreme Breaker (skip entry if 6 consecutive M5 bars
                                    all one direction AND move > 2×ATR14)
  Fix D — Bar-Close Confirmation   (entry on sweep + close confirmation)

  Note: Fix E (per-asset rate limit) is live-trading only; it is not
  applied in backtest so we can measure raw strategy signal quality.
"""

from __future__ import annotations
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple, Dict
import requests
import sys, os

import numpy as np
import pandas as pd


log = logging.getLogger("Backtester")


# ─────────────────────────────────────────────────────────────────────────
#  DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class OHLCZone:
    entry:      float
    sl:         float
    tp:         float
    is_bullish: bool
    score:      int
    factors:    dict
    creation_idx: int
    active:     bool = True
    pnl_usd:    float = 0.0

@dataclass
class Trade:
    symbol:     str
    direction:  str          # 'buy' or 'sell'
    entry_time: datetime
    entry:      float
    sl:         float
    tp:         float
    rr:         float
    score:      int
    factors:    dict
    entry_bar_idx: int = 0   # bar index at signal time — used for timeout calc
    sl_moved_to_be: bool = False # Track if Stop Loss was moved to Breakeven
    tp1_hit: bool = False        # Track if 50% of TP was reached
    tp2_hit: bool = False        # Track if 75% of TP was reached
    trail_sl_active: bool = False # Track if 85% of TP was reached
    current_weight: float = 1.0  # Remaining position size weight
    
    exit_time:  Optional[datetime] = None
    exit_price: Optional[float]    = None
    outcome:    str = "OPEN"       # 'WIN', 'LOSS', 'TIMEOUT', 'BREAKEVEN', 'HALF-WIN'
    pnl_r:      float = 0.0        # profit in R-multiples
    pnl_usd:    float = 0.0        # profit in USD (based on risk_usd)


@dataclass
class BacktestResult:
    symbol:      str
    trades:      list[Trade]        = field(default_factory=list)
    total:       int                = 0
    wins:        int                = 0
    losses:      int                = 0
    timeouts:    int                = 0
    win_rate:    float              = 0.0
    avg_rr_win:  float              = 0.0
    avg_rr_loss: float              = 0.0
    expectancy:  float              = 0.0
    total_pnl_r: float              = 0.0
    total_pnl_usd: float            = 0.0
    max_drawdown_r: float           = 0.0
    sharpe_ratio: float             = 0.0


# ─────────────────────────────────────────────────────────────────────────
#  HELPER MATH (mirrors strategy/engine.py helpers exactly)
# ─────────────────────────────────────────────────────────────────────────

def _ema(data: np.ndarray, period: int) -> np.ndarray:
    k   = 2.0 / (period + 1)
    out = np.zeros_like(data, dtype=float)
    out[0] = data[0]
    for i in range(1, len(data)):
        out[i] = data[i] * k + out[i - 1] * (1.0 - k)
    return out


def _get_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(highs) < period + 1:
        return 0.0
    tr_list = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, len(highs))
    ]
    return sum(tr_list[-period:]) / period


def _compute_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """
    Proper Wilder ADX using +DI / -DI.
    Returns a value in [0, 100].
    """
    needed = period * 3 + 1
    if len(highs) < needed:
        return 0.0

    # Use at most last 300 bars for speed
    h = highs[-300:]
    l = lows[-300:]
    c = closes[-300:]
    n = len(h)

    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    tr_arr   = np.zeros(n)

    for i in range(1, n):
        up_move   = float(h[i] - h[i - 1])
        down_move = float(l[i - 1] - l[i])
        plus_dm[i]  = up_move   if (up_move > down_move and up_move > 0)   else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr_arr[i]   = max(h[i] - l[i],
                          abs(h[i] - c[i - 1]),
                          abs(l[i] - c[i - 1]))

    # Wilder smoothing: seed = sum of first `period` values, then rolling
    def _wilder(arr: np.ndarray, p: int) -> np.ndarray:
        out = np.zeros(len(arr))
        if len(arr) < p + 1:
            return out
        out[p] = arr[1:p + 1].sum()           # seed
        for j in range(p + 1, len(arr)):
            out[j] = out[j - 1] - (out[j - 1] / p) + arr[j]
        return out

    smooth_tr  = _wilder(tr_arr,    period)
    smooth_pdm = _wilder(plus_dm,   period)
    smooth_mdm = _wilder(minus_dm,  period)

    # +DI and -DI (percentage)
    with np.errstate(divide='ignore', invalid='ignore'):
        pdi = np.where(smooth_tr > 0, 100.0 * smooth_pdm / smooth_tr, 0.0)
        mdi = np.where(smooth_tr > 0, 100.0 * smooth_mdm / smooth_tr, 0.0)
        # DX
        denom = pdi + mdi
        dx    = np.where(denom > 0, 100.0 * np.abs(pdi - mdi) / denom, 0.0)

    # ADX = Wilder smooth of DX
    adx_arr = _wilder(dx, period)
    raw = float(adx_arr[-1])
    return max(0.0, min(100.0, raw))




def _last_pivot_high(highs: np.ndarray, left: int, right: int, cap: int) -> tuple[Optional[float], int]:
    latest = len(highs) - right - 1
    for i in range(latest, left - 1, -1):
        if (latest - i) > cap:
            break
        pivot = highs[i]
        if (all(highs[i - j] < pivot for j in range(1, left + 1)) and
                all(highs[i + j] < pivot for j in range(1, right + 1))):
            return float(pivot), i
    return None, 0


def _last_pivot_low(lows: np.ndarray, left: int, right: int, cap: int) -> tuple[Optional[float], int]:
    latest = len(lows) - right - 1
    for i in range(latest, left - 1, -1):
        if (latest - i) > cap:
            break
        pivot = lows[i]
        if (all(lows[i - j] > pivot for j in range(1, left + 1)) and
                all(lows[i + j] > pivot for j in range(1, right + 1))):
            return float(pivot), i
    return None, 0


# ─────────────────────────────────────────────────────────────────────────
#  BACKTESTING ENGINE
# ─────────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Replays the TTFM StrategyEngine scoring logic bar-by-bar on historical data.

    Parameters
    ----------
    df_m5 : pd.DataFrame
        M5 OHLCV data for the symbol. Columns: open, high, low, close, tick_volume.
        Index must be UTC-aware datetime.
    df_h1 : pd.DataFrame
        H1 OHLCV data for HTF bias computation.
    df_h4 : pd.DataFrame
        H4 OHLCV data for HTF bias computation.
    symbol : str
        Symbol name (for reporting only).
    left_bars, right_bars : int
        Pivot detection parameters (same as live engine).
    min_rr : float
        Minimum reward-to-risk ratio required for a trade.
    min_score : int
        Minimum composite score (out of 140) to take a trade.
    risk_usd : float
        Fixed risk per trade in USD (used for PnL calc only).
    session_start, session_end : float
        Decimal UTC hours defining the trading session.
    max_pivot_bars : int
        Maximum age of a pivot before it's discarded.
    max_future_bars : int
        Max bars to look forward to resolve TP/SL (trade timeout).
    spread_pts : float
        Fixed spread in price units (used only if 'spread' column absent).
    use_ai: bool
        Whether to enable Kronos/Vibe AI scoring.
    """

    # ── Strategy constants (mirrors engine.py v8.1) ──────────────────────
    MIN_BODY_RATIO    = 0.20   # M5 zone-forming candle min body ratio
    H4_MIN_BODY_RATIO = 0.40   # Fix A: H4 candle must have ≥40% body-to-range
    H4_ZONE_LOOKBACK  = 30     # Fix B: last 30 H4 candles (~5 days)
    MOMENTUM_BARS     = 6      # Fix C: consecutive M5 bars for extreme check
    MOMENTUM_ATR_MULT = 2.0    # Fix C: move must exceed this × ATR14 to block

    def __init__(
        self,
        df_m5:          pd.DataFrame,
        df_h1:          pd.DataFrame,
        df_h4:          pd.DataFrame,
        df_d1:          pd.DataFrame = None,   # accepted but not used (D1 gate removed in v8.1)
        symbol:         str   = "SYMBOL",
        left_bars:      int   = 8,
        right_bars:     int   = 8,
        min_rr:         float = 2.5,
        min_score:      int   = 80,
        risk_usd:       float = 5.0,
        session_start:  float = 7.5,
        session_end:    float = 19.0,
        max_pivot_bars: int   = 120,
        max_future_bars: int  = 96,
        spread_pts:     float = 0.0002,
        use_ai:         bool  = False,
    ):
        self.df      = df_m5.copy()
        self.df_h1   = df_h1.copy()
        self.df_h4   = df_h4.copy()
        self.symbol  = symbol
        self.left    = left_bars
        self.right   = right_bars
        self.min_rr  = min_rr
        self.risk_usd        = risk_usd
        self.session_start   = session_start
        self.session_end     = session_end
        self.max_pivot_bars  = max_pivot_bars
        self.max_future_bars = max_future_bars
        self.spread_pts      = spread_pts
        self.use_ai          = use_ai
        self.min_score       = min_score

        # Ensure UTC index
        if self.df.index.tzinfo is None:
            self.df.index = self.df.index.tz_localize("UTC")
        if self.df_h1.index.tzinfo is None:
            self.df_h1.index = self.df_h1.index.tz_localize("UTC")
        if self.df_h4.index.tzinfo is None:
            self.df_h4.index = self.df_h4.index.tz_localize("UTC")

        # Normalise column names
        for _df in [self.df, self.df_h1, self.df_h4]:
            _df.columns = [c.lower() for c in _df.columns]
            if "tick_volume" in _df.columns and "volume" not in _df.columns:
                _df["volume"] = _df["tick_volume"]

    # ─── Public API ──────────────────────────────────────────────────────

    def run(self) -> BacktestResult:
        result = BacktestResult(symbol=self.symbol)
        trades = []
        open_trade: Optional[Trade] = None
        
        # ── Zone Memory (The CMP Strategy Core) ────────────────────────
        active_zones: List[OHLCZone] = []
        MAX_ZONES = 5

        opens  = self.df["open"].values
        highs  = self.df["high"].values
        lows   = self.df["low"].values
        closes = self.df["close"].values
        times  = self.df.index

        start_bar = self.left + self.right + 50
        total_bars = len(self.df)
        report_step = max(1, total_bars // 20)
        log.info(f"[{self.symbol}] Starting backtest on {total_bars} bars...")

        for i in range(start_bar, total_bars):
            if i % report_step == 0:
                log.info(f"  [{self.symbol}] {100 * i / total_bars:3.0f}% complete...")

            bar_time = times[i]
            
            # ── 1. Manage existing trade ─────────────────────────────
            if open_trade is not None:
                open_trade, closed = self._advance_trade(open_trade, i, highs, lows, times)
                if closed:
                    trades.append(closed)
                    open_trade = None

            # ── 2. Identify New Zones from Bar i-1 (Just Closed) ──────
            sl_h = highs[:i]
            sl_l = lows[:i]
            sl_c = closes[:i]
            sl_o = opens[:i]

            # ─── HTF Fractal Context (H4 Zones) ─────────────────────────
            df_h4_slice = self.df_h4[self.df_h4.index < bar_time]
            htf_zones   = self._get_active_htf_zones(df_h4_slice)

            last_m5_close = float(sl_c[-1])
            valid_bullish_htf = any(
                z["sl"] <= last_m5_close <= z["entry"] for z in htf_zones if z["is_bullish"]
            )
            valid_bearish_htf = any(
                z["entry"] <= last_m5_close <= z["sl"] for z in htf_zones if not z["is_bullish"]
            )

            is_bullish = sl_c[-1] > sl_o[-1]
            is_bearish = sl_c[-1] < sl_o[-1]

            last_o = float(sl_o[-1])
            last_c = float(sl_c[-1])
            last_h = float(sl_h[-1])
            last_l = float(sl_l[-1])
            candle_range = last_h - last_l
            body         = abs(last_c - last_o)
            body_ratio   = body / candle_range if candle_range > 0 else 0

            if body_ratio < self.MIN_BODY_RATIO:
                pass  # Doji — skip zone creation
            elif is_bullish and valid_bullish_htf:
                e = last_o   # M5 open = entry trigger
                s = last_l   # M5 candle low = tight SL
                if (e - s) > 0:
                    active_zones.append(OHLCZone(e, s, e + (e - s) * self.min_rr, True,  100, {}, i - 1))
            elif is_bearish and valid_bearish_htf:
                e = last_o   # M5 open = entry trigger
                s = last_h   # M5 candle high = tight SL
                if (s - e) > 0:
                    active_zones.append(OHLCZone(e, s, e - (s - e) * self.min_rr, False, 100, {}, i - 1))

            if len(active_zones) > MAX_ZONES:
                active_zones.pop(0)

            # ── 3. Check for Retest Entry at Current Bar i ───────────────
            # Deactivate zones whose SL was breached by this bar
            for z in active_zones:
                if z.active:
                    if (z.is_bullish and lows[i] < z.sl) or (not z.is_bullish and highs[i] > z.sl):
                        z.active = False

            # Session filter
            decimal_hour = bar_time.hour + bar_time.minute / 60.0
            in_session   = ("BTC" in self.symbol or "ETH" in self.symbol) or \
                           (self.session_start <= decimal_hour < self.session_end)

            if open_trade is None and in_session:
                for z in active_zones:
                    if not z.active:
                        continue
                    # Fix D: Bar-close confirmation — sweep + rejection required
                    bull_confirmed = z.is_bullish  and lows[i]  <= z.entry and closes[i] > z.entry
                    bear_confirmed = not z.is_bullish and highs[i] >= z.entry and closes[i] < z.entry
                    if bull_confirmed or bear_confirmed:
                        direction = "buy" if z.is_bullish else "sell"
                        # Fix C: Momentum extreme breaker
                        if self._is_momentum_extreme(highs, lows, closes, opens, i, direction):
                            continue
                        open_trade = Trade(
                            symbol=self.symbol,
                            direction=direction,
                            entry_time=bar_time, entry=z.entry,
                            sl=z.sl, tp=z.tp,
                            rr=self.min_rr, score=z.score,
                            factors=z.factors, entry_bar_idx=i,
                        )
                        z.active = False
                        break

        # Close any still-open trade at end-of-data
        if open_trade is not None:
            last_close = float(closes[-1])
            open_trade.exit_time  = times[-1]
            open_trade.exit_price = last_close
            open_trade.outcome    = "TIMEOUT"
            r = open_trade.rr
            # Guard: entry == sl would cause ZeroDivisionError (degenerate trade)
            safe_risk = abs(open_trade.entry - open_trade.sl)
            if safe_risk > 0:
                if open_trade.direction == "buy":
                    open_trade.pnl_r = (last_close - open_trade.entry) / safe_risk
                else:
                    open_trade.pnl_r = (open_trade.entry - last_close) / safe_risk
            else:
                open_trade.pnl_r = 0.0
            open_trade.pnl_usd = open_trade.pnl_r * self.risk_usd
            trades.append(open_trade)

        result.trades = trades
        self._compute_stats(result)
        return result

    # ─── Forward-simulate an open trade ──────────────────────────────────

    def _advance_trade(
        self, trade: Trade, current_bar: int,
        highs: np.ndarray, lows: np.ndarray, times
    ) -> tuple[Optional[Trade], Optional[Trade]]:
        """
        Check if the trade's SL or TP was hit at current_bar.
        Returns (still_open_trade_or_None, closed_trade_or_None).
        """
        h = highs[current_bar]
        l = lows[current_bar]
        t = times[current_bar]

        risk = abs(trade.entry - trade.sl) if not trade.sl_moved_to_be else abs(trade.entry - (trade.entry - trade.entry * 0.001)) # safeguard risk div
        tp_dist = abs(trade.tp - trade.entry)

        # Helper to process hits
        def process_targets(high_price, low_price):
            if trade.direction == "buy":
                # TP1 (50%)
                if not trade.tp1_hit and risk > 0 and high_price >= trade.entry + (tp_dist * 0.5):
                    trade.tp1_hit = True
                    trade.sl = trade.entry
                    trade.sl_moved_to_be = True
                    close_weight = 0.5
                    trade.pnl_r += close_weight * (trade.rr * 0.5)
                    trade.current_weight -= close_weight
                # TP2 (75%)
                if not trade.tp2_hit and risk > 0 and high_price >= trade.entry + (tp_dist * 0.75):
                    trade.tp2_hit = True
                    close_weight = 0.3
                    trade.pnl_r += close_weight * (trade.rr * 0.75)
                    trade.current_weight -= close_weight
                # Trailing SL (85%)
                if not trade.trail_sl_active and risk > 0 and high_price >= trade.entry + (tp_dist * 0.85):
                    trade.trail_sl_active = True
                    trade.sl = trade.entry + (tp_dist * 0.5)  # lock in 50% profit for remainder
            else:
                # TP1 (50%)
                if not trade.tp1_hit and risk > 0 and low_price <= trade.entry - (tp_dist * 0.5):
                    trade.tp1_hit = True
                    trade.sl = trade.entry
                    trade.sl_moved_to_be = True
                    close_weight = 0.5
                    trade.pnl_r += close_weight * (trade.rr * 0.5)
                    trade.current_weight -= close_weight
                # TP2 (75%)
                if not trade.tp2_hit and risk > 0 and low_price <= trade.entry - (tp_dist * 0.75):
                    trade.tp2_hit = True
                    close_weight = 0.3
                    trade.pnl_r += close_weight * (trade.rr * 0.75)
                    trade.current_weight -= close_weight
                # Trailing SL (85%)
                if not trade.trail_sl_active and risk > 0 and low_price <= trade.entry - (tp_dist * 0.85):
                    trade.trail_sl_active = True
                    trade.sl = trade.entry - (tp_dist * 0.5)

        # Process partial targets first
        process_targets(h, l)

        # Now check full SL or full TP
        if trade.direction == "buy":
            if l <= trade.sl:
                trade.exit_time  = t
                trade.exit_price = trade.sl
                
                if trade.trail_sl_active:
                    trade.pnl_r += trade.current_weight * (trade.rr * 0.5)
                    trade.outcome = "WIN"
                elif trade.sl_moved_to_be:
                    trade.outcome = "HALF-WIN"
                else:
                    trade.outcome = "LOSS"
                    trade.pnl_r -= trade.current_weight
                
                trade.pnl_usd = trade.pnl_r * self.risk_usd
                return None, trade

            if h >= trade.tp:
                trade.exit_time  = t
                trade.exit_price = trade.tp
                trade.outcome = "WIN"
                trade.pnl_r += trade.current_weight * trade.rr
                trade.pnl_usd = trade.pnl_r * self.risk_usd
                return None, trade
        else:
            if h >= trade.sl:
                trade.exit_time  = t
                trade.exit_price = trade.sl
                
                if trade.trail_sl_active:
                    trade.pnl_r += trade.current_weight * (trade.rr * 0.5)
                    trade.outcome = "WIN"
                elif trade.sl_moved_to_be:
                    trade.outcome = "HALF-WIN"
                else:
                    trade.outcome = "LOSS"
                    trade.pnl_r -= trade.current_weight

                trade.pnl_usd = trade.pnl_r * self.risk_usd
                return None, trade

            if l <= trade.tp:
                trade.exit_time  = t
                trade.exit_price = trade.tp
                trade.outcome = "WIN"
                trade.pnl_r += trade.current_weight * trade.rr
                trade.pnl_usd = trade.pnl_r * self.risk_usd
                return None, trade

        # Timeout — use stored bar index, no fragile lookup needed
        bars_since_entry = current_bar - trade.entry_bar_idx
        if bars_since_entry >= self.max_future_bars:
            close_px = (highs[current_bar] + lows[current_bar]) / 2.0
            trade.exit_time  = t
            trade.exit_price = close_px
            trade.outcome    = "TIMEOUT"
            if trade.direction == "buy":
                trade.pnl_r = (close_px - trade.entry) / risk if risk > 0 else 0
            else:
                trade.pnl_r = (trade.entry - close_px) / risk if risk > 0 else 0
            trade.pnl_usd = trade.pnl_r * self.risk_usd
            return None, trade

        return trade, None

    def _is_momentum_extreme(
        self,
        highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, opens: np.ndarray,
        bar_idx: int, direction: str
    ) -> bool:
        """
        Fix C: Returns True if the last MOMENTUM_BARS closed bars before bar_idx
        are ALL one-directional AND total range > MOMENTUM_ATR_MULT × ATR14.

        direction='buy'  → checks for bearish crash (we'd be buying into it)
        direction='sell' → checks for bullish spike (we'd be selling into it)
        """
        needed = bar_idx - (self.MOMENTUM_BARS + 2)
        if needed < 20:
            return False

        start = bar_idx - self.MOMENTUM_BARS - 2
        end   = bar_idx - 2

        seg_c = closes[start:end]
        seg_o = opens[start:end]

        if direction == "buy":
            all_directional = all(seg_c[j] < seg_o[j] for j in range(len(seg_c)))
        else:
            all_directional = all(seg_c[j] > seg_o[j] for j in range(len(seg_c)))

        if not all_directional:
            return False

        atr_start = bar_idx - 20
        atr_h = highs[atr_start:bar_idx - 2]
        atr_l = lows[atr_start:bar_idx - 2]
        atr_c = closes[atr_start:bar_idx - 2]
        tr_list = [
            max(atr_h[j] - atr_l[j], abs(atr_h[j] - atr_c[j-1]), abs(atr_l[j] - atr_c[j-1]))
            for j in range(1, len(atr_h))
        ]
        if len(tr_list) < 14:
            return False
        atr14 = sum(tr_list[-14:]) / 14

        seg_h = highs[start:end]
        seg_l = lows[start:end]
        total_move = float(seg_h.max() - seg_l.min())
        return total_move > (self.MOMENTUM_ATR_MULT * atr14)

    def _get_active_htf_zones(self, df_htf: pd.DataFrame) -> List[dict]:
        """
        Scans H4 bars for structural OHLC zones that are:
          - Recent      (last H4_ZONE_LOOKBACK = 30 candles, ~5 days on H4)
          - Displacement(body ≥ H4_MIN_BODY_RATIO = 40% of candle range)
          - Not yet invalidated by SL breach in subsequent bars
        Only reads CONFIRMED closed bars (excludes the last/current open bar).
        """
        zones = []
        n = len(df_htf)
        if n < 2:
            return zones

        confirmed = n - 1
        lookback  = min(self.H4_ZONE_LOOKBACK, confirmed)
        start     = confirmed - lookback

        for pos in range(start, confirmed):
            o = float(df_htf.iloc[pos]["open"])
            h = float(df_htf.iloc[pos]["high"])
            l = float(df_htf.iloc[pos]["low"])
            c = float(df_htf.iloc[pos]["close"])

            candle_range = h - l
            body         = abs(c - o)
            if candle_range <= 0 or (body / candle_range) < self.H4_MIN_BODY_RATIO:
                continue

            if c > o:    # Bullish displacement → support zone (Open to Low)
                zones.append({"entry": o, "sl": l, "is_bullish": True,  "active": True, "pos": pos})
            elif c < o:  # Bearish displacement → resistance zone (Open to High)
                zones.append({"entry": o, "sl": h, "is_bullish": False, "active": True, "pos": pos})

        # 2. Forward pass — deactivate zones whose SL was breached by later bars
        for z in zones:
            for pos in range(z["pos"] + 1, confirmed):
                h = float(df_htf.iloc[pos]["high"])
                l = float(df_htf.iloc[pos]["low"])
                if z["is_bullish"] and l < z["sl"]:
                    z["active"] = False
                    break
                elif not z["is_bullish"] and h > z["sl"]:
                    z["active"] = False
                    break

        return [z for z in zones if z["active"]]

    # ─── Stats ───────────────────────────────────────────────────────────

    def _compute_stats(self, result: BacktestResult):
        trades = result.trades
        if not trades:
            return

        result.total    = len(trades)
        result.wins     = sum(1 for t in trades if t.outcome == "WIN")
        result.losses   = sum(1 for t in trades if t.outcome == "LOSS")
        result.timeouts = sum(1 for t in trades if t.outcome == "TIMEOUT")
        be_trades       = sum(1 for t in trades if t.outcome == "BREAKEVEN")
        
        # Win rate excludes Breakeven trades (as they are scratches)
        decisive_trades = result.wins + result.losses
        result.win_rate = result.wins / decisive_trades if decisive_trades > 0 else 0.0

        win_rr   = [t.pnl_r for t in trades if t.outcome == "WIN"]
        loss_rr  = [t.pnl_r for t in trades if t.outcome == "LOSS"]
        result.avg_rr_win  = float(np.mean(win_rr))  if win_rr  else 0.0
        result.avg_rr_loss = float(np.mean(loss_rr)) if loss_rr else 0.0

        pnl_series = np.cumsum([t.pnl_r for t in trades])
        result.total_pnl_r   = float(pnl_series[-1]) if len(pnl_series) > 0 else 0.0
        result.total_pnl_usd = float(np.sum([t.pnl_usd for t in trades]))

        # Max drawdown (in R)
        peak = pnl_series[0]
        max_dd = 0.0
        for v in pnl_series:
            if v > peak:
                peak = v
            dd = peak - v
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown_r = max_dd

        # Expectancy = (WR * avg_win_R) + ((1-WR) * avg_loss_R)
        result.expectancy = (result.win_rate * result.avg_rr_win) + ((1 - result.win_rate) * result.avg_rr_loss)

        # Sharpe-like ratio (R-series)
        r_series = np.array([t.pnl_r for t in trades])
        if len(r_series) > 1 and np.std(r_series) > 0:
            result.sharpe_ratio = float(np.mean(r_series) / np.std(r_series) * np.sqrt(len(r_series)))
        else:
            result.sharpe_ratio = 0.0

