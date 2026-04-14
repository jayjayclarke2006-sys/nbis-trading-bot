import alpaca_trade_api as tradeapi
import yfinance as yf
import pandas as pd
import time
import requests
import threading
from flask import Flask

# ===== CONFIG =====
API_KEY = "YOUR_API_KEY"
API_SECRET = "YOUR_SECRET_KEY"
BASE_URL = "https://paper-api.alpaca.markets"

TELEGRAM_TOKEN = "YOUR_TELEGRAM_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"

SYMBOLS = ["NBIS", "WULF", "IREN", "CIFR"]

RISK_PER_TRADE = 0.02
MAX_POSITION_PCT = 0.25

STOP_LOSS = 0.97
TAKE_PROFIT = 1.06
TRAILING_STOP = 0.98   # lock profits if it drops 2% from high
EARLY_EXIT_RSI = 65    # sell early if overbought and momentum weak

api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

# ===== TELEGRAM =====
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

# ===== RSI =====
def calculate_rsi(data, period=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

# ===== STRATEGY =====
def run_strategy():
    print("=== NEW CYCLE ===")

    account = api.get_account()
    equity = float(account.equity)

    for symbol in SYMBOLS:
        try:
            print(f"Checking {symbol}")

            df = yf.download(symbol, period="2d", interval="5m", progress=False)

            if df.empty or len(df) < 30:
                continue

            df["RSI"] = calculate_rsi(df["Close"])

            price = float(df["Close"].iloc[-1])
            rsi = float(df["RSI"].iloc[-1])
            high = float(df["High"].rolling(10).max().iloc[-1])
            volume = float(df["Volume"].iloc[-1])
            avg_volume = float(df["Volume"].rolling(10).mean().iloc[-1])

            positions = {p.symbol: p for p in api.list_positions()}

            # ===== BUY =====
            if symbol not in positions:

                # breakout + volume + RSI filter
                if price > high and volume > avg_volume and rsi < 70:

                    qty = int((equity * RISK_PER_TRADE) / price)
                    position_value = qty * price

                    if position_value / equity > MAX_POSITION_PCT:
                        continue

                    if qty > 0:
                        api.submit_order(
                            symbol=symbol,
                            qty=qty,
                            side='buy',
                            type='market',
                            time_in_force='gtc'
                        )

                        send_telegram(f"BUY {symbol} @ {price}")
                        print(f"Bought {symbol}")

            # ===== SELL =====
            else:
                position = positions[symbol]
                entry = float(position.avg_entry_price)
                qty = int(position.qty)

                stop_price = entry * STOP_LOSS
                take_profit_price = entry * TAKE_PROFIT

                # Track highest price since entry (approx using recent highs)
                recent_high = float(df["High"].rolling(10).max().iloc[-1])
                trailing_stop_price = recent_high * TRAILING_STOP

                # SELL CONDITIONS
                if (
                    price <= stop_price or
                    price >= take_profit_price or
                    price <= trailing_stop_price or
                    (rsi > EARLY_EXIT_RSI and price < df["Close"].iloc[-2])
                ):

                    api.submit_order(
                        symbol=symbol,
                        qty=qty,
                        side='sell',
                        type='market',
                        time_in_force='gtc'
                    )

                    send_telegram(f"SELL {symbol} @ {price}")
                    print(f"Sold {symbol}")

            time.sleep(2)

        except Exception as e:
            print(f"Error with {symbol}: {e}")

# ===== LOOP =====
def run_bot():
    print("Bot loop started")
    send_telegram("Bot is live 🚀")

    while True:
        run_strategy()
        time.sleep(600)

# ===== START =====
threading.Thread(target=run_bot).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
