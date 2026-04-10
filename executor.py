import MetaTrader5 as mt5
import socketio
import json
import time

# --- Configuration ---
# Update this with your NestJS domain once deployed
SERVER_URL = "http://localhost:3000"
API_KEY = "your_secret"

sio = socketio.Client()

def execute_trade(data):
    symbol = data['ticker']
    
    # Initialize symbol
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        print(f"Symbol {symbol} not found!")
        return None

    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)

    volume = float(data.get('lots', 0.01))
    order_type = mt5.ORDER_TYPE_BUY if data['action'].lower() == 'buy' else mt5.ORDER_TYPE_SELL
    
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if data['action'].lower() == 'buy' else tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": float(data['sl']),
        "tp": float(data['tp2']),
        "magic": 123456,
        "comment": f"ID:{data.get('id', 'N/A')}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    print(f"WS: Executing {data['action']} {volume} {symbol}...")
    result = mt5.order_send(request)
    return result

@sio.event
def connect():
    print("WS: Connected to NestJS Bridge")

@sio.event
def disconnect():
    print("WS: Disconnected from server")

@sio.on('new_trade')
def on_new_trade(data):
    print(f"\n[SIGNAL] Received Real-time Trade: {data['ticker']} {data['action']}")
    res = execute_trade(data)
    
    if res is None:
        print("WS: Execution failed (symbol error)")
    elif res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"WS: SUCCESS! Order #{res.order} placed.")
    else:
        print(f"WS: FAILED (Code {res.retcode}): {res.comment}")

def main():
    if not mt5.initialize():
        print("MT5 Init failed. Start MT5 and enable Algo Trading.")
        return

    print(f"MT5 Connected: {mt5.terminal_info().name}")

    while True:
        try:
            if not sio.connected:
                print(f"WS: Connecting to {SERVER_URL}...")
                sio.connect(SERVER_URL, headers={"x-api-key": API_KEY})
            sio.wait()
        except Exception as e:
            print(f"WS Error: {e}. Retrying in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    main()
