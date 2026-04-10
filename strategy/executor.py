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

    def open_trade(self, symbol, action, entry, sl, tp, risk_usd, journal):
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
            journal.open_trade(symbol, action, executed_price, sl, tp, lots, risk_usd, result.order)
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
        # Note: 'spread' in copy_rates is the SPREAD AT THE TIME OF BAR CLOSE
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 100)
        if bars is None or len(bars) == 0:
            return True # Fallback if no history
            
        avg_spread = sum(b['spread'] for b in bars) / len(bars)
        
        # Max limit is 2x the average spread
        if current_spread > (avg_spread * 2.5): # Use 2.5x as a slightly looser buffer
            return False
            
        return True

    def manage_open_trades(self, timeout_minutes: int, journal):
        """
        Checks every open trade for:
        1. Timeout  → close after timeout_minutes
        2. Breakeven → move SL to entry once price moved 1R in our favour
        """
        db_trades = journal.get_open_trades()
        if not db_trades:
            return

        now = datetime.now()

        for trade in db_trades:
            ticket  = trade.get("mt5_ticket")
            if not ticket:
                continue

            opened  = datetime.fromisoformat(trade["opened_at"])
            age_min = (now - opened).total_seconds() / 60

            # ── Timeout ──────────────────────────────────────────────
            if age_min >= timeout_minutes:
                log.warning(f"Trade #{ticket} timed out ({age_min:.1f} min) — closing")
                self._close_position(ticket, "timeout", journal)
                continue

            # ── Breakeven ─────────────────────────────────────────────
            if trade.get("breakeven_set"):
                continue

            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                continue

            pos    = positions[0]
            entry  = float(trade["entry"])
            sl     = float(trade["sl"])
            risk   = abs(entry - sl)

            # Price has moved 1R in our favour
            price_move = abs(pos.price_current - entry)
            if price_move >= risk:
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
                    log.error(f"Breakeven failed on #{ticket}: {result.comment if result else mt5.last_error()}")

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

    def _get_filling(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC
        if info.filling_mode & mt5.ORDER_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        if info.filling_mode & mt5.ORDER_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN