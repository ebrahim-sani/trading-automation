import requests
import logging

log    = logging.getLogger("Journal")
BASE   = "http://localhost:4000"
HEADERS = {"Content-Type": "application/json", "x-api-key": "test_key_123"}

def _post(path, data):
    try:
        r = requests.post(f"{BASE}{path}", json=data, headers=HEADERS, timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"POST {path}: {e}")
        return None

def _get(path):
    try:
        r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=3)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                log.error(f"GET {path}: Response is not valid JSON")
                return []
        log.error(f"Bridge Error {r.status_code} at {path}")
        return []
    except Exception as e:
        log.error(f"GET {path}: {e}")
        return []

class JournalClient:
    def log_signal(self, symbol, action, entry, sl, tp, rr, bias1h, bias4h, aligned):
        _post("/internal/signal", dict(
            ticker=symbol, action=action, entry=entry,
            sl=sl, tp=tp, rr=rr,
            bias1h=bias1h, bias4h=bias4h, aligned=aligned
        ))

    def open_trade(self, symbol, action, entry, sl, tp, lots, risk_usd, ticket):
        _post("/internal/trade/open", dict(
            ticker=symbol, action=action, entry=entry,
            sl=sl, tp=tp, lots=lots, riskUsd=risk_usd, mt5Ticket=ticket
        ))

    def fail_trade(self, symbol, action, entry, sl, tp, lots, risk_usd, error):
        _post("/internal/trade/fail", dict(
            ticker=symbol, action=action, entry=entry,
            sl=sl, tp=tp, lots=lots, riskUsd=risk_usd, error=error
        ))

    def close_trade(self, ticket, reason, pnl):
        _post("/internal/trade/close", dict(mt5Ticket=ticket, reason=reason, pnl=pnl))

    def set_breakeven(self, ticket):
        _post(f"/internal/trade/{ticket}/breakeven", {})

    def get_open_trades(self):
        result = _get("/internal/trade/open")
        return result if isinstance(result, list) else []

    def get_today_pnl(self):
        result = _get("/internal/today-pnl")
        if result and "pnl" in result:
            return float(result["pnl"])
        return 0.0