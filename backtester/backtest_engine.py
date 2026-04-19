"""
TTFM Backtesting Engine v1.0
─────────────────────────────────────────────────────────────────────────────
Replays the StrategyEngine scoring logic bar-by-bar over historical MT5 data
without placing any real trades. Computes trades, PnL, win rate, and RR stats.

Key design decisions:
  - Mirror _score_factors() logic 1-to-1 from strategy/engine.py
  - HTF bias is computed inline using the same bars slice (no extra MT5 call)
  - Spread penalty uses a rolling mean of the 'spread' column (if available)
  - Kronos / Vibe scores default to 0 (cannot be reliably replayed historically)
  - Trades are closed at TP or SL using future OHLC (forward simulation)
  - All timestamps are UTC-aware
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

try:
    from model.kronos import Kronos, KronosTokenizer, KronosPredictor
    import torch
    KRONOS_AVAILABLE = True
except ImportError:
    KRONOS_AVAILABLE = False

log = logging.getLogger("Backtester")


# ─────────────────────────────────────────────────────────────────────────
#  DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────

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

    # Live engine max score  = 140 (Trend20 + Sweep20 + Disp20 + ATR20 + Vol20 + AIE20 + Vibe20)
    # Backtest max score     = 100 (same minus AIE20 + Vibe20 which can't be replayed)
    LIVE_MAX_SCORE      = 140
    BACKTEST_MAX_SCORE  = 100

    def __init__(
        self,
        df_m5:          pd.DataFrame,
        df_h1:          pd.DataFrame,
        df_h4:          pd.DataFrame,
        symbol:         str   = "SYMBOL",
        left_bars:      int   = 8,
        right_bars:     int   = 8,
        min_rr:         float = 2.5,
        min_score:      int   = 80,
        risk_usd:       float = 5.0,
        session_start:  float = 7.5,
        session_end:    float = 19.0,
        max_pivot_bars: int   = 120,
        max_future_bars: int  = 96,    # ~8 hrs on M5
        spread_pts:     float = 0.0002,
        use_ai:         bool  = False,
    ):
        self.df       = df_m5.copy()
        self.df_h1    = df_h1.copy()
        self.df_h4    = df_h4.copy()
        self.symbol   = symbol
        self.left     = left_bars
        self.right    = right_bars
        self.min_rr   = min_rr
        self.risk_usd  = risk_usd
        self.session_start = session_start
        self.session_end   = session_end
        self.max_pivot_bars = max_pivot_bars
        self.max_future_bars = max_future_bars
        self.spread_pts = spread_pts
        self.use_ai = use_ai

        self.min_score = min_score
        
        self.predictor = None
        if self.use_ai:
            if not KRONOS_AVAILABLE:
                log.warning("Kronos modules not found. AI integration will use Vibe only.")
            else:
                try:
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                    
                    # PRIORITY: Load fine-tuned specialist if it exists
                    ft_path = os.path.join(os.path.dirname(__file__), "..", "Kronos", "finetune_csv", "finetuned", f"EXNESS_{self.symbol}_M5", "basemodel", "best_model")
                    
                    if os.path.exists(ft_path):
                        log.info(f"[{self.symbol}] Loading Fine-Tuned Specialist weights...")
                        tok_path = os.path.join(os.path.dirname(ft_path), "..", "tokenizer", "best_model")
                        tok = KronosTokenizer.from_pretrained(tok_path) if os.path.exists(tok_path) else KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
                        mod = Kronos.from_pretrained(ft_path)
                    else:
                        tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
                        mod = Kronos.from_pretrained("NeoQuasar/Kronos-small")
                    
                    self.predictor = KronosPredictor(mod, tok)
                    log.info(f"[{self.symbol}] KronosPredictor loaded on {device}")
                except Exception as e:
                    log.error(f"Failed to load Kronos model: {e}. Running without Kronos.")

        # If not using AI, we auto-scale the threshold down because Kronos (+20) and Vibe (+20) 
        # are excluded from the test matrix. 
        if not self.use_ai:
            if self.min_score > 100:
                self.min_score = 100
                
            orig = self.min_score
            self.min_score = int((self.min_score / 140.0) * 100.0)
            log.info(f"[{symbol}] Score auto-scaled: live {orig}/140 -> backtest {self.min_score}/100 (Kronos+Vibe excluded)")
        else:
            log.info(f"[{symbol}] Running with AI Enabled (Kronos+Vibe). Max score: 140. Min threshold: {self.min_score}")

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
        result   = BacktestResult(symbol=self.symbol)
        trades   = []
        open_trade: Optional[Trade] = None

        opens  = self.df["open"].values
        highs  = self.df["high"].values
        lows   = self.df["low"].values
        closes = self.df["close"].values
        vols   = self.df["volume"].values if "volume" in self.df.columns else np.ones(len(self.df))
        times  = self.df.index

        spreads = (
            self.df["spread"].values
            if "spread" in self.df.columns
            else np.full(len(self.df), self.spread_pts)
        )

        min_bars_needed = self.left + self.right + 50
        log.info(f"[{self.symbol}] Starting backtest on {len(self.df)} bars...")

        persistent_top_liq: Optional[float] = None
        persistent_bot_liq: Optional[float] = None
        persistent_top_idx: int = 0
        persistent_bot_idx: int = 0

        for i in range(min_bars_needed, len(self.df) - 1):
            bar_time = times[i]

            # ── Manage open trade first (forward simulate) ─────────────
            # This MUST run before the session filter so we can close trades during off-hours
            if open_trade is not None:
                open_trade, closed = self._advance_trade(open_trade, i, highs, lows, times)
                if closed:
                    trades.append(closed)
                    open_trade = None

            # ── Session filter ─────────────────────────────────────────
            decimal_hour = bar_time.hour + bar_time.minute / 60.0
            if not (self.session_start <= decimal_hour < self.session_end):
                continue

            # ── Only enter new trades if none open ─────────────────────
            if open_trade is not None:
                continue

            # ── Slice bars up to and including bar i-1 (confirmed candle) ──
            sl_h = highs[:i]
            sl_l = lows[:i]
            sl_c = closes[:i]
            sl_o = opens[:i]
            sl_v = vols[:i]

            # ── Spike filter ────────────────────────────────────────────
            atr = _get_atr(sl_h[-50:], sl_l[-50:], sl_c[-50:], 14)
            if atr <= 0:
                continue
            recent_range = float(np.max(sl_h[-10:] - sl_l[-10:]))
            if recent_range > atr * 2.5:
                continue  # spike market

            # ── Pivot liquidity pools ───────────────────────────────────
            new_high, nh_idx = _last_pivot_high(sl_h, self.left, self.right, self.max_pivot_bars)
            new_low,  nl_idx = _last_pivot_low(sl_l, self.left, self.right, self.max_pivot_bars)

            if new_high is not None:
                persistent_top_liq = new_high
                persistent_top_idx = nh_idx
            if new_low is not None:
                persistent_bot_liq = new_low
                persistent_bot_idx = nl_idx

            if persistent_top_liq is None or persistent_bot_liq is None:
                continue

            top_age = (i - 1) - persistent_top_idx
            bot_age = (i - 1) - persistent_bot_idx

            # ── Current candle values (i-1 = last confirmed bar) ───────
            open_c  = float(sl_o[-1])
            high_c  = float(sl_h[-1])
            low_c   = float(sl_l[-1])
            close_c = float(sl_c[-1])
            vol_c   = float(sl_v[-1])
            high_prev = float(sl_h[-2])
            low_prev  = float(sl_l[-2])

            # ── Spread ──────────────────────────────────────────────────
            current_spread = float(spreads[i - 1])
            avg_spread     = float(np.mean(spreads[max(0, i - 101):i - 1])) if i > 1 else current_spread

            # ── Score ───────────────────────────────────────────────────
            scores = self._score_factors(
                i=i,
                bar_time=bar_time,
                open_c=open_c, high_c=high_c, low_c=low_c, close_c=close_c,
                vol_c=vol_c,
                top_liq=persistent_top_liq, bot_liq=persistent_bot_liq,
                top_age=top_age, bot_age=bot_age,
                highs=sl_h, lows=sl_l, closes=sl_c, vols=sl_v,
                atr=atr,
                current_spread=current_spread, avg_spread=avg_spread,
            )

            bull_score = scores["total_bull"]
            bear_score = scores["total_bear"]
            sweep_bull = scores["sweep_bull"]
            sweep_bear = scores["sweep_bear"]
            trend_bull = scores["trend_bull"]
            trend_bear = scores["trend_bear"]

            valid_long  = bull_score >= self.min_score and (sweep_bull > 0 or trend_bull > 0)
            valid_short = bear_score >= self.min_score and (sweep_bear > 0 or trend_bear > 0)

            # ── Build signals ───────────────────────────────────────────
            signal = None

            if valid_long:
                sl_long_base = min(low_c, np.min(sl_l[-3:])) if len(sl_l) >= 3 else low_c
                # ENFORCE EXACT 1:2 RR + SL BREATHING ROOM
                # Add 10 points (1 pip) breathing room to SL
                pip_size = 0.01 if "JPY" in self.symbol else 0.0001
                sl_long = sl_long_base - pip_size
                risk    = close_c - sl_long
                if risk > 0:
                    tp_long = close_c + risk * 2.0
                    rr = 2.0
                    signal = Trade(
                        symbol=self.symbol, direction="buy",
                        entry_time=bar_time, entry=close_c,
                        sl=sl_long, tp=tp_long, rr=rr,
                        score=bull_score, factors=scores,
                        entry_bar_idx=i,
                    )

            if valid_short and (signal is None or bear_score > bull_score):
                sl_short_base = max(high_c, np.max(sl_h[-3:])) if len(sl_h) >= 3 else high_c
                # Add 10 points (1 pip) breathing room to SL
                pip_size = 0.01 if "JPY" in self.symbol else 0.0001
                sl_short = sl_short_base + pip_size
                risk     = sl_short - close_c
                if risk > 0:
                    tp_short = close_c - risk * 2.0
                    rr = 2.0
                    signal = Trade(
                        symbol=self.symbol, direction="sell",
                        entry_time=bar_time, entry=close_c,
                        sl=sl_short, tp=tp_short, rr=rr,
                        score=bear_score, factors=scores,
                        entry_bar_idx=i,
                    )

            if signal is not None:
                open_trade = signal
                log.debug(
                    f"[{self.symbol}] SIGNAL @ {bar_time} | "
                    f"{signal.direction.upper()} | score={signal.score} | RR={signal.rr:.2f}"
                )

        # Close any still-open trade at end-of-data
        if open_trade is not None:
            last_close = float(closes[-1])
            open_trade.exit_time  = times[-1]
            open_trade.exit_price = last_close
            open_trade.outcome    = "TIMEOUT"
            r = open_trade.rr
            open_trade.pnl_r   = (last_close - open_trade.entry) / (open_trade.entry - open_trade.sl) if open_trade.direction == "buy" else (open_trade.entry - last_close) / (open_trade.sl - open_trade.entry)
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

        if trade.direction == "buy":
            # TP1 & Breakeven logic
            if not trade.tp1_hit and risk > 0:
                midpoint = trade.entry + (trade.tp - trade.entry) * 0.5
                if h >= midpoint:
                    trade.tp1_hit = True
                    trade.sl = trade.entry
                    trade.sl_moved_to_be = True
                    # Partial close: Half position is closed at +1R (if RR was 2)
                    # For a 1:2 trade, midpoint is 1R. 
                    # If we risk 1%, we make 0.5% here.
                    trade.pnl_r += 0.5
                    trade.pnl_usd += self.risk_usd * 0.5

            if l <= trade.sl:
                trade.exit_time  = t
                trade.exit_price = trade.sl
                if trade.tp1_hit:
                    trade.outcome = "HALF-WIN"
                else:
                    trade.outcome = "LOSS"
                    trade.pnl_r   = -1.0
                    trade.pnl_usd = -self.risk_usd
                return None, trade

            if h >= trade.tp:
                trade.exit_time  = t
                trade.exit_price = trade.tp
                if trade.tp1_hit:
                    trade.outcome = "WIN"
                    trade.pnl_r   += 1.0 # The other half closed at +2R total (so +1R for half)
                    trade.pnl_usd += self.risk_usd * 1.0
                else:
                    trade.outcome = "WIN"
                    trade.pnl_r   = trade.rr
                    trade.pnl_usd = trade.rr * self.risk_usd
                return None, trade
        else:  # sell
            # TP1 & Breakeven logic
            if not trade.tp1_hit and risk > 0:
                midpoint = trade.entry - (trade.entry - trade.tp) * 0.5
                if l <= midpoint:
                    trade.tp1_hit = True
                    trade.sl = trade.entry
                    trade.sl_moved_to_be = True
                    trade.pnl_r += 0.5
                    trade.pnl_usd += self.risk_usd * 0.5

            if h >= trade.sl:
                trade.exit_time  = t
                trade.exit_price = trade.sl
                if trade.tp1_hit:
                    trade.outcome = "HALF-WIN"
                else:
                    trade.outcome = "LOSS"
                    trade.pnl_r   = -1.0
                    trade.pnl_usd = -self.risk_usd
                return None, trade

            if l <= trade.tp:
                trade.exit_time  = t
                trade.exit_price = trade.tp
                if trade.tp1_hit:
                    trade.outcome = "WIN"
                    trade.pnl_r   += 1.0
                    trade.pnl_usd += self.risk_usd * 1.0
                else:
                    trade.outcome = "WIN"
                    trade.pnl_r   = trade.rr
                    trade.pnl_usd = trade.rr * self.risk_usd
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

    # ─── Score Factors (mirrors strategy/engine.py _score_factors) ────────

    def _score_factors(
        self, i: int, bar_time: pd.Timestamp,
        open_c, high_c, low_c, close_c, vol_c,
        top_liq, bot_liq, top_age, bot_age,
        highs, lows, closes, vols,
        atr, current_spread, avg_spread,
    ) -> dict:

        # ── 1. Macro Trend ──────────────────────────────────────────────
        bias_1h = self._htf_bias_at(i, self.df_h1)
        bias_4h = self._htf_bias_at(i, self.df_h4)
        adx     = _compute_adx(highs, lows, closes, 14)

        trend_strength = max(0.0, min(1.0, (adx - 20) / 20.0))
        trend_pts = 10 + (10 * trend_strength)

        trend_bull = int(trend_pts) if (bias_1h == "BULLISH" and bias_4h == "BULLISH") else 0
        trend_bear = int(trend_pts) if (bias_1h == "BEARISH" and bias_4h == "BEARISH") else 0

        # ── 2. Sweep / Reversion ────────────────────────────────────────
        recent_lows = lows[-10:]
        recent_highs = highs[-10:]
        bull_is_sweep = (np.min(recent_lows) < bot_liq and close_c > bot_liq)
        bear_is_sweep = (np.max(recent_highs) > top_liq and close_c < top_liq)

        bull_age_mult = 1.0 - (min(bot_age, 80) / 160.0)
        bear_age_mult = 1.0 - (min(top_age, 80) / 160.0)

        sweep_bull = int(20 * bull_age_mult) if bull_is_sweep else 0
        sweep_bear = int(20 * bear_age_mult) if bear_is_sweep else 0

        # ── 3. Displacement ─────────────────────────────────────────────
        candle_range = high_c - low_c
        body_size    = abs(close_c - open_c)
        body_frac    = (body_size / candle_range) if candle_range > 0 else 0

        disp_pts = 0
        if body_frac > 0.5:
            disp_pts = int(10 + (min(1.0, (body_frac - 0.5) / 0.4) * 10))

        disp_bull = disp_pts if close_c > open_c else 0
        disp_bear = disp_pts if close_c < open_c else 0

        # ── 4. ATR Expansion ────────────────────────────────────────────
        sma_p = 10
        atr_series = []
        for offset in range(sma_p - 1, -1, -1):
            end = len(highs) if offset == 0 else -offset
            sl_h = highs[:end] if end != 0 else highs
            sl_l = lows[:end]  if end != 0 else lows
            sl_c = closes[:end] if end != 0 else closes
            if len(sl_h) >= 15:
                atr_series.append(_get_atr(sl_h[-30:], sl_l[-30:], sl_c[-30:], 14))

        current_atr = atr_series[-1] if atr_series else atr
        avg_atr     = float(np.mean(atr_series)) if atr_series else atr
        expansion_ratio = (current_atr / avg_atr) if avg_atr > 0 else 1.0

        vol_score = 0
        if expansion_ratio > 1.0:
            vol_score = int(10 + (min(1.0, (expansion_ratio - 1.0) / 0.5) * 10))

        # ── 5. Volume Spike ─────────────────────────────────────────────
        recent_vols = vols[-22:-1]
        avg_vol     = float(np.mean(recent_vols)) if len(recent_vols) > 0 else 0.0
        spike_ratio = (vol_c / avg_vol) if avg_vol > 0 else 1.0

        volm_score = 0
        if spike_ratio > 1.5:
            volm_score = int(10 + (min(1.0, (spike_ratio - 1.5) / 1.5) * 10))

        # ── Spread Penalty ───────────────────────────────────────────────
        spread_penalty = 0
        if avg_spread > 0 and current_spread > avg_spread:
            spread_penalty = int(min(10, ((current_spread - avg_spread) / avg_spread) * 10))

        # 6. AI Edge (Kronos & Vibe)
        aie_bull, aie_bear = 0, 0
        vibe_bull, vibe_bear = 0, 0
        
        if self.use_ai:
            aie_bull, aie_bear = self._get_aie_score(bar_time)
            vibe_bull, vibe_bear = self._get_vibe_consensus(bar_time)
            
            # Dynamic Ensemble Weighting based on Volatility (ADX)
            # If choppy/ranging (ADX < 25), prioritize Vibe SMC structural logic.
            # If trending (ADX >= 25), prioritize Kronos momentum predictions.
            if adx < 25:
                vibe_bull = int(vibe_bull * 1.5)
                vibe_bear = int(vibe_bear * 1.5)
                aie_bull  = int(aie_bull * 0.5)
                aie_bear  = int(aie_bear * 0.5)
            else:
                vibe_bull = int(vibe_bull * 0.5)
                vibe_bear = int(vibe_bear * 0.5)
                aie_bull  = int(aie_bull * 1.5)
                aie_bear  = int(aie_bear * 1.5)

        total_bull = max(0, trend_bull + sweep_bull + disp_bull + vol_score + volm_score + aie_bull + vibe_bull - spread_penalty)
        total_bear = max(0, trend_bear + sweep_bear + disp_bear + vol_score + volm_score + aie_bear + vibe_bear - spread_penalty)

        return {
            "trend_bull": trend_bull, "trend_bear": trend_bear,
            "sweep_bull": sweep_bull, "sweep_bear": sweep_bear,
            "disp_bull":  disp_bull,  "disp_bear":  disp_bear,
            "vol_score":  vol_score,  "volm_score": volm_score,
            "aie_bull":   aie_bull,   "aie_bear":   aie_bear,
            "vibe_bull":  vibe_bull,  "vibe_bear":  vibe_bear,
            "penalty":    spread_penalty,
            "total_bull": total_bull, "total_bear": total_bear,
        }

    def _htf_bias_at(self, m5_bar_idx: int, df_htf: pd.DataFrame) -> str:
        """Return HTF EMA-200 bias as of the M5 bar's timestamp."""
        bar_time = self.df.index[m5_bar_idx]
        subset = df_htf[df_htf.index <= bar_time]
        if len(subset) < 201:
            return "UNKNOWN"
        closes  = subset["close"].values
        ema_arr = _ema(closes, 200)
        return "BULLISH" if closes[-1] > ema_arr[-1] else "BEARISH"

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

    # ─── AI Fetchers ───────────────────────────────────────────────────────
    
    def _get_vibe_consensus(self, bar_time: pd.Timestamp) -> Tuple[int, int]:
        try:
            url = "http://localhost:8899/skills/execute"
            
            # Extract the bars up to the current bar
            sub_df = self.df.loc[:bar_time].tail(100).copy()
            sub_df.reset_index(inplace=True)
            data_payload = sub_df[['time', 'open', 'high', 'low', 'close', 'volume']].rename(columns={'volume': 'tick_volume'}).to_dict('records')
            
            resp = requests.post(url, json={"skill": "smc", "symbol": self.symbol, "data": data_payload}, timeout=1)
            
            if resp.status_code == 200:
                signal = resp.json().get("signal", 0)
                if signal == 1: return 20, 0
                if signal == -1: return 0, 20
        except Exception:
            pass
        return 0, 0

    def _get_aie_score(self, bar_time: pd.Timestamp) -> Tuple[int, int]:
        if self.predictor is None:
            return 0, 0

        try:
            sub_df = self.df.loc[:bar_time].tail(512).copy()
            sub_df.reset_index(inplace=True)
            sub_df['timestamps'] = sub_df['time']
            sub_df['amount'] = sub_df['volume'] * sub_df['close']
            
            x_ts = sub_df['timestamps']
            y_ts = pd.Series([x_ts.iloc[-1] + timedelta(minutes=5)])
            
            pred = self.predictor.predict(
                df=sub_df[['open', 'high', 'low', 'close', 'volume', 'amount']],
                x_timestamp=x_ts,
                y_timestamp=y_ts
            )
            
            if pred is not None and not pred.empty:
                val = pred.iloc[0]['close']
                current_close = sub_df.iloc[-1]['close']
                if val > current_close: return 20, 0
                if val < current_close: return 0, 20
        except Exception as e:
            log.debug(f"Kronos inference error: {e}")
            
        return 0, 0
