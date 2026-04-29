import logging
import MetaTrader5 as mt5
import json
import os
from datetime import datetime, timedelta, timezone
from lot_calculator import calculate_lots

log = logging.getLogger("Executor")

class MT5Executor:

    def __init__(self):
        self.magic = 20260101
        self.vault_path = os.path.join(os.path.dirname(__file__), "opentrades.json")
        self._ensure_vault()

    def _ensure_vault(self):
        if not os.path.exists(self.vault_path):
            with open(self.vault_path, 'w') as f:
                json.dump([], f)

    def _get_local_trades(self):
        try:
            with open(self.vault_path, 'r') as f:
                return json.load(f)
        except: return []

    def _save_local_trades(self, trades):
        try:
            with open(self.vault_path, 'w') as f:
                json.dump(trades, f, indent=4)
        except Exception as e:
            log.error(f"Failed to save local vault: {e}")

    def _add_local_trade(self, trade_data):
        trades = self._get_local_trades()
        trades = [t for t in trades if t.get("mt5Ticket") != trade_data.get("mt5Ticket")]
        trades.append(trade_data)
        self._save_local_trades(trades)

    def _remove_local_trade(self, ticket):
        trades = self._get_local_trades()
        new_trades = [t for t in trades if str(t.get("mt5Ticket")) != str(ticket)]
        self._save_local_trades(new_trades)

    def _update_local_trade(self, ticket, updates):
        trades = self._get_local_trades()
        found = False
        for t in trades:
            if str(t.get("mt5Ticket")) == str(ticket):
                t.update(updates)
                found = True
        if found:
            self._save_local_trades(trades)

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
        if not self._is_spread_ok(symbol):
            log.warning(f"  → Rejected: {symbol} spread is too wide (News/Rollover?)")
            return

        order_type = mt5.ORDER_TYPE_BUY  if action == "buy"  else mt5.ORDER_TYPE_SELL
        price      = tick.ask            if action == "buy"  else tick.bid
        filling    = self._get_filling(symbol)

        # ── Margin Pre-Check (Guard 2) ────────────────────────────────
        # Prevents 10019 "No money" broker rejections by checking locally first.
        account    = mt5.account_info()
        margin_req = mt5.order_calc_margin(order_type, symbol, lots, price)
        if account is None or margin_req is None:
            log.error(f"Cannot get account/margin info for {symbol} — skipping")
            return
        log.info(
            f"  Margin check | {symbol} {action} {lots}L | "
            f"Need: ${margin_req:.2f} | Free: ${account.margin_free:.2f}"
        )
        if margin_req > account.margin_free * 0.90:
            log.warning(
                f"  → Rejected: insufficient margin | {symbol} | "
                f"Need ${margin_req:.2f} but only ${account.margin_free:.2f} free"
            )
            return

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       lots,
            "type":         order_type,
            "price":        price,
            "sl":           sl,
            "tp":           tp,
            "deviation":    20,
            "magic":        self.magic,
            "comment":      "TTFM",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        result = mt5.order_send(request)

        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            executed_price = result.price if result.price else price
            log.info(f"TRADE OPENED | {symbol} {action} | Ticket #{result.order} | Price {executed_price}")
            
            journal.open_trade(symbol, action, executed_price, sl, tp, lots, risk_usd, result.order, score=score, setup_score=setup_score)
            
            self._add_local_trade({
                "mt5Ticket":     result.order,
                "ticker":        symbol,
                "action":        action,
                "entry":         executed_price,
                "sl":            sl,
                "tp":            tp,
                "lots":          lots,
                "openedAt":      datetime.now(timezone.utc).isoformat(),
                "breakevenSet":  False,
                "partialClosed": False
            })
        else:
            err = result.comment if result else str(mt5.last_error())
            log.error(f"FAILED | {symbol} {action} | {err} (retcode: {result.retcode if result else 'N/A'})")
            journal.fail_trade(symbol, action, entry, sl, tp, lots, risk_usd, err)

    def _is_spread_ok(self, symbol: str) -> bool:
        info = mt5.symbol_info(symbol)
        if not info: return False
        current_spread = info.spread
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 100)
        if bars is None or len(bars) == 0:
            return True
        avg_spread = sum(b['spread'] for b in bars) / len(bars)
        if current_spread > (avg_spread * 2.5):
            return False
        return True

    def manage_open_trades(self, timeout_minutes: int, journal):
        local_trades = self._get_local_trades()
        db_trades = local_trades if local_trades else journal.get_open_trades()
        
        if not db_trades:
            return

        now = datetime.now(timezone.utc)

        for trade in db_trades:
            ticket = trade.get("mt5Ticket")
            if not ticket:
                continue

            opened_str = trade.get("openedAt") if trade.get("openedAt") else trade.get("opened_at")
            if not opened_str: continue
            opened = datetime.fromisoformat(opened_str.replace("Z", "+00:00"))
            age_min = (now - opened).total_seconds() / 60

            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                self._sync_closed_position(ticket, journal)
                continue

            pos   = positions[0]
            entry = float(trade["entry"])
            sl    = float(trade["sl"])
            tp    = float(trade["tp"])

            is_buy  = pos.type == mt5.ORDER_TYPE_BUY
            current = pos.price_current

            price_move_in_favour = (current - entry) if is_buy else (entry - current)

            if age_min >= timeout_minutes and not trade.get("breakevenSet"):
                log.warning(f"Trade #{ticket} timed out ({age_min:.1f} min) — closing")
                self._close_position(ticket, "timeout", journal)
                continue

            tp_distance = abs(tp - entry)
            if not trade.get("breakevenSet") and price_move_in_favour >= (tp_distance * 0.5):
                partial_lots = round(pos.volume * 0.50, 2)
                partial_lots = max(mt5.symbol_info(pos.symbol).volume_min, partial_lots)
                if partial_lots < pos.volume:
                    self._partial_close(ticket, pos, partial_lots, "TP1_50p_target", journal)
                
                req = {
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "symbol":   pos.symbol,
                    "sl":       entry,
                    "tp":       pos.tp,
                }
                result = mt5.order_send(req)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    log.info(f"TP1 Taken & Breakeven set on #{ticket} @ {entry:.5f}")
                    journal.set_breakeven(ticket)
                    self._update_local_trade(ticket, {"breakevenSet": True})

            if not trade.get("partialClosed") and price_move_in_favour >= (tp_distance * 0.75):
                partial_lots = round(pos.volume * 0.30, 2)
                partial_lots = max(mt5.symbol_info(pos.symbol).volume_min, min(partial_lots, pos.volume - mt5.symbol_info(pos.symbol).volume_min))
                if partial_lots > 0:
                    self._partial_close(ticket, pos, partial_lots, "TP2_75p_target", journal)

            if trade.get("breakevenSet") and price_move_in_favour >= (tp_distance * 0.85):
                atr = self._get_current_atr(pos.symbol)
                if atr > 0:
                    new_sl = (current - atr * 0.5) if is_buy else (current + atr * 0.5)
                    current_sl = pos.sl
                    should_update = (is_buy and new_sl > current_sl) or (not is_buy and new_sl < current_sl)
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

            if trade.get("breakevenSet") and age_min > 5:
                atr = self._get_current_atr(pos.symbol)
                if atr > 0 and price_move_in_favour < 0 and abs(price_move_in_favour) > atr * 1.5:
                    log.warning(f"Trade #{ticket}: Adverse move {price_move_in_favour:.5f} — closing to protect BE")
                    self._close_position(ticket, "adverse_momentum", journal)

    def _partial_close(self, ticket: int, pos, lots: float, reason: str, journal):
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
            "magic":        self.magic,
            "comment":      f"TTFM {reason}",
            "type_filling": self._get_filling(pos.symbol),
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"PARTIAL CLOSE #{ticket} | {lots} lots | Reason: {reason}")
            journal.set_partial_closed(ticket)
            self._update_local_trade(ticket, {"partialClosed": True})
        else:
            log.error(f"Partial close failed #{ticket}: {result.comment if result else mt5.last_error()}")

    def _get_current_atr(self, symbol: str, period: int = 14) -> float:
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
            "magic":        self.magic,
            "comment":      f"TTFM {reason}",
            "type_filling": self._get_filling(pos.symbol),
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"CLOSED #{ticket} | Reason: {reason} | PnL: ${pnl:.2f}")
            journal.close_trade(ticket, reason, pnl)
            self._remove_local_trade(ticket)
        else:
            log.error(f"Close failed #{ticket}: {result.comment if result else mt5.last_error()}")

    def _sync_closed_position(self, ticket: int, journal):
        """Processes trades that closed natively in MT5 (SL/TP)."""
        now = datetime.now(timezone.utc)
        from_ts = datetime(2020, 1, 1)
        deals = mt5.history_deals_get(from_ts, now, position=ticket)

        if not deals:
            log.warning(f"Could not find history for closed position #{ticket}")
            self._remove_local_trade(ticket)
            return

        pnl = sum(d.profit + d.swap + d.commission for d in deals)

        last_deal = deals[-1]
        reason = last_deal.comment if last_deal.comment else "SL/TP Hit"

        log.info(f"Position #{ticket} closed natively | Reason: {reason} | PnL: ${pnl:.2f}")
        journal.close_trade(ticket, reason, pnl)
        self._remove_local_trade(ticket)

    def _get_filling(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC
        if info.filling_mode & mt5.ORDER_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        if info.filling_mode & mt5.ORDER_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN