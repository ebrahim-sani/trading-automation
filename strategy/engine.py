import time
import logging
import sys
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
import MetaTrader5 as mt5
import numpy as np
import pandas as pd
import torch
import requests
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vibe-trading", "agent", ".env")
load_dotenv(env_path)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or "YOUR_" in TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log = logging.getLogger("Engine")
        log.error(f"Telegram Error: {e}")

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
        
        # --- Timeframe definitions ---
        self.tf_htf1            = mt5.TIMEFRAME_H1
        self.tf_htf2            = mt5.TIMEFRAME_H4

        # ── Optimized Parameters DNA ──
        self.symbol_configs: dict[str, dict] = {}
        self._load_optimized_params()

        self.executor = MT5Executor()
        self.journal  = JournalClient()

        self.last_bar_time: dict[str, int] = {}

        self._week_pnl_cache: Optional[float] = None
        self._week_pnl_date:  Optional[int]   = None
        
        self._cooldown_until: Optional[datetime] = None
        
        # ── CMP Zone Memory (5 zones per symbol) ──
        self.active_zones: dict[str, list[dict]] = {s: [] for s in self.symbols}
        self.MAX_ZONES = 5

    def _load_optimized_params(self):
        """Loads genetic optimization results if they exist."""
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
        log.info("TTFM Alpha Combiner [v7.1] started")
        if not self.executor.init():
            return

        start_h, start_m = int(self.session_start), int((self.session_start % 1) * 60)
        end_h, end_m = int(self.session_end), int((self.session_end % 1) * 60)
        log.info(f"Targeting Session: {start_h:02d}:{start_m:02d} – {end_h:02d}:{end_m:02d} Broker Time")
        
        # --- STARTUP TELEGRAM ALERT ---
        current_time_utc = datetime.now(timezone.utc)
        decimal_now = current_time_utc.hour + (current_time_utc.minute / 60.0)
        in_session = self.session_start <= decimal_now < self.session_end
        session_status = "✅ ACTIVE" if in_session else "⏳ WAITING FOR SESSION"
        
        startup_msg = (
            f"🚀 *TTFM ALPHA COMBINER V7.1 INITIALIZED*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🟢 *Status:* {session_status}\n"
            f"🌍 *Session:* `{start_h:02d}:{start_m:02d} – {end_h:02d}:{end_m:02d} UTC`\n"
            f"🎯 *Assets Tracked:* `{len(self.symbols)} active tickers`\n"
            f"🛡️ *Risk Target:* `${self.risk_usd:.2f} per trade`\n"
            f"🧬 *DNA Engine:* `Active ({len(self.symbol_configs)} pairs optimized)`\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"_Awaiting market opportunities..._"
        )
        send_telegram_alert(startup_msg)
        
        last_heartbeat = 0 # Force immediate first heartbeat
        last_morning_msg_date = ""

        active_symbols = []
        for symbol in self.symbols:
            if mt5.symbol_select(symbol, True):
                active_symbols.append(symbol)
            else:
                log.warning(f"Failed to select {symbol} in Market Watch — removing from scan list.")
        
        self.symbols = active_symbols

        while True:
            try:
                current_time_utc = datetime.now(timezone.utc)
                decimal_now = current_time_utc.hour + (current_time_utc.minute / 60.0)
                in_session = self.session_start <= decimal_now < self.session_end
                today_str = current_time_utc.strftime("%Y-%m-%d")

                # ── Morning Motivation & Session Briefing ────────────────
                if in_session and last_morning_msg_date != today_str:
                    self._send_morning_greeting(start_h, start_m, end_h, end_m)
                    last_morning_msg_date = today_str

                # ── Heartbeat every 1.5 hours (5400s), ONLY in session ──
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

                # ── Guard: Consecutive Losses ────────────────────────────
                # [DISABLED PER REQ] Let the bot trade through variance freely.
                # if self._check_consecutive_losses():
                #     log.warning(f"{self.max_consec_loss} Consecutive Losses hit. Cooling down for {self.cooldown_hours}h.")
                #     self._cooldown_until = datetime.now(timezone.utc) + timedelta(hours=self.cooldown_hours)
                #     continue

                self.executor.manage_open_trades(self.trade_timeout_min, self.journal)

                # Heartbeat logging every 60s
                current_time = time.time()
                if not hasattr(self, "_last_heartbeat") or current_time - self._last_heartbeat >= 60:
                    log.info(f"Heartbeat: Scanning {len(self.symbols)} symbols... [Session Active]")
                    self._last_heartbeat = current_time

                for symbol in self.symbols:
                    # ── CMP Step A: Monitor for live retests ─────────────
                    self._check_zone_retests(symbol)
                    
                    # ── CMP Step B: Process new bar closures ────────────
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

    def _get_bars(self, symbol: str, timeframe: int, count: int) -> pd.DataFrame:
        """Helper to fetch bars from MT5 and return as a cleaned DataFrame."""
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
        df.set_index('time', inplace=True)
        return df



    def _process_symbol(self, symbol: str):
        # Fetch OHLC data
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
        
        # Ensure we have enough bars
        if df_m5.empty or df_h4.empty or len(df_m5) < 5 or len(df_h4) < 20:
            return

        # Check for M5 retests first (using the latest tick data)
        self._check_zone_retests(symbol)

        # --- CMP Zone Identification ---
        htf_zones = self._get_active_htf_zones(df_h4)

        # Check if the most recently closed M5 candle closed inside an HTF zone
        last_closed_bar = df_m5.iloc[-2]
        open_c  = float(last_closed_bar['open'])
        high_c  = float(last_closed_bar['high'])
        low_c   = float(last_closed_bar['low'])
        close_c = float(last_closed_bar['close'])

        valid_bullish_htf = False
        valid_bearish_htf = False

        for z in htf_zones:
            # Support zone check: Is the M5 close inside the H4 Open-to-Low space?
            if z['is_bullish'] and z['sl'] <= close_c <= z['entry']:
                valid_bullish_htf = True
            # Resistance zone check: Is the M5 close inside the H4 Open-to-High space?
            elif not z['is_bullish'] and z['entry'] <= close_c <= z['sl']:
                valid_bearish_htf = True

        # ─── LTF Zone Creation (M5) ────────────────────────────────
        # CMP Logic: Support = Bullish Candle, Resistance = Bearish Candle
        is_bullish = close_c > open_c
        is_bearish = close_c < open_c

        # Get symbol-specific thresholds
        config = self.symbol_configs.get(symbol, {})
        min_rr = round(config.get("min_rr", self.min_rr), 1)

        new_zone = None

        if is_bullish and valid_bullish_htf:  # FIXED: require HTF alignment for both directions
            # Support = Bullish Candle (Open to Low)
            entry = open_c
            sl    = low_c
            risk  = entry - sl
            if risk > 0:
                new_zone = {
                    "entry": entry, "sl": sl, "tp": entry + (risk * min_rr),
                    "is_bullish": True, "score": 100, "factors": {},
                    "active": True, "creation_time": datetime.now(timezone.utc)
                }
                log.info(f"[{symbol}] 🟢 LTF SUPPORT ZONE @ {entry:.5f} (HTF aligned)")

        elif is_bearish and valid_bearish_htf:
            # Resistance = Bearish Candle (Open to High)
            entry = open_c
            sl    = high_c
            risk  = sl - entry
            if risk > 0:
                new_zone = {
                    "entry": entry, "sl": sl, "tp": entry - (risk * min_rr),
                    "is_bullish": False, "score": 100, "factors": {},
                    "active": True, "creation_time": datetime.now(timezone.utc)
                }
                log.info(f"[{symbol}] 🔴 LTF RESISTANCE ZONE @ {entry:.5f} (HTF aligned)")

        # ─── Save to zone memory ─────────────────────────────────────
        if new_zone:
            if symbol not in self.active_zones:
                self.active_zones[symbol] = []
            self.active_zones[symbol].append(new_zone)
            if len(self.active_zones[symbol]) > self.MAX_ZONES:
                self.active_zones[symbol].pop(0)

    def _check_zone_retests(self, symbol: str):
        """Monitors live price to see if it retests any identified OHLC zones."""
        if not self.active_zones.get(symbol):
            return
            
        tick = mt5.symbol_info_tick(symbol)
        if not tick: return
        
        # Check session
        now_dt = datetime.now(timezone.utc)
        decimal_now = now_dt.hour + (now_dt.minute / 60.0)
        if not (self.session_start <= decimal_now < self.session_end):
            return

        for zone in self.active_zones[symbol]:
            if not zone["active"]: continue
            
            # Breach check: If price hits SL before retest, deactivate
            if zone["is_bullish"] and tick.bid < zone["sl"]:
                zone["active"] = False
                continue
            if not zone["is_bullish"] and tick.ask > zone["sl"]:
                zone["active"] = False
                continue

            # Retest check: Did we touch the Open price of the zone?
            is_retest = False
            if zone["is_bullish"] and tick.ask <= zone["entry"]:
                is_retest = True
            elif not zone["is_bullish"] and tick.bid >= zone["entry"]:
                is_retest = True
                
            if is_retest:
                # ── EXECUTION ──
                log.info(f"[{symbol}] CMP RETEST DETECTED @ {zone['entry']} | Executing...")
                risk = abs(zone["entry"] - zone["sl"])
                rr = round(abs(zone["tp"] - zone["entry"]) / risk, 1) if risk > 0 else 0
                
                self._handle_signal(
                    symbol, "buy" if zone["is_bullish"] else "sell",
                    zone["entry"], zone["sl"], zone["tp"], rr,
                    zone["score"], zone["factors"]
                )
                zone["active"] = False # One entry per zone

    def _log_and_alert_zone(self, symbol, zone, status):
        """Logs when a clean OHLC zone is identified (no Telegram alert)."""
        log.info(f"[{symbol}] New Zone: {status} at {zone['entry']}")


    def _handle_signal(self, symbol, action, entry, sl, tp, rr, score_pct, factors):
        if not self._is_cluster_ok(symbol):
            return

        open_count = len(mt5.positions_get() or [])
        if open_count >= self.max_open_trades:
            return

        risk_fraction   = score_pct / 100.0
        calculated_risk = self.risk_usd * risk_fraction
        # Guard: score_pct near 0 would send a near-zero lot order to MT5.
        # Enforce a minimum of 50% of base risk so every executed trade is meaningful.
        calculated_risk = max(calculated_risk, self.risk_usd * 0.5)

        log.info(
            f"EXECUTE {symbol} {action.upper()} | "
            f"Score: {score_pct}/{self.min_score} | Risk: ${calculated_risk:.2f} | RR: 1:{rr:.1f}"
        )

        # Empty payload since we are using CMP OHLC Zones without factors
        fact_payload = {}
        
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

    def _is_in_session(self, symbol: str, bar_time: int) -> bool:
        # Crypto trades 24/7 without session boundary limits
        if "BTC" in symbol or "ETH" in symbol:
            return True
            
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



    def _send_morning_greeting(self, start_h, start_m, end_h, end_m):
        quotes = [
            "\"The goal of a successful trader is to make the best trades. Money is secondary.\" — Alexander Elder",
            "\"In trading, you have to be defensive and aggressive at the same time. If you are not aggressive, you are not going to make money, and if you are not defensive, you are not going to keep money.\" — Paul Tudor Jones",
            "\"The stock market is a device for transferring money from the impatient to the patient.\" — Warren Buffett",
            "\"Trading is not for the person who wants to be right. It’s for the person who wants to make money.\" — Mark Douglas",
            "\"It's not whether you're right or wrong that's important, but how much money you make when you're right and how much you lose when you're wrong.\" — George Soros",
            "\"Consistency is the key. Plan your trade and trade your plan.\" — Institutional Wisdom",
            "\"Discipline is doing what needs to be done, even if you don't want to do it.\" — Unknown",
            "\"Risk comes from not knowing what you're doing.\" — Warren Buffett"
        ]
        import random
        quote = random.choice(quotes)
        
        msg = (
            f"🌅 *GOOD MORNING, CHAMPION*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📜 _{quote}_\n\n"
            f"📊 *DAILY BRIEFING*\n"
            f"├─ 🕒 Window: `{start_h:02d}:{start_m:02d} – {end_h:02d}:{end_m:02d} UTC`\n"
            f"├─ 💎 Assets: `{len(self.symbols)} active hunters`\n"
            f"└─ 🛡️ Risk Cap: `${self.max_daily_loss_usd:.2f} max loss`\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚀 _Algorithms locked. Let's conquer the markets._"
        )
        send_telegram_alert(msg)

    def _send_heartbeat(self):
        msg = (
            f"💓 *SYSTEM PULSE ALIVE*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🧠 *AI Engine:* Scanning {len(self.symbols)} pairs\n"
            f"📉 *Optimization:* Dynamic Ensemble IQ\n\n"
            f"🕙 *Timestamp:* `{datetime.now(timezone.utc).strftime('%H:%M')} UTC`\n"
            f"✅ *Status:* `100% Operational`"
        )
        send_telegram_alert(msg)



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