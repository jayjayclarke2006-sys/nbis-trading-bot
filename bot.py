from flask import Flask
import threading
import time
import os
from datetime import datetime
import requests
import yfinance as yf
import pandas as pd
import numpy as np

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

app = Flask(__name__)

# =====================
# ENV
# =====================
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["NBIS", "WULF", "IREN", "CIFR"]

# =====================
# SETTINGS
# =====================
RISK_PER_TRADE = 0.01
STOP_LOSS = 0.03
TAKE_PROFIT = 0.06

RUN_INTERVAL = 300  # 5 minutes

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

trade_log = []

# =====================
# TELEGRAM
# =====================
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        )
    except:
        pass

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
        print(f"Fetching data for {symbol}...")

        df = yf.download(symbol, period="3mo", interval="1h", progress=False)

        if df is None or df.empty:
            print("No data returned")
            return None

        df["ema_fast"] = ema(df["Close"], 50)
        df["ema_slow"] = ema(df["Close"], 200)
        df["rsi"] = rsi(df["Close"], 14)

        df.dropna(inplace=True)

        if len(df) < 2:
            print("Not enough data")
            return None

        return df

    except Exception as e:
        print("Data error:", e)
        return None

# =====================
# POSITION SIZE
# =====================
def calc_qty(price, equity):
    risk_amount = equity * RISK_PER_TRADE
    risk_per_share = price * STOP_LOSS

    if risk_per_share == 0:
        return 0

    return int(risk_amount // risk_per_share)

# =====================
# STRATEGY
# =====================
def run_strategy():
    print("\n=== Running strategy ===")

    for SYMBOL in SYMBOLS:
        print(f"\nChecking {SYMBOL}")

        df = get_data(SYMBOL)

        if df is None:
            time.sleep(2)
            continue

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(latest["Close"])
        print(f"Price: {price}")

        # CONDITIONS
        trend = latest["ema_fast"] > latest["ema_slow"]
        momentum = latest["rsi"] > 55
        rising = latest["Close"] > prev["Close"]

        print(f"Trend: {trend} | RSI: {latest['rsi']} | Rising: {rising}")

        # BUY
        if trend and momentum and rising:
            print("BUY SIGNAL DETECTED")

            try:
                account = client.get_account()
                equity = float(account.equity)
            except:
                continue

            qty = calc_qty(price, equity)

            if qty > 0:
                client.submit_order(
                    MarketOrderRequest(
                        symbol=SYMBOL,
                        qty=qty,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY
                    )
                )

                msg = f"BUY {SYMBOL} @ {price}"
                print(msg)
                send_telegram(msg)

        # SELL
        try:
            positions = client.get_all_positions()

            for p in positions:
                if p.symbol == SYMBOL:
                    entry = float(p.avg_entry_price)
                    qty = int(float(p.qty))

                    stop = entry * (1 - STOP_LOSS)
                    tp = entry * (1 + TAKE_PROFIT)

                    if price <= stop or price >= tp:
                        print("SELL SIGNAL")

                        client.submit_order(
                            MarketOrderRequest(
                                symbol=SYMBOL,
                                qty=qty,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            )
                        )

                        msg = f"SELL {SYMBOL} @ {price}"
                        print(msg)
                        send_telegram(msg)

        except Exception as e:
            print("Position error:", e)

        time.sleep(2)

# =====================
# LOOP
# =====================
def bot_loop():
    print("Bot started")
    send_telegram("Bot is live 🚀")

    while True:
        try:
            run_strategy()
        except Exception as e:
            print("Loop error:", e)

        print(f"Sleeping for {RUN_INTERVAL} seconds...\n")
        time.sleep(RUN_INTERVAL)

# =====================
# WEB
# =====================
@app.route("/")
def home():
    return "Bot is running"

# =====================
# START
# =====================
if __name__ == "__main__":
    threading.Thread(target=bot_loop).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
