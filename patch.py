import re

with open('backtester/backtest_engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix __init__ to remove predictor
init_replacement = '''        self.min_score = min_score

        # Ensure UTC index
        if self.df.index.tzinfo is None:'''
content = re.sub(r'        self\.min_score = min_score\n.*?# Ensure UTC index\n        if self\.df\.index\.tzinfo is None:', init_replacement, content, flags=re.DOTALL)

# Now, we need to replace the core logic of run() up to the build signals part
run_replacement = '''    def run(self) -> BacktestResult:
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

            is_bullish = sl_c[-1] > sl_o[-1]
            is_bearish = sl_c[-1] < sl_o[-1]
                
            # Create zones (Bullish Candle = Support, Bearish = Resistance)
            if is_bullish:
                e, s = float(sl_o[-1]), float(sl_l[-1])
                if (e - s) > 0:
                    active_zones.append(OHLCZone(e, s, e + (e-s)*self.min_rr, True, 100, {}, i-1))
            elif is_bearish:
                e, s = float(sl_o[-1]), float(sl_h[-1])
                if (s - e) > 0:
                    active_zones.append(OHLCZone(e, s, e - (s-e)*self.min_rr, False, 100, {}, i-1))

            if len(active_zones) > MAX_ZONES:
                active_zones.pop(0)

            # ── 3. Check for Retest Entry at Current Bar i ───────────
            # Deactivate zones if price breaches SL before entry
            for z in active_zones:
                if z.active:
                    if (z.is_bullish and lows[i] < z.sl) or (not z.is_bullish and highs[i] > z.sl):
                        z.active = False
            
            # Session filter
            decimal_hour = bar_time.hour + bar_time.minute / 60.0
            in_session = ("BTC" in self.symbol or "ETH" in self.symbol) or (self.session_start <= decimal_hour < self.session_end)
            
            if open_trade is None and in_session:
                for z in active_zones:
                    if not z.active or z.score < self.min_score:
                        continue
                    
                    # Entry trigger: price reaches the Open price level
                    if (z.is_bullish and lows[i] <= z.entry) or (not z.is_bullish and highs[i] >= z.entry):
                        open_trade = Trade(
                            symbol=self.symbol, direction="buy" if z.is_bullish else "sell",
                            entry_time=bar_time, entry=z.entry, sl=z.sl, tp=z.tp, 
                            rr=self.min_rr, score=z.score, factors=z.factors, entry_bar_idx=i
                        )
                        z.active = False # Deactivate after entry
                        break 

        # Close any still-open trade at end-of-data'''
content = re.sub(r'    def run\(self\) -> BacktestResult:.*?# Close any still-open trade at end-of-data', run_replacement, content, flags=re.DOTALL)

# Remove `_score_factors` and AI methods
content = re.sub(r'    # ─── Score Factors.*?    def _compute_stats', '    # ─── Stats ───────────────────────────────────────────────────────────\n\n    def _compute_stats', content, flags=re.DOTALL)
content = re.sub(r'    # ─── AI Fetchers.*', '', content, flags=re.DOTALL)

with open('backtester/backtest_engine.py', 'w', encoding='utf-8') as f:
    f.write(content)
