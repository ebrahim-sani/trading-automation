import time
import logging
import sys
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
import MetaTrader5 as mt5
import numpy as np
import pandas as pd
import torch
import requests

# Add Kronos path to sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "Kronos"))
from model.kronos import Kronos, KronosTokenizer, KronosPredictor

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

CORRELATION_CLUSTERS = {
    "EUR":    ["EURUSDm", "EURJPYm"],
    "GBP":    ["GBPUSDm", "GBPJPYm"],
    "JPY":    ["USDJPYm", "EURJPYm", "GBPJPYm"],
    "METALS": ["XAUUSDm", "XAGUSDm"],
    "BTC":    ["BTCUSDm"],
    "ETH":    ["ETHUSDm"],
}
MAX_CLUSTER_TRADES = 1


class StrategyEngine:
    """
    TTFM Alpha Combiner [v7.1] — Refined Institutional Engine
    ─────────────────────────────────────────────────────────────────────────
    Enhancements added based on engineering critique:
      1. Gradient Scoring  — Factors award 0-20 points smoothly, avoiding binary cliffs.
      2. Session Filter    — Hard gate to ensure we don't trade during Asian/dead sessions.
      3. Target Ceiling    — Structural TPs are capped to prevent unrealistic RR targets.
      4. Spread Penalty    — Factors in real-time spread into the score.
      5. Cooldown Breaker  — Pauses for 2 hours if 2 consecutive MT5 trades hit SL.
    """

    def __init__(
        self,
        symbols:               list,
        timeframe_entry:       str   = "M5",
        left_bars:             int   = 8,
        right_bars:            int   = 8,
        min_rr:                float = 2.5,
        min_score:             int   = 80,
        risk_usd:              float = 5.0,
        trade_timeout_minutes: int   = 60,
        max_open_trades:       int   = 3,
        max_pivot_bars:        int   = 120,
        session_start_hour:    float = 7.5,    # 07:30 Broker Time target
        session_end_hour:      float = 19.0,   # 19:00 Broker Time
        max_daily_loss_usd:    float = None,
        max_weekly_loss_usd:   float = None,
        max_consecutive_loss:  int   = 2,      # Cooldown after N losses
        cooldown_hours:        int   = 2,      # Cooldown duration
    ):
        self.symbols            = symbols
        self.tf_entry           = TF_MAP[timeframe_entry]
        self.left_bars          = left_bars
        self.right_bars         = right_bars
        self.min_rr             = min_rr
        self.min_score          = min_score
        self.risk_usd           = risk_usd
        self.trade_timeout_min  = trade_timeout_minutes
        self.max_open_trades    = max_open_trades
        self.max_pivot_bars     = max_pivot_bars
        self.session_start      = session_start_hour
        self.session_end        = session_end_hour
        self.max_daily_loss_usd  = max_daily_loss_usd  or risk_usd * 3
        self.max_weekly_loss_usd = max_weekly_loss_usd or risk_usd * 8
        self.max_consec_loss    = max_consecutive_loss
        self.cooldown_hours     = cooldown_hours

        # ── Kronos Foundation Model ──
        log.info("Initialising Kronos Foundation Model...")
        try:
            self.tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
            self.model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
            self.predictor = KronosPredictor(self.model, self.tokenizer)
            log.info("Kronos Model Loaded Successfully")
        except Exception as e:
            log.error(f"Failed to load Kronos: {e}")
            self.predictor = None

        self.executor = MT5Executor()
        self.journal  = JournalClient()

        self.last_bar_time: dict[str, int] = {}

        self.top_liq: dict[str, float] = {}
        self.bot_liq: dict[str, float] = {}
        # Track the bar index where the pool was formed to calculate age
        self.top_liq_idx: dict[str, int] = {}
        self.bot_liq_idx: dict[str, int] = {}

        self._week_pnl_cache: Optional[float] = None
        self._week_pnl_date:  Optional[int]   = None
        
        self._cooldown_until: Optional[datetime] = None

    def run(self):
        log.info("TTFM Alpha Combiner [v7.1] started")
        if not self.executor.init():
            return

        start_h, start_m = int(self.session_start), int((self.session_start % 1) * 60)
        end_h, end_m = int(self.session_end), int((self.session_end % 1) * 60)
        log.info(f"Targeting Session: {start_h:02d}:{start_m:02d} – {end_h:02d}:{end_m:02d} Broker Time")
        
        for symbol in self.symbols:
            if not mt5.symbol_select(symbol, True):
                log.error(f"Failed to select {symbol} in Market Watch.")

        while True:
            try:
                # ── Guard: Cooldown Circuit Breaker ──────────────────────
                if self._cooldown_until and datetime.now(timezone.utc) < self._cooldown_until:
                    # Still in cooldown
                    time.sleep(60)
                    continue
                elif self._cooldown_until:
                    log.info("Cooldown period ended. Resuming trading.")
                    self._cooldown_until = None

                # ── Guard: Daily loss limit ──────────────────────────────
                today_pnl = self.journal.get_today_pnl()
                if today_pnl <= -self.max_daily_loss_usd:
                    log.warning(f"DAILY LIMIT HIT: PnL={today_pnl:.2f}. Pausing.")
                    time.sleep(300)
                    continue

                # ── Guard: Weekly loss limit ─────────────────────────────
                if self._is_weekly_limit_hit():
                    log.warning("WEEKLY LIMIT HIT: Pausing until next week.")
                    time.sleep(600)
                    continue

                # ── Guard: Consecutive Losses ────────────────────────────
                if self._check_consecutive_losses():
                    log.warning(f"{self.max_consec_loss} Consecutive Losses hit. Cooling down for {self.cooldown_hours}h.")
                    self._cooldown_until = datetime.now(timezone.utc) + timedelta(hours=self.cooldown_hours)
                    continue

                self.executor.manage_open_trades(self.trade_timeout_min, self.journal)

                # Heartbeat logging every 60s
                current_time = time.time()
                if not hasattr(self, "_last_heartbeat") or current_time - self._last_heartbeat >= 60:
                    log.info(f"Heartbeat: Scanning {len(self.symbols)} symbols... [Session Active]")
                    self._last_heartbeat = current_time

                for symbol in self.symbols:
                    self._process_symbol(symbol)

                time.sleep(5)

            except KeyboardInterrupt:
                log.info("Shutdown requested")
                break
            except Exception as e:
                log.error(f"Loop error: {e}", exc_info=True)
                if mt5.terminal_info() is None:
                    log.warning("Connection lost — re-initialising MT5…")
                    mt5.initialize()
                time.sleep(10)

        mt5.shutdown()

    def _process_symbol(self, symbol: str):
        bars = mt5.copy_rates_from_pos(symbol, self.tf_entry, 0, 350)
        if bars is None or len(bars) < self.left_bars + self.right_bars + 30:
            return

        closed_bar_time = int(bars[-2]["time"])
        if self.last_bar_time.get(symbol) == closed_bar_time:
            return
        
        # Session check (only process completed bars inside the session window)
        if not self._is_in_session(closed_bar_time):
            self.last_bar_time[symbol] = closed_bar_time
            return

        self.last_bar_time[symbol] = closed_bar_time
        log.info(f"[{symbol}] Processing candle @ {datetime.fromtimestamp(closed_bar_time, tz=timezone.utc).strftime('%H:%M')} — Recalculating Factor Scores...")

        opens  = np.array([b["open"]        for b in bars])
        highs  = np.array([b["high"]        for b in bars])
        lows   = np.array([b["low"]         for b in bars])
        closes = np.array([b["close"]       for b in bars])
        vols   = np.array([b["tick_volume"] for b in bars])

        open_c  = float(opens[-2])
        high_c  = float(highs[-2])
        low_c   = float(lows[-2])
        close_c = float(closes[-2])
        vol_c   = float(vols[-2])
        
        # Track relative bar index for age
        current_idx = len(bars) - 2

        high_prev = float(highs[-3])
        low_prev  = float(lows[-3])

        info = mt5.symbol_info(symbol)

        # ── Safety guard: news spike ─────────────────────────────────
        atr = self._get_atr(bars[:-1], 14)
        if self._is_spike_market(highs[:-1], lows[:-1], atr):
            return

        # ── Update persistent liquidity pools + their age ────────────
        new_high, nh_idx = self._last_pivot_high_with_idx(highs[:-1])
        new_low, nl_idx  = self._last_pivot_low_with_idx(lows[:-1])
        
        if new_high is not None:
            self.top_liq[symbol] = new_high
            self.top_liq_idx[symbol] = current_idx - (len(highs[:-1]) - 1 - nh_idx)
        else:
            self.top_liq.pop(symbol, None)
            self.top_liq_idx.pop(symbol, None)

        if new_low is not None:
            self.bot_liq[symbol] = new_low
            self.bot_liq_idx[symbol] = current_idx - (len(lows[:-1]) - 1 - nl_idx)
        else:
            self.bot_liq.pop(symbol, None)
            self.bot_liq_idx.pop(symbol, None)

        top_liq = self.top_liq.get(symbol)
        bot_liq = self.bot_liq.get(symbol)
        if top_liq is None or bot_liq is None:
            return

        top_age = current_idx - self.top_liq_idx.get(symbol, current_idx)
        bot_age = current_idx - self.bot_liq_idx.get(symbol, current_idx)

        # ── Spread check ─────────────────────────────────────────────
        avg_spread = self._get_avg_spread(symbol)
        current_spread = info.spread

        # ── Compute all 5 factors ────────────────────────────────────
        scores = self._score_factors(
            symbol    = symbol,
            bars      = bars,
            open_c    = open_c,
            high_c    = high_c,
            low_c     = low_c,
            close_c   = close_c,
            vol_c     = vol_c,
            top_liq   = top_liq,
            bot_liq   = bot_liq,
            top_age   = top_age,
            bot_age   = bot_age,
            highs     = highs,
            lows      = lows,
            closes    = closes,
            vols      = vols,
            atr       = atr,
            current_spread = current_spread,
            avg_spread     = avg_spread
        )

        bull_score  = scores["total_bull"]
        bear_score  = scores["total_bear"]
        sweep_bull  = scores["sweep_bull"]
        sweep_bear  = scores["sweep_bear"]
        aie_bull    = scores["aie_bull"]
        aie_bear    = scores["aie_bear"]

        # If Kronos is active, we check if it supports our bias
        # We don't strictly require Kronos (AIE) to be > 0, but it adds +20 points
        valid_long  = bull_score >= self.min_score and sweep_bull > 0
        valid_short = bear_score >= self.min_score and sweep_bear > 0

        signals = []

        if valid_long:
            sl_long = min(low_c, low_prev) - info.point * 10
            risk    = close_c - sl_long
            if risk > 0:
                tp_min  = close_c + risk * self.min_rr
                tp_max  = close_c + risk * (self.min_rr + 1.0) # Realistic ceiling
                
                if top_liq >= tp_min:
                    # Cap the structural target
                    tp_long = min(top_liq, tp_max)
                else:
                    tp_long = tp_min
                    
                rr_long = (tp_long - close_c) / risk

                signals.append(("buy", close_c, sl_long, tp_long, rr_long, bull_score, scores))

        if valid_short:
            sl_short = max(high_c, high_prev) + info.point * 10
            risk     = sl_short - close_c
            if risk > 0:
                tp_min   = close_c - risk * self.min_rr
                tp_max   = close_c - risk * (self.min_rr + 1.0)
                
                if bot_liq <= tp_min:
                    tp_short = max(bot_liq, tp_max)
                else:
                    tp_short = tp_min
                    
                rr_short = (close_c - tp_short) / risk

                signals.append(("sell", close_c, sl_short, tp_short, rr_short, bear_score, scores))

        signals.sort(key=lambda s: s[5], reverse=True)
        for action, entry, sl, tp, rr, score_pct, factor_dict in signals:
            self._handle_signal(symbol, action, entry, sl, tp, rr, score_pct, factor_dict)

    def _score_factors(self, symbol, bars, open_c, high_c, low_c, close_c, vol_c,
                       top_liq, bot_liq, top_age, bot_age,
                       highs, lows, closes, vols, atr, current_spread, avg_spread) -> dict:
        """
        Calculates gradient-based scores for the 5 factors, mitigating binary cliffs.
        """
        # 1. Macro Trend (Gradient based on ADX & Alignment)
        bias_1h, _ = self._get_htf_bias(symbol, mt5.TIMEFRAME_H1)
        bias_4h, _ = self._get_htf_bias(symbol, mt5.TIMEFRAME_H4)
        adx = self._compute_adx(highs[:-1], lows[:-1], closes[:-1], 14)
        
        # Max 20 if aligned and ADX > 40. Scale linearly if ADX is between 20-40.
        trend_strength = max(0.0, min(1.0, (adx - 20) / 20.0))
        trend_pts = 10 + (10 * trend_strength) # Base 10 if aligned, + up to 10 for strength
        
        trend_bull = int(trend_pts) if (bias_1h == "BULLISH" and bias_4h == "BULLISH") else 0
        trend_bear = int(trend_pts) if (bias_1h == "BEARISH" and bias_4h == "BEARISH") else 0

        # 2. Sweep / Reversion (Gradient based on age of pool)
        # Sweeping a 10-bar old pool is better than a 100-bar old irrelevant pool.
        bull_is_sweep = (low_c < bot_liq and close_c > bot_liq)
        bear_is_sweep = (high_c > top_liq and close_c < top_liq)
        
        # Penalty increases as age approaches max_pivot_bars. Keep min 10 pts for valid sweep.
        bull_age_mult = 1.0 - (min(bot_age, 80) / 160.0)
        bear_age_mult = 1.0 - (min(top_age, 80) / 160.0)
        
        sweep_bull = int(20 * bull_age_mult) if bull_is_sweep else 0
        sweep_bear = int(20 * bear_age_mult) if bear_is_sweep else 0

        # 3. Displacement (Gradient based on body/range fraction)
        candle_range = high_c - low_c
        body_size    = abs(close_c - open_c)
        body_frac    = (body_size / candle_range) if candle_range > 0 else 0
        
        # > 50% = valid. Scales to 20 pts at 90% body fraction.
        disp_pts = 0
        if body_frac > 0.5:
            disp_pts = int(10 + (min(1.0, (body_frac - 0.5) / 0.4) * 10))
            
        disp_bull = disp_pts if close_c > open_c else 0
        disp_bear = disp_pts if close_c < open_c else 0

        # 4. ATR Expansion (Gradient based on expansion multiplier)
        current_atr, avg_atr = self._get_atr_expansion(bars, 14, 10)
        expansion_ratio = (current_atr / avg_atr) if avg_atr > 0 else 1.0
        
        # > 1.0 = valid. Scales to 20 pts at 1.5x expansion.
        vol_score = 0
        if expansion_ratio > 1.0:
            vol_score = int(10 + (min(1.0, (expansion_ratio - 1.0) / 0.5) * 10))

        # 5. Volume Spike (Gradient based on spike multiplier)
        recent_vols = vols[-22:-1]
        avg_vol     = float(np.mean(recent_vols)) if len(recent_vols) > 0 else 0.0
        spike_ratio = (vol_c / avg_vol) if avg_vol > 0 else 1.0
        
        # > 1.5x = valid. Scales to 20 pts at 3.0x spike.
        volm_score = 0
        if spike_ratio > 1.5:
            volm_score = int(10 + (min(1.0, (spike_ratio - 1.5) / 1.5) * 10))
            
        # ── Spread Penalty (Microstructure adjustment) ──
        # Deduct up to 10 points if spread is worse than average
        spread_penalty = 0
        if avg_spread > 0 and current_spread > avg_spread:
            spread_penalty = int(min(10, ((current_spread - avg_spread) / avg_spread) * 10))

        # 6. AI Edge (Kronos Foundation Model)
        aie_bull, aie_bear = self._get_aie_score(symbol, bars)

        # 7. Vibe Institutional Consensus (Smart Money Concepts)
        vibe_bull, vibe_bear = self._get_vibe_consensus(symbol, bars)

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

    def _handle_signal(self, symbol, action, entry, sl, tp, rr, score_pct, factors):
        if not self._is_cluster_ok(symbol):
            return

        open_count = len(mt5.positions_get() or [])
        if open_count >= self.max_open_trades:
            return

        risk_fraction   = score_pct / 100.0
        calculated_risk = self.risk_usd * risk_fraction

        log.info(
            f"EXECUTE {symbol} {action.upper()} | "
            f"Score: {score_pct}/100 | Risk: ${calculated_risk:.2f} | RR: 1:{rr:.1f}\n"
            f"  Factors → Trend:{factors.get('trend_'+action.lower(), 0)} "
            f"Sweep:{factors.get('sweep_'+action.lower(), 0)} "
            f"Disp:{factors.get('disp_'+action.lower(), 0)} "
            f"ATR:{factors['vol_score']} Vol:{factors['volm_score']} "
            f"Penalty:-{factors['penalty']}"
        )

        # Log to bridge with new factor payload structure
        fact_payload = {
            "trend": factors.get('trend_'+action.lower(), 0),
            "sweep": factors.get('sweep_'+action.lower(), 0),
            "disp":  factors.get('disp_'+action.lower(), 0),
            "atr":   factors['vol_score'],
            "vol":   factors['volm_score'],
            "aie":   factors.get('aie_'+action.lower(), 0),
            "vibe":  factors.get('vibe_'+action.lower(), 0),
            "penalty": factors['penalty']
        }
        
        self.journal.log_signal(
            symbol, action, entry, sl, tp, rr, "HTF", "HTF", True, 
            score=score_pct, factors=fact_payload
        )
        self.executor.open_trade(
            symbol, action, entry, sl, tp, calculated_risk, self.journal,
            score=score_pct, setup_score=risk_fraction
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  TECHNICAL HELPERS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _is_in_session(self, bar_time: int) -> bool:
        dt = datetime.fromtimestamp(bar_time, tz=timezone.utc)
        decimal_time = dt.hour + (dt.minute / 60.0)
        return self.session_start <= decimal_time < self.session_end

    def _check_consecutive_losses(self) -> bool:
        """
        Queries MT5 for today's closed deals and sees if the last N trades 
        (matching our EA magic number) were all consecutive losses.
        """
        now = datetime.now()
        start = datetime(now.year, now.month, now.day)
        deals = mt5.history_deals_get(start, now)
        if not deals:
            return False
            
        # Filter for TTFM deals (magic 20260101) where the deal was closing a position
        ttfm_closings = [d for d in deals if d.magic == 20260101 and d.entry == mt5.DEAL_ENTRY_OUT]
        if len(ttfm_closings) < self.max_consec_loss:
            return False
            
        # Check last N deals
        recent = ttfm_closings[-self.max_consec_loss:]
        all_losses = all(d.profit < 0 for d in recent)
        
        return all_losses

    def _get_vibe_consensus(self, symbol, bars):
        """
        Calls Vibe-Trading Research API to get institutional (SMC) sentiment.
        Returns vibe_bull, vibe_bear (0 or 20)
        """
        try:
            # We use /runs/direct as a quick way to run a skill without full session state
            # Note: Port 8899 is our Vibe server
            url = "http://localhost:8899/skills/execute"
            
            # Prepare data sample (vibe expects OHLC)
            df_tail = pd.DataFrame(bars).tail(100)
            data_payload = df_tail[['open', 'high', 'low', 'close', 'tick_volume']].to_dict('records')
            
            resp = requests.post(
                url, 
                json={
                    "skill": "smc",
                    "symbol": symbol,
                    "data": data_payload
                },
                timeout=3
            )
            
            if resp.status_code == 200:
                signal = resp.json().get("signal", 0)
                if signal == 1: return 20, 0
                if signal == -1: return 0, 20
            
            return 0, 0
        except Exception:
            # Silent fail - don't let Vibe downtime crash the MT5 executor
            return 0, 0

    def _get_aie_score(self, symbol, bars):
        """
        Uses Kronos Foundation Model to predict the next candle.
        Returns bull_aie, bear_aie (0 or 20)
        """
        if self.predictor is None:
            return 0, 0

        try:
            # Prepare data for Kronos
            df = pd.DataFrame(bars)
            df.rename(columns={'tick_volume': 'volume'}, inplace=True)
            df['timestamps'] = pd.to_datetime(df['time'], unit='s')
            
            # Kronos needs OHLC
            input_df = df[['open', 'high', 'low', 'close', 'volume']].copy()
            # We add a dummy amount column if missing
            input_df['amount'] = input_df['volume'] * input_df['close']
            
            x_ts = df['timestamps']
            
            # Predict just 1 candle ahead
            tf_min = self._get_lookback_minutes()
            y_ts = pd.Series([x_ts.iloc[-1] + timedelta(minutes=tf_min)])
            
            # Limit context to lookback (Max 512 for Kronos)
            lookback = min(len(df), 512)
            pred = self.predictor.predict(
                df=input_df.tail(lookback),
                x_timestamp=x_ts.tail(lookback),
                y_timestamp=y_ts,
                pred_len=1,
                T=1.0,
                verbose=False
            )
            
            p_close = pred['close'].iloc[0]
            curr_close = df['close'].iloc[-1]
            
            # Scale predictive reward
            if p_close > curr_close:
                return 20, 0
            elif p_close < curr_close:
                return 0, 20
            
            return 0, 0
        except Exception as e:
            log.warning(f"Kronos Prediction Error on {symbol}: {e}")
            return 0, 0

    def _get_lookback_minutes(self) -> int:
        for k, v in TF_MAP.items():
            if v == self.tf_entry:
                if k.startswith('M'): return int(k[1:])
                if k.startswith('H'): return int(k[1:]) * 60
        return 5

    def _get_atr_expansion(

        self, bars: np.ndarray, atr_period: int = 14, sma_period: int = 10
    ) -> tuple[float, float]:
        needed = atr_period + sma_period + 3
        if len(bars) < needed:
            return 0.0, 1.0

        confirmed = bars[:-1]
        atr_series = []
        for offset in range(sma_period - 1, -1, -1):
            b = confirmed if offset == 0 else confirmed[:-offset]
            if len(b) >= atr_period + 1:
                atr_series.append(self._get_atr(b, atr_period))

        if not atr_series:
            return 0.0, 1.0

        return atr_series[-1], sum(atr_series) / len(atr_series)

    def _is_spike_market(self, highs: np.ndarray, lows: np.ndarray, atr: float, lookback: int = 10) -> bool:
        if atr <= 0 or len(highs) < lookback:
            return False
        recent_highs = highs[-lookback:]
        recent_lows = lows[-lookback:]
        max_candle_range = float(np.max(recent_highs - recent_lows))
        return max_candle_range > atr * 2.5

    def _get_htf_bias(self, symbol: str, timeframe) -> tuple:
        bars = mt5.copy_rates_from_pos(symbol, timeframe, 0, 201)
        if bars is None or len(bars) < 201:
            return "UNKNOWN", 0.0
        closes  = np.array([b["close"] for b in bars])
        ema_arr = self._ema(closes, 200)
        return ("BULLISH" if closes[-2] > ema_arr[-2] else "BEARISH"), float(ema_arr[-2])

    def _compute_adx(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
        lookback = period * 2
        if len(closes) < lookback + 1: return 0.0
        recent = closes[-lookback:]
        up = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
        down = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])
        total = up + down
        if total < 2: return 0.0
        dominance = max(up, down) / total
        return max(0.0, min(100.0, (dominance - 0.5) * 200.0))

    def _get_atr(self, bars: np.ndarray, period: int = 14) -> float:
        if len(bars) < period + 1: return 0.0
        tr_list = [
            max(bars[i]['high'] - bars[i]['low'],
                abs(bars[i]['high'] - bars[i - 1]['close']),
                abs(bars[i]['low']  - bars[i - 1]['close']))
            for i in range(1, len(bars))
        ]
        return sum(tr_list[-period:]) / period

    def _ema(self, data: np.ndarray, period: int) -> np.ndarray:
        k   = 2.0 / (period + 1)
        out = np.zeros_like(data, dtype=float)
        out[0] = data[0]
        for i in range(1, len(data)):
            out[i] = data[i] * k + out[i - 1] * (1.0 - k)
        return out

    def _last_pivot_high_with_idx(self, highs: np.ndarray) -> tuple[Optional[float], int]:
        lb, rb, cap = self.left_bars, self.right_bars, self.max_pivot_bars
        latest = len(highs) - rb - 1
        for i in range(latest, lb - 1, -1):
            if (latest - i) > cap: break
            pivot = highs[i]
            if (all(highs[i - j] < pivot for j in range(1, lb + 1)) and
                    all(highs[i + j] < pivot for j in range(1, rb + 1))):
                return float(pivot), i
        return None, 0

    def _last_pivot_low_with_idx(self, lows: np.ndarray) -> tuple[Optional[float], int]:
        lb, rb, cap = self.left_bars, self.right_bars, self.max_pivot_bars
        latest = len(lows) - rb - 1
        for i in range(latest, lb - 1, -1):
            if (latest - i) > cap: break
            pivot = lows[i]
            if (all(lows[i - j] > pivot for j in range(1, lb + 1)) and
                    all(lows[i + j] > pivot for j in range(1, rb + 1))):
                return float(pivot), i
        return None, 0

    def _get_avg_spread(self, symbol: str, bars: int = 100) -> float:
        data = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, bars)
        if data is None or len(data) == 0: return 0.0
        return float(np.mean([b["spread"] for b in data]))

    def _is_weekly_limit_hit(self) -> bool:
        current_week = datetime.now().isocalendar()[1]
        if self._week_pnl_date != current_week:
            self._week_pnl_date, self._week_pnl_cache = current_week, None
        if self._week_pnl_cache is None:
            try: self._week_pnl_cache = self.journal.get_week_pnl()
            except Exception: return False
        return (self._week_pnl_cache or 0.0) <= -self.max_weekly_loss_usd

    def _is_cluster_ok(self, symbol: str) -> bool:
        open_symbols = [p.symbol for p in (mt5.positions_get() or [])]
        for name, members in CORRELATION_CLUSTERS.items():
            if symbol not in members: continue
            if sum(1 for s in open_symbols if s in members) >= MAX_CLUSTER_TRADES:
                log.info(f"  Cluster '{name}' at max — reject {symbol}")
                return False
        return True