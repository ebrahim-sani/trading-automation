import logging
import MetaTrader5 as mt5
from datetime import datetime
from lot_calculator import calculate_lots

log = logging.getLogger("Executor")

class MT5Executor:

    def init(self) -> bool:
        if not mt5.initialize():
            log.error(f"MT5 init failed: {mt5.last_error()}")
            return False
        info = mt5.terminal_info()
        log.info(f"MT5 OK | Build: {info.build} | Trade allowed: {info.trade_allowed}")
        return True

    def open_trade(self, symbol, action, entry, sl, tp, risk_usd, journal, score=None, setup_score=None):
        lots = calculate_lots(entry, sl, risk_usd, symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error(f"No tick data for {symbol}")
            return

        # ── Spread Shield (Guard 1) ──────────────────────────────────
        # Check if current spread is significantly wider than average
        if not self._is_spread_ok(symbol):
            log.warning(f"  → Rejected: {symbol} spread is too wide (News/Rollover?)")
            return

        order_type = mt5.ORDER_TYPE_BUY  if action == "buy"  else mt5.ORDER_TYPE_SELL
        price      = tick.ask            if action == "buy"  else tick.bid
        filling    = self._get_filling(symbol)

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       lots,
            "type":         order_type,
            "price":        price,
            "sl":           sl,
            "tp":           tp,
            "deviation":    20,        # Max deviation (2 pips on 5-digit brokers)
            "magic":        20260101,
            "comment":      "TTFM",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        result = mt5.order_send(request)

        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            executed_price = result.price if result.price else price
            log.info(
                f"FILLED #{result.order} | {action.upper()} {lots} {symbol} @ {executed_price:.5f} | "
                f"SL: {sl:.5f} | TP: {tp:.5f}"
            )
            journal.open_trade(symbol, action, executed_price, sl, tp, lots, risk_usd, result.order, score=score, setup_score=setup_score)
        else:
            err = result.comment if result else str(mt5.last_error())
            log.error(f"FAILED | {symbol} {action} | {err} (retcode: {result.retcode if result else 'N/A'})")
            journal.fail_trade(symbol, action, entry, sl, tp, lots, risk_usd, err)

    def _is_spread_ok(self, symbol: str) -> bool:
        """
        Institutional Safeguard: Skip trades if the spread is > 2x the normal average.
        Protects against news-spikes or low-liquidity rollover.
        """
        info = mt5.symbol_info(symbol)
        if not info: return False

        current_spread = info.spread

        # Get historical bars to define 'normal' spread (last 100 bars)
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 100)
        if bars is None or len(bars) == 0:
            return True  # Fallback if no history

        avg_spread = sum(b['spread'] for b in bars) / len(bars)

        # Max limit is 2.5x the average spread
        if current_spread > (avg_spread * 2.5):
            return False

        return True

    def manage_open_trades(self, timeout_minutes: int, journal):
        """
        Checks every open trade for:
        1. Timeout        → close after timeout_minutes if not yet breakeven
        2. Breakeven      → move SL to entry once price moved 1R in our favour
        3. Partial close  → close 30% of position at +1.5R  (NEW)
        4. Trailing SL    → tighten SL to peak - 0.5×ATR once price > +2R  (NEW)
        5. Adverse exit   → close trade if price moving strongly against us  (NEW)
        """
        db_trades = journal.get_open_trades()
        if not db_trades:
            return

        now = datetime.now()

        for trade in db_trades:
            ticket = trade.get("mt5_ticket")
            if not ticket:
                continue

            opened  = datetime.fromisoformat(trade["opened_at"])
            age_min = (now - opened).total_seconds() / 60

            # ── Position missing means it hit SL/TP natively in MT5 ──
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                self._sync_closed_position(ticket, journal)
                continue

            pos   = positions[0]
            entry = float(trade["entry"])
            sl    = float(trade["sl"])
            tp    = float(trade["tp"])
            risk  = abs(entry - sl)

            is_buy  = pos.type == mt5.ORDER_TYPE_BUY
            current = pos.price_current

            price_move_in_favour = (current - entry) if is_buy else (entry - current)
            price_move_raw       = abs(current - entry)

            # ── 1. Timeout (only if not yet breakeven) ────────────────
            if age_min >= timeout_minutes and not trade.get("breakevenSet"):
                log.warning(f"Trade #{ticket} timed out ({age_min:.1f} min) — closing")
                self._close_position(ticket, "timeout", journal)
                continue

            # ── 2. Breakeven at +1R ──────────────────────────────────
            if not trade.get("breakevenSet") and price_move_in_favour >= risk:
                req = {
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "symbol":   pos.symbol,
                    "sl":       entry,
                    "tp":       pos.tp,
                }
                result = mt5.order_send(req)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    log.info(f"Breakeven set on #{ticket} @ {entry:.5f}")
                    journal.set_breakeven(ticket)
                else:
                    log.error(f"Breakeven failed on #{ticket}: "
                              f"{result.comment if result else mt5.last_error()}")

            # ── 3. Partial close at +1.5R ────────────────────────────
            if not trade.get("partialClosed") and price_move_in_favour >= risk * 1.5:
                partial_lots = round(pos.volume * 0.30, 2)
                partial_lots = max(
                    mt5.symbol_info(pos.symbol).volume_min,
                    min(partial_lots, pos.volume - mt5.symbol_info(pos.symbol).volume_min),
                )
                if partial_lots > 0:
                    self._partial_close(ticket, pos, partial_lots, "partial_1.5R", journal)

            # ── 4. Trailing SL once +2R is hit ───────────────────────
            if trade.get("breakevenSet") and price_move_in_favour >= risk * 2.0:
                atr = self._get_current_atr(pos.symbol)
                if atr > 0:
                    new_sl = (current - atr * 0.5) if is_buy else (current + atr * 0.5)
                    current_sl = pos.sl
                    # Only tighten SL — never widen it
                    should_update = (is_buy and new_sl > current_sl) or \
                                    (not is_buy and new_sl < current_sl)
                    if should_update:
                        req = {
                            "action":   mt5.TRADE_ACTION_SLTP,
                            "position": ticket,
                            "symbol":   pos.symbol,
                            "sl":       round(new_sl, mt5.symbol_info(pos.symbol).digits),
                            "tp":       pos.tp,
                        }
                        result = mt5.order_send(req)
                        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                            log.info(f"Trailing SL updated #{ticket} → {new_sl:.5f} (ATR trail)")

            # ── 5. Adverse momentum exit (if price reversing strongly) ─
            # Only applies after trade has been open > 5 min and SL moved to BE
            if trade.get("breakevenSet") and age_min > 5:
                atr = self._get_current_atr(pos.symbol)
                if atr > 0 and price_move_in_favour < 0 and abs(price_move_in_favour) > atr * 1.5:
                    log.warning(
                        f"Trade #{ticket}: Adverse move {price_move_in_favour:.5f} "
                        f"(>{atr * 1.5:.5f} ATR) — closing to protect BE"
                    )
                    self._close_position(ticket, "adverse_momentum", journal)

    def _partial_close(self, ticket: int, pos, lots: float, reason: str, journal):
        """Close a partial portion of an open position."""
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick       = mt5.symbol_info_tick(pos.symbol)
        price      = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       lots,
            "type":         close_type,
            "position":     ticket,
            "price":        price,
            "magic":        20260101,
            "comment":      f"TTFM {reason}",
            "type_filling": self._get_filling(pos.symbol),
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            pnl = (price - pos.price_open) * lots if pos.type == mt5.ORDER_TYPE_BUY \
                  else (pos.price_open - price) * lots
            log.info(f"PARTIAL CLOSE #{ticket} | {lots} lots | Reason: {reason} | ~PnL: ${pnl:.2f}")
            try:
                journal.set_partial_closed(ticket)
            except Exception:
                pass  # journal may not support partial yet
        else:
            log.error(f"Partial close failed #{ticket}: {result.comment if result else mt5.last_error()}")

    def _get_current_atr(self, symbol: str, period: int = 14) -> float:
        """Fetch current ATR(14) from M5 bars for trailing stop calculations."""
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, period + 2)
        if bars is None or len(bars) < period + 1:
            return 0.0
        tr_list = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]['high'], bars[i]['low'], bars[i - 1]['close']
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(tr_list[-period:]) / period

    def _close_position(self, ticket: int, reason: str, journal):
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            log.warning(f"Position #{ticket} not found")
            return

        pos        = positions[0]
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick       = mt5.symbol_info_tick(pos.symbol)
        price      = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        pnl        = pos.profit

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     ticket,
            "price":        price,
            "magic":        20260101,
            "comment":      f"TTFM {reason}",
            "type_filling": self._get_filling(pos.symbol),
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"CLOSED #{ticket} | Reason: {reason} | PnL: ${pnl:.2f}")
            journal.close_trade(ticket, reason, pnl)
        else:
            log.error(f"Close failed #{ticket}: {result.comment if result else mt5.last_error()}")

    def _sync_closed_position(self, ticket: int, journal):
        """
        Catches positions that disappeared (hit SL or TP natively in MT5)
        and synchronizes their closed state and final PnL back to the database.
        """
        now = datetime.now()
        from_ts = datetime(2020, 1, 1)
        deals = mt5.history_deals_get(from_ts, now, position=ticket)

        if not deals:
            log.warning(f"Could not find history for closed position #{ticket}")
            return

        pnl = sum(d.profit + d.swap + d.commission for d in deals)

        last_deal = deals[-1]
        reason = last_deal.comment if last_deal.comment else "SL/TP Hit"

        log.info(f"Position #{ticket} closed natively | Reason: {reason} | PnL: ${pnl:.2f}")
        journal.close_trade(ticket, reason, pnl)

    def _get_filling(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC
        if info.filling_mode & mt5.ORDER_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        if info.filling_mode & mt5.ORDER_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN