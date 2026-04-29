import MetaTrader5 as mt5
import logging

log = logging.getLogger("LotCalc")

def calculate_lots(entry: float, sl: float, risk_usd: float, symbol: str) -> float:
    """
    Returns the lot size such that SL hit = exactly risk_usd loss.
    Uses MT5's own tick value data so it's accurate for any broker.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        log.error(f"symbol_info returned None for {symbol}")
        return 0.01

    price_delta = abs(entry - sl)
    if price_delta == 0:
        log.warning(f"{symbol}: entry == sl, defaulting to 0.01 lots")
        return 0.01

    # tick_value = P&L in account currency for one tick move on 1 lot
    # tick_size  = the size of that one tick in price terms
    # So: value_per_price_unit = tick_value / tick_size
    # And: value_of_sl_distance = price_delta * (tick_value / tick_size)
    # So: lots = risk_usd / (price_delta * tick_value / tick_size)
    value_per_unit = info.trade_tick_value / info.trade_tick_size
    raw_lots       = risk_usd / (price_delta * value_per_unit)

    # Clamp to broker's volume min/max/step
    step = info.volume_step
    lots = round(raw_lots / step) * step
    lots = max(info.volume_min, min(info.volume_max, lots))

    log.info(
        f"{symbol} | Risk ${risk_usd} | Delta: {price_delta:.5f} | "
        f"Val/unit: {value_per_unit:.4f} | Raw: {raw_lots:.4f} | Final: {lots}"
    )
    return lots