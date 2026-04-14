import os
import time
import requests
import yfinance as yf
import pandas as pd
import alpaca_trade_api as tradeapi
from flask import Flask
from threading import Thread

# =========================
# CONFIG
# =========================

SYMBOLS = ["CIFR", "NBIS"]

TAKE_PROFIT = 0.06   # 6%
STOP_LOSS = 0.03     # 3%
MAX_POSITIONS = 3

CHECK_INTERVAL = 60  # seconds

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Alpaca API (IMPORTANT FIX)
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL")

api = tradeapi.REST(
    API_KEY,
    SECRET_KEY,
    BASE_URL,
    api_version="v2"
)

# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except:
        pass

# =========================
# STRATEGY
# =========================

def get_data(symbol):
    df = yf.download(symbol, period="1d", interval="1m")
    return df

def run_strategy():
    print("Running strategy...")

    positions = {p.symbol: p for p in api.list_positions()}

    for symbol in SYMBOLS:
        try:
            print(f"Checking {symbol}")

            df = get_data(symbol)
            if df.empty:
                continue

            price = float(df["Close"].iloc[-1])

            # ===== EXISTING POSITION =====
            if symbol in positions:
                entry_price = float(positions[symbol].avg_entry_price)

                change = (price - entry_price) / entry_price

                # TAKE PROFIT
                if change >= TAKE_PROFIT:
                    api.submit_order(
                        symbol=symbol,
                        qty=positions[symbol].qty,
                        side="sell",
                        type="market",
                        time_in_force="gtc"
                    )
                    send_telegram(f"SELL {symbol} @ {price} (TP hit)")
                    print(f"TP SELL {symbol}")

                # STOP LOSS
                elif change <= -STOP_LOSS:
                    api.submit_order(
                        symbol=symbol,
                        qty=positions[symbol].qty,
                        side="sell",
                        type="market",
                        time_in_force="gtc"
                    )
                    send_telegram(f"SELL {symbol} @ {price} (SL hit)")
                    print(f"SL SELL {symbol}")

            # ===== NEW ENTRY =====
            else:
                if len(positions) >= MAX_POSITIONS:
                    print("Max positions reached")
                    continue

                # SIMPLE MOMENTUM ENTRY
                if df["Close"].iloc[-1] > df["Close"].iloc[-5]:

                    cash = float(api.get_account().cash)
                    qty = int((cash * 0.3) / price)

                    if qty > 0:
                        api.submit_order(
                            symbol=symbol,
                            qty=qty,
                            side="buy",
                            type="market",
                            time_in_force="gtc"
                        )
                        send_telegram(f"BUY {symbol} @ {price}")
                        print(f"BUY {symbol}")

        except Exception as e:
            print(f"Error with {symbol}: {e}")

# =========================
# LOOP
# =========================

def bot_loop():
    print("Bot loop started")
    while True:
        run_strategy()
        time.sleep(CHECK_INTERVAL)

# =========================
# KEEP RENDER ALIVE
# =========================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

def run_web():
    app.run(host="0.0.0.0", port=10000)

# =========================
# START
# =========================

if __name__ == "__main__":
    Thread(target=bot_loop).start()
    run_web()
