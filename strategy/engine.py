import time
import logging
import sys
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List
import MetaTrader5 as mt5
import numpy as np
import pandas as pd
import torch
import requests
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vibe-trading", "agent", ".env")
load_dotenv(env_path)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or "YOUR_" in TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.getLogger("Engine").error(f"Telegram Error: {e}")

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
    TTFM Alpha Combiner [v8.1] — CMP with Surgical Execution Filters
    ─────────────────────────────────────────────────────────────────────────
    Root-cause analysis of the 7-consecutive-loss streak identified that
    only two EXECUTION issues were at fault:
      (a) Multiple entries on the same asset in minutes (churn)
      (b) Entering during extreme, sustained momentum moves (crashes/spikes)

    The v8.0 D1 EMA200 gate was too blunt — CMP is a zone-based mean-reversion
    strategy, not a trend-following one. It trades both sides at institutional
    levels. The D1 EMA gate eliminated 91% of valid setups and dropped gold
    (XAUUSDm) to 0 trades entirely.

    v8.1 replaces the three over-aggressive v8.0 filters with targeted fixes:

      Fix A — H4 Displacement Quality Filter (body ≥ 40%):
        Only strong H4 candles qualify as zones. 40% is strict enough to
        exclude doji/spinning-tops while allowing valid institutional candles
        in volatile assets like Gold and Crypto that naturally have larger wicks.

      Fix B — H4 Zone Freshness (last 30 candles = ~5 days):
        Zones older than 5 days on H4 are low-probability. 30 candles is a
        meaningful cutoff without discarding same-week valid zones.

      Fix C — Momentum Extreme Breaker (replaces D1 gate):
        If the last 6 consecutive closed M5 bars (30 minutes) are ALL in
        one direction AND the total range exceeds 2× ATR14, the market is
        in a momentum extreme. Entries against that move are skipped.
        This catches crashes/spikes precisely without eliminating counter-
        trend CMP setups in normal conditions.

      Fix D — Bar-Close Confirmation (sweep + rejection, kept from v8.0):
        Entries fire only on M5 bar close confirming sweep+rejection.
        Bull: bar LOW ≤ zone.entry AND bar CLOSE > zone.entry
        Bear: bar HIGH ≥ zone.entry AND bar CLOSE < zone.entry

      Fix E — Per-Asset Rate Limit 3h (kept from v8.0):
        After any trade fires on an asset, that asset is blocked 3 hours.
        Prevents the churn pattern (US500 hit 5× in one hour).
    """

    # ── Strategy constants ───────────────────────────────────────────────
    H4_MIN_BODY_RATIO  = 0.40   # Fix A: H4 candle ≥40% body-to-range (was 60%)
    H4_ZONE_LOOKBACK   = 30     # Fix B: last 30 H4 candles (~5 days, was 12)
    M5_MIN_BODY_RATIO  = 0.20   # existing: M5 zone-forming candle min body ratio
    ZONE_MAX_AGE_HOURS = 24     # LTF zones older than 24h are deactivated
    ASSET_COOLDOWN_HRS = 3      # Fix E: hours before same asset can trade again
    MOMENTUM_BARS      = 6      # Fix C: consecutive M5 bars to check for extreme
    MOMENTUM_ATR_MULT  = 2.0    # Fix C: move must exceed this × ATR14 to block

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
        session_start_hour:    float = 7.5,
        session_end_hour:      float = 19.0,
        max_daily_loss_usd:    float = None,
        max_weekly_loss_usd:   float = None,
        max_consecutive_loss:  int   = 2,
        cooldown_hours:        int   = 2,
    ):
        self.symbols             = symbols
        self.tf_entry            = TF_MAP[timeframe_entry]
        self.left_bars           = left_bars
        self.right_bars          = right_bars
        self.min_rr              = min_rr
        self.min_score           = min_score
        self.risk_usd            = risk_usd
        self.trade_timeout_min   = trade_timeout_minutes
        self.max_open_trades     = max_open_trades
        self.max_pivot_bars      = max_pivot_bars
        self.session_start       = session_start_hour
        self.session_end         = session_end_hour
        self.max_daily_loss_usd  = max_daily_loss_usd  or risk_usd * 3
        self.max_weekly_loss_usd = max_weekly_loss_usd or risk_usd * 8
        self.max_consec_loss     = max_consecutive_loss
        self.cooldown_hours      = cooldown_hours

        # ── Timeframe definitions ────────────────────────────────────────
        self.tf_htf1 = mt5.TIMEFRAME_H1
        self.tf_htf2 = mt5.TIMEFRAME_H4
        self.tf_d1   = mt5.TIMEFRAME_D1   # Fix 1: daily trend

        # ── Optimized Parameters DNA ─────────────────────────────────────
        self.symbol_configs: dict[str, dict] = {}
        self._load_optimized_params()

        self.executor = MT5Executor()
        self.journal  = JournalClient()

        self.last_bar_time: dict[str, int] = {}

        self._week_pnl_cache: Optional[float] = None
        self._week_pnl_date:  Optional[int]   = None

        self._cooldown_until: Optional[datetime] = None

        # ── CMP Zone Memory (5 LTF zones per symbol) ────────────────────
        self.active_zones: dict[str, list[dict]] = {s: [] for s in self.symbols}
        self.MAX_ZONES = 5

        # ── D1 Trend Cache (refreshed every 4 hours, expensive fetch) ────
        self._d1_cache: dict[str, dict] = {}

        # ── Per-Asset Rate Limiter ────────────────────────────────────────
        self._asset_last_trade: dict[str, datetime] = {}

    # ─────────────────────────────────────────────────────────────────────
    #  STARTUP & MAIN LOOP
    # ─────────────────────────────────────────────────────────────────────

    def _load_optimized_params(self):
        path = os.path.join(os.path.dirname(__file__), "..", "backtester", "optimized_params.json")
        if os.path.exists(path):
            try:
                import json
                with open(path, "r") as f:
                    self.symbol_configs = json.load(f)
                log.info(f"Loaded optimized parameters for {len(self.symbol_configs)} symbols")
            except Exception as e:
                log.error(f"Failed to load optimized params: {e}")

    def run(self):
        log.info("TTFM Alpha Combiner [v8.1] started")
        if not self.executor.init():
            return

        start_h, start_m = int(self.session_start), int((self.session_start % 1) * 60)
        end_h,   end_m   = int(self.session_end),   int((self.session_end   % 1) * 60)
        log.info(f"Session target: {start_h:02d}:{start_m:02d} – {end_h:02d}:{end_m:02d} UTC")

        current_time_utc = datetime.now(timezone.utc)
        decimal_now = current_time_utc.hour + (current_time_utc.minute / 60.0)
        in_session   = self.session_start <= decimal_now < self.session_end
        session_status = "✅ ACTIVE" if in_session else "⏳ WAITING FOR SESSION"

        startup_msg = (
            f"🚀 *TTFM ALPHA COMBINER V8.1 INITIALIZED*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🟢 *Status:* {session_status}\n"
            f"🌍 *Session:* `{start_h:02d}:{start_m:02d} – {end_h:02d}:{end_m:02d} UTC`\n"
            f"🎯 *Assets Tracked:* `{len(self.symbols)} active tickers`\n"
            f"🛡️ *Risk Target:* `${self.risk_usd:.2f} per trade`\n"
            f"🧬 *DNA Engine:* `Active ({len(self.symbol_configs)} pairs optimized)`\n"
            f"🔒 *Filters:* `H4 Quality (40%) + Bar-Close + Momentum Breaker + 3h Cooldown`\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"_Awaiting market opportunities..._"
        )
        send_telegram_alert(startup_msg)

        last_heartbeat        = 0
        last_morning_msg_date = ""

        active_symbols = []
        for symbol in self.symbols:
            if mt5.symbol_select(symbol, True):
                active_symbols.append(symbol)
            else:
                log.warning(f"Failed to select {symbol} in Market Watch — removing.")

        self.symbols = active_symbols
        # Initialise zone memory for any symbol added after __init__
        for s in self.symbols:
            if s not in self.active_zones:
                self.active_zones[s] = []

        while True:
            try:
                current_time_utc = datetime.now(timezone.utc)
                decimal_now      = current_time_utc.hour + (current_time_utc.minute / 60.0)
                in_session       = self.session_start <= decimal_now < self.session_end
                today_str        = current_time_utc.strftime("%Y-%m-%d")

                # ── Morning Motivation & Session Briefing ────────────────
                if in_session and last_morning_msg_date != today_str:
                    self._send_morning_greeting(start_h, start_m, end_h, end_m)
                    last_morning_msg_date = today_str

                # ── Heartbeat every 1.5 hours (in session only) ──────────
                if in_session and (time.time() - last_heartbeat > 5400):
                    self._send_heartbeat()
                    last_heartbeat = time.time()

                # ── Guard: Cooldown Circuit Breaker ──────────────────────
                if self._cooldown_until and current_time_utc < self._cooldown_until:
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

                self.executor.manage_open_trades(self.trade_timeout_min, self.journal)

                # Heartbeat log every 60s
                current_time = time.time()
                if not hasattr(self, "_last_heartbeat") or current_time - self._last_heartbeat >= 60:
                    log.info(f"Heartbeat: Scanning {len(self.symbols)} symbols...")
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

    # ─────────────────────────────────────────────────────────────────────
    #  DATA HELPERS
    # ─────────────────────────────────────────────────────────────────────

    def _get_bars(self, symbol: str, timeframe: int, count: int) -> pd.DataFrame:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        return df

    def _calc_ema(self, data: np.ndarray, period: int) -> float:
        """Returns the last EMA value for the given period."""
        if len(data) < 2:
            return float(data[-1]) if len(data) else 0.0
        k   = 2.0 / (period + 1)
        ema = float(data[0])
        for v in data[1:]:
            ema = float(v) * k + ema * (1.0 - k)
        return ema

    def _is_momentum_extreme(self, df_m5: pd.DataFrame, direction: str) -> bool:
        """
        Fix C: Returns True when the last MOMENTUM_BARS closed M5 candles are ALL
        one-directional AND the total range exceeds MOMENTUM_ATR_MULT × ATR14.

        direction='buy'  → guards against a bearish crash (would be trading against it)
        direction='sell' → guards against a bullish spike (would be trading against it)
        """
        needed = self.MOMENTUM_BARS + 16
        if len(df_m5) < needed:
            return False

        bars   = df_m5.iloc[-(self.MOMENTUM_BARS + 2):-2]
        closes = bars["close"].values.astype(float)
        opens  = bars["open"].values.astype(float)

        if direction == "buy":
            all_directional = all(closes[i] < opens[i] for i in range(len(closes)))
        else:
            all_directional = all(closes[i] > opens[i] for i in range(len(closes)))

        if not all_directional:
            return False

        atr_bars = df_m5.iloc[-20:-2]
        atr_h    = atr_bars["high"].values.astype(float)
        atr_l    = atr_bars["low"].values.astype(float)
        atr_c    = atr_bars["close"].values.astype(float)
        tr_list  = [
            max(atr_h[i] - atr_l[i], abs(atr_h[i] - atr_c[i-1]), abs(atr_l[i] - atr_c[i-1]))
            for i in range(1, len(atr_h))
        ]
        if len(tr_list) < 14:
            return False
        atr14 = sum(tr_list[-14:]) / 14

        total_move = float(bars["high"].values.max() - bars["low"].values.min())
        return total_move > (self.MOMENTUM_ATR_MULT * atr14)

    # ─────────────────────────────────────────────────────────────────────
    #  D1 TREND GATE (kept for reference, no longer called by _process_symbol)
    # ─────────────────────────────────────────────────────────────────────

    def _get_d1_trend(self, symbol: str) -> str:
        """
        Returns 'bullish', 'bearish', or 'neutral'.
        Cached per symbol for 4 hours — D1 EMA200 moves slowly.
        'neutral' is returned only when data is insufficient.
        """
        cache = self._d1_cache.get(symbol)
        now   = datetime.now(timezone.utc)
        if cache and now < cache["expires"]:
            return cache["trend"]

        df = self._get_bars(symbol, self.tf_d1, 220)
        if df.empty or len(df) < 50:
            log.warning(f"[{symbol}] D1 trend: insufficient data — defaulting to neutral")
            return "neutral"

        closes    = df["close"].values.astype(float)
        ema200    = self._calc_ema(closes, 200)
        last_close = float(closes[-1])

        trend = "bullish" if last_close > ema200 else "bearish"
        self._d1_cache[symbol] = {
            "trend":   trend,
            "expires": now + timedelta(hours=4),
        }
        log.info(
            f"[{symbol}] D1 Trend: {trend.upper()} "
            f"(close={last_close:.4f} vs EMA200={ema200:.4f})"
        )
        return trend

    # ─────────────────────────────────────────────────────────────────────
    #  CORE STRATEGY: ZONE DETECTION & MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────

    def _process_symbol(self, symbol: str):
        # ── Fetch bars ───────────────────────────────────────────────────
        df_m5 = self._get_bars(symbol, self.tf_entry, 150)
        df_h4 = self._get_bars(symbol, self.tf_htf2, 150)

        if df_m5.empty or df_h4.empty:
            return

        closed_bar_time = int(df_m5.iloc[-2].name.timestamp())
        if self.last_bar_time.get(symbol) == closed_bar_time:
            return

        # Session check (only process completed bars inside the session window)
        if not self._is_in_session(symbol, closed_bar_time):
            self.last_bar_time[symbol] = closed_bar_time
            return

        self.last_bar_time[symbol] = closed_bar_time

        if len(df_m5) < 5 or len(df_h4) < 15:
            return

        # ── Get active H4 zones ──────────────────────────────────────────
        htf_zones = self._get_active_htf_zones(df_h4)

        # ── Fix D: Bar-Close Retest Confirmation ─────────────────────────
        # Entry requires: low ≤ entry AND close > entry (bull sweep+rejection)
        #                 high ≥ entry AND close < entry (bear sweep+rejection)
        last_closed = df_m5.iloc[-2]
        bar_h  = float(last_closed["high"])
        bar_l  = float(last_closed["low"])
        bar_c  = float(last_closed["close"])

        for zone in self.active_zones.get(symbol, []):
            if not zone["active"]:
                continue

            # Age gate — deactivate stale zones
            age_hours = (datetime.now(timezone.utc) - zone["creation_time"]).total_seconds() / 3600
            if age_hours > self.ZONE_MAX_AGE_HOURS:
                zone["active"] = False
                log.debug(f"[{symbol}] Zone @ {zone['entry']:.5f} expired ({age_hours:.1f}h old)")
                continue

            # SL breach: deactivate if the closed bar broke through the zone SL
            if zone["is_bullish"] and bar_l < zone["sl"]:
                zone["active"] = False
                log.debug(f"[{symbol}] Bull zone @ {zone['entry']:.5f} invalidated (bar_l={bar_l:.5f} < sl={zone['sl']:.5f})")
                continue
            if not zone["is_bullish"] and bar_h > zone["sl"]:
                zone["active"] = False
                log.debug(f"[{symbol}] Bear zone @ {zone['entry']:.5f} invalidated (bar_h={bar_h:.5f} > sl={zone['sl']:.5f})")
                continue

            # Entry confirmation: sweep + close on correct side
            if zone["is_bullish"] and bar_l <= zone["entry"] and bar_c > zone["entry"]:
                if self._is_momentum_extreme(df_m5, "buy"):
                    log.info(f"[{symbol}] Momentum extreme — skipping bull entry")
                    continue
                risk = zone["entry"] - zone["sl"]
                rr   = round((zone["tp"] - zone["entry"]) / risk, 1) if risk > 0 else 0
                log.info(
                    f"[{symbol}] ✅ BULL RETEST CONFIRMED @ {zone['entry']:.5f} "
                    f"| SL: {zone['sl']:.5f} | TP: {zone['tp']:.5f} | RR: 1:{rr}"
                )
                self._handle_signal(
                    symbol, "buy", zone["entry"], zone["sl"], zone["tp"],
                    rr, zone["score"], zone["factors"]
                )
                zone["active"] = False

            elif not zone["is_bullish"] and bar_h >= zone["entry"] and bar_c < zone["entry"]:
                if self._is_momentum_extreme(df_m5, "sell"):
                    log.info(f"[{symbol}] Momentum extreme — skipping bear entry")
                    continue
                risk = zone["sl"] - zone["entry"]
                rr   = round((zone["entry"] - zone["tp"]) / risk, 1) if risk > 0 else 0
                log.info(
                    f"[{symbol}] ✅ BEAR RETEST CONFIRMED @ {zone['entry']:.5f} "
                    f"| SL: {zone['sl']:.5f} | TP: {zone['tp']:.5f} | RR: 1:{rr}"
                )
                self._handle_signal(
                    symbol, "sell", zone["entry"], zone["sl"], zone["tp"],
                    rr, zone["score"], zone["factors"]
                )
                zone["active"] = False

        # ── New LTF Zone Creation from the just-closed M5 bar ────────────
        open_c  = float(last_closed["open"])
        high_c  = float(last_closed["high"])
        low_c   = float(last_closed["low"])
        close_c = float(last_closed["close"])

        # ── Determine H4 zone alignment ──────────────────────────────────
        valid_bullish_htf = any(
            z["sl"] <= close_c <= z["entry"] for z in htf_zones if z["is_bullish"]
        )
        valid_bearish_htf = any(
            z["entry"] <= close_c <= z["sl"] for z in htf_zones if not z["is_bullish"]
        )

        # ── M5 candle body quality filter ────────────────────────────────
        is_bullish   = close_c > open_c
        is_bearish   = close_c < open_c
        candle_range = high_c - low_c
        body         = abs(close_c - open_c)
        body_ratio   = body / candle_range if candle_range > 0 else 0

        # ── Symbol-level DNA params ───────────────────────────────────────
        config = self.symbol_configs.get(symbol, {})
        min_rr = round(config.get("min_rr", self.min_rr), 1)

        new_zone = None

        if body_ratio < self.M5_MIN_BODY_RATIO:
            pass  # Doji — skip

        elif is_bullish and valid_bullish_htf:
            entry = open_c
            sl    = low_c    # M5 candle low — tight SL, reachable TP
            risk  = entry - sl
            if risk > 0:
                new_zone = {
                    "entry":         entry,
                    "sl":            sl,
                    "tp":            entry + (risk * min_rr),
                    "is_bullish":    True,
                    "score":         100,
                    "factors":       {},
                    "active":        True,
                    "creation_time": datetime.now(timezone.utc),
                }
                log.info(
                    f"[{symbol}] 🟢 LTF SUPPORT ZONE @ {entry:.5f} "
                    f"| SL: {sl:.5f} | TP: {new_zone['tp']:.5f} | RR: 1:{min_rr}"
                )

        elif is_bearish and valid_bearish_htf:
            entry = open_c
            sl    = high_c   # M5 candle high — tight SL, reachable TP
            risk  = sl - entry
            if risk > 0:
                new_zone = {
                    "entry":         entry,
                    "sl":            sl,
                    "tp":            entry - (risk * min_rr),
                    "is_bullish":    False,
                    "score":         100,
                    "factors":       {},
                    "active":        True,
                    "creation_time": datetime.now(timezone.utc),
                }
                log.info(
                    f"[{symbol}] 🔴 LTF RESISTANCE ZONE @ {entry:.5f} "
                    f"| SL: {sl:.5f} | TP: {new_zone['tp']:.5f} | RR: 1:{min_rr}"
                )

        if new_zone:
            if symbol not in self.active_zones:
                self.active_zones[symbol] = []
            self.active_zones[symbol].append(new_zone)
            if len(self.active_zones[symbol]) > self.MAX_ZONES:
                self.active_zones[symbol].pop(0)

    # ─────────────────────────────────────────────────────────────────────
    #  H4 ZONE IDENTIFICATION (DISPLACEMENT + FRESHNESS)
    # ─────────────────────────────────────────────────────────────────────

    def _get_active_htf_zones(self, df_htf: pd.DataFrame) -> List[dict]:
        """
        Scans H4 bars for structural OHLC zones that are:
          - Recent (last 30 candles = ~5 days on H4)
          - Strong displacement candles (body ≥ 40% of range)
          - Not yet invalidated by SL breach
        """
        zones = []
        if len(df_htf) < 2:
            return zones

        lookback = min(self.H4_ZONE_LOOKBACK, len(df_htf) - 1)

        for i in range(len(df_htf) - lookback, len(df_htf) - 1):
            o = float(df_htf.iloc[i]["open"])
            h = float(df_htf.iloc[i]["high"])
            l = float(df_htf.iloc[i]["low"])
            c = float(df_htf.iloc[i]["close"])

            candle_range = h - l
            body         = abs(c - o)
            if candle_range <= 0 or (body / candle_range) < self.H4_MIN_BODY_RATIO:
                continue

            if c > o:    # Bullish displacement → support zone (Open to Low)
                zones.append({"entry": o, "sl": l, "is_bullish": True,  "active": True, "idx": i})
            elif c < o:  # Bearish displacement → resistance zone (Open to High)
                zones.append({"entry": o, "sl": h, "is_bullish": False, "active": True, "idx": i})

        # Forward pass — deactivate zones whose SL was breached by later bars
        for z in zones:
            for i in range(z["idx"] + 1, len(df_htf)):
                h = float(df_htf.iloc[i]["high"])
                l = float(df_htf.iloc[i]["low"])
                if z["is_bullish"] and l < z["sl"]:
                    z["active"] = False
                    break
                elif not z["is_bullish"] and h > z["sl"]:
                    z["active"] = False
                    break

        return [z for z in zones if z["active"]]

    # ─────────────────────────────────────────────────────────────────────
    #  EXECUTION
    # ─────────────────────────────────────────────────────────────────────

    def _handle_signal(self, symbol, action, entry, sl, tp, rr, score_pct, factors):
        # ── Fix 6: Per-asset rate limit (3-hour cooldown) ────────────────
        last_trade = self._asset_last_trade.get(symbol)
        if last_trade is not None:
            elapsed     = (datetime.now(timezone.utc) - last_trade).total_seconds()
            cooldown_s  = self.ASSET_COOLDOWN_HRS * 3600
            if elapsed < cooldown_s:
                remaining = (cooldown_s - elapsed) / 3600
                log.info(f"[{symbol}] Rate limited — {remaining:.1f}h cooldown remaining")
                return

        if not self._is_cluster_ok(symbol):
            return

        open_count = len(mt5.positions_get() or [])
        if open_count >= self.max_open_trades:
            return

        risk_fraction   = score_pct / 100.0
        calculated_risk = self.risk_usd * risk_fraction
        calculated_risk = max(calculated_risk, self.risk_usd * 0.5)

        log.info(
            f"EXECUTE {symbol} {action.upper()} | "
            f"Risk: ${calculated_risk:.2f} | RR: 1:{rr:.1f}"
        )

        self.journal.log_signal(
            symbol, action, entry, sl, tp, rr, "D1+H4", "D1+H4", True,
            score=score_pct, factors={}
        )
        self.executor.open_trade(
            symbol, action, entry, sl, tp, calculated_risk, self.journal,
            score=score_pct, setup_score=risk_fraction
        )

        # Set rate limit after trade attempt — prevents repeat entries win or loss
        self._asset_last_trade[symbol] = datetime.now(timezone.utc)

    # ─────────────────────────────────────────────────────────────────────
    #  TECHNICAL HELPERS
    # ─────────────────────────────────────────────────────────────────────

    def _is_in_session(self, symbol: str, bar_time: int) -> bool:
        if "BTC" in symbol or "ETH" in symbol:
            return True
        dt           = datetime.fromtimestamp(bar_time, tz=timezone.utc)
        decimal_time = dt.hour + (dt.minute / 60.0)
        return self.session_start <= decimal_time < self.session_end

    def _check_consecutive_losses(self) -> bool:
        now   = datetime.now()
        start = datetime(now.year, now.month, now.day)
        deals = mt5.history_deals_get(start, now)
        if not deals:
            return False
        ttfm_closings = [d for d in deals if d.magic == 20260101 and d.entry == mt5.DEAL_ENTRY_OUT]
        if len(ttfm_closings) < self.max_consec_loss:
            return False
        recent    = ttfm_closings[-self.max_consec_loss:]
        all_losses = all(d.profit < 0 for d in recent)
        return all_losses

    def _is_weekly_limit_hit(self) -> bool:
        current_week = datetime.now().isocalendar()[1]
        if self._week_pnl_date != current_week:
            self._week_pnl_date, self._week_pnl_cache = current_week, None
        if self._week_pnl_cache is None:
            try:
                self._week_pnl_cache = self.journal.get_week_pnl()
            except Exception:
                return False
        return (self._week_pnl_cache or 0.0) <= -self.max_weekly_loss_usd

    def _is_cluster_ok(self, symbol: str) -> bool:
        open_symbols = [p.symbol for p in (mt5.positions_get() or [])]
        for name, members in CORRELATION_CLUSTERS.items():
            if symbol not in members:
                continue
            if sum(1 for s in open_symbols if s in members) >= MAX_CLUSTER_TRADES:
                log.info(f"  Cluster '{name}' at max — reject {symbol}")
                return False
        return True

    # ─────────────────────────────────────────────────────────────────────
    #  TELEGRAM ALERTS
    # ─────────────────────────────────────────────────────────────────────

    def _send_morning_greeting(self, start_h, start_m, end_h, end_m):
        import random
        quotes = [
            "\"The goal of a successful trader is to make the best trades. Money is secondary.\" — Alexander Elder",
            "\"In trading, you have to be defensive and aggressive at the same time.\" — Paul Tudor Jones",
            "\"The stock market is a device for transferring money from the impatient to the patient.\" — Warren Buffett",
            "\"Trading is not for the person who wants to be right. It's for the person who wants to make money.\" — Mark Douglas",
            "\"It's not whether you're right or wrong, but how much money you make when right.\" — George Soros",
            "\"Consistency is the key. Plan your trade and trade your plan.\" — Institutional Wisdom",
            "\"Discipline is doing what needs to be done, even if you don't want to.\" — Unknown",
            "\"Risk comes from not knowing what you're doing.\" — Warren Buffett",
        ]
        msg = (
            f"🌅 *GOOD MORNING, CHAMPION*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📜 _{random.choice(quotes)}_\n\n"
            f"📊 *DAILY BRIEFING*\n"
            f"├─ 🕒 Window: `{start_h:02d}:{start_m:02d} – {end_h:02d}:{end_m:02d} UTC`\n"
            f"├─ 💎 Assets: `{len(self.symbols)} active hunters`\n"
            f"├─ 🛡️ Risk Cap: `${self.max_daily_loss_usd:.2f} max loss`\n"
            f"└─ 🔒 Filters: `H4 Quality (40%) + Bar-Close + Momentum Breaker + 3h Cooldown`\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚀 _Algorithms locked. Let's conquer the markets._"
        )
        send_telegram_alert(msg)

    def _send_heartbeat(self):
        msg = (
            f"💓 *SYSTEM PULSE ALIVE*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🧠 *AI Engine:* Scanning {len(self.symbols)} pairs\n"
            f"🕙 *Timestamp:* `{datetime.now(timezone.utc).strftime('%H:%M')} UTC`\n"
            f"✅ *Status:* `100% Operational`"
        )
        send_telegram_alert(msg)
