import MetaTrader5 as mt5
import socketio
import json
import time
import requests
import os
from dotenv import load_dotenv

# Load environment variables from the agent directory
load_dotenv("vibe-trading/agent/.env")

# --- Configuration ---
SERVER_URL = "http://localhost:3000"
API_KEY = "your_secret"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

sio = socketio.Client()

def send_telegram_alert(message):
    """Send a notification to the user via Telegram."""
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
        print(f"Telegram Error: {e}")

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
    action_str = data['action'].upper()
    order_type = mt5.ORDER_TYPE_BUY if action_str == 'BUY' else mt5.ORDER_TYPE_SELL
    
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if action_str == 'BUY' else tick.bid

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
    
    print(f"WS: Executing {action_str} {volume} {symbol}...")
    result = mt5.order_send(request)
    
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        # Success! Prepare telegram alert
        msg = (
            f"🚀 *TTFM TRADE EXECUTED*\n\n"
            f"🔹 *Asset:* {symbol}\n"
            f"🔹 *Action:* {action_str}\n"
            f"🔹 *Lots:* {volume}\n"
            f"🔹 *Entry:* {price:.5f}\n"
            f"🔹 *SL:* {data['sl']}\n"
            f"🔹 *TP:* {data['tp2']}\n\n"
            f"🧠 *AI Reasoning:* {data.get('reason', 'Institutional SMC Sweep Detected')}"
        )
        send_telegram_alert(msg)
        
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
