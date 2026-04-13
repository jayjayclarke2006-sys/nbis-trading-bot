from flask import Flask
import time
import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

app = Flask(__name__)

# =====================
# ENV VARIABLES
# =====================
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

SYMBOLS = ["NBIS", "WULF", "IREN", "CIFR"]

RUN_INTERVAL = 600  # 10 mins

# =====================
# TELEGRAM
# =====================
def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        )
    except:
        print("Telegram failed")

# =====================
# INDICATORS
# =====================
def ema(series, span):
    return series.ewm(span=span).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

# =====================
# DATA
# =====================
def get_data(symbol):
    try:
        print(f"Fetching {symbol}...")

        df = yf.download(symbol, period="3mo", interval="1h", progress=False)

        if df is None or df.empty:
            print("No data")
            return None

        df["ema_fast"] = ema(df["Close"], 50)
        df["ema_slow"] = ema(df["Close"], 200)
        df["rsi"] = rsi(df["Close"])

        df.dropna(inplace=True)

        if len(df) < 2:
            return None

        return df

    except Exception as e:
        print("Data error:", e)

        if "Rate limited" in str(e):
            print("Sleeping due to rate limit...")
            time.sleep(30)

        return None

# =====================
# STRATEGY
# =====================
def run_bot():
    print("Bot loop started")

    send_telegram("Bot is live 🚀")

    while True:
        print("\n=== NEW CYCLE ===")

        for symbol in SYMBOLS:
            print(f"\nChecking {symbol}")

            df = get_data(symbol)

            if df is None:
                time.sleep(2)
                continue

            # 🔥 FIXED (no Series issues)
            price = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-2])

            ema_fast = float(df["ema_fast"].iloc[-1])
            ema_slow = float(df["ema_slow"].iloc[-1])
            rsi_val = float(df["rsi"].iloc[-1])

            print("Price:", price)

            trend = ema_fast > ema_slow
            momentum = rsi_val > 55
            rising = price > prev_close

            print(f"Trend={trend} RSI={rsi_val} Rising={rising}")

            # BUY
            if trend and momentum and rising:
                print("BUY SIGNAL")

                try:
                    account = client.get_account()
                    equity = float(account.equity)

                    qty = int((equity * 0.01) / (price * 0.03))

                    if qty > 0:
                        client.submit_order(
                            MarketOrderRequest(
                                symbol=symbol,
                                qty=qty,
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY
                            )
                        )

                        msg = f"BUY {symbol} @ {price}"
                        print(msg)
                        send_telegram(msg)

                except Exception as e:
                    print("Buy error:", e)

            # SELL
            try:
                positions = client.get_all_positions()

                for p in positions:
                    if p.symbol == symbol:
                        entry = float(p.avg_entry_price)
                        qty = int(float(p.qty))

                        stop = entry * 0.97
                        tp = entry * 1.06

                        if price <= stop or price >= tp:
                            print("SELL SIGNAL")

                            client.submit_order(
                                MarketOrderRequest(
                                    symbol=symbol,
                                    qty=qty,
                                    side=OrderSide.SELL,
                                    time_in_force=TimeInForce.DAY
                                )
                            )

                            msg = f"SELL {symbol} @ {price}"
                            print(msg)
                            send_telegram(msg)

            except Exception as e:
                print("Sell error:", e)

            time.sleep(2)

        print(f"\nSleeping {RUN_INTERVAL}s...\n")
        time.sleep(RUN_INTERVAL)

# =====================
# WEB ROUTE
# =====================
@app.route("/")
def home():
    return "Bot is running"

# =====================
# START
# =====================
if __name__ == "__main__":
    print("Starting bot...")

    import threading
    threading.Thread(target=run_bot).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
