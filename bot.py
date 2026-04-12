from flask import Flask
import threading
import time
import os
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf
import pandas as pd
import numpy as np

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

app = Flask(__name__)

# =====================
# ENV VARIABLES
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
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06

EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14

RUN_INTERVAL = 600

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

trade_log = []
last_trade_time = None

# =====================
# TELEGRAM
# =====================
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

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
    df = yf.download(symbol, period="3mo", interval="1h", progress=False)

    if df is None or df.empty:
        return None

    df["ema_fast"] = ema(df["Close"], EMA_FAST)
    df["ema_slow"] = ema(df["Close"], EMA_SLOW)
    df["rsi"] = rsi(df["Close"], RSI_PERIOD)

    df.dropna(inplace=True)

    if len(df) < 2:
        return None

    return df

# =====================
# POSITION SIZE
# =====================
def calc_qty(price, equity):
    risk_amount = equity * RISK_PER_TRADE
    risk_per_share = price * STOP_LOSS_PCT

    if risk_per_share == 0:
        return 0

    qty = int(risk_amount // risk_per_share)
    return max(0, qty)

# =====================
# STRATEGY
# =====================
def run_strategy():

    for SYMBOL in SYMBOLS:
        print(f"\nChecking {SYMBOL}")

        df = get_data(SYMBOL)

        if df is None:
            print("No data")
            continue

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(latest["Close"])

        # BUY CONDITIONS
        trend = latest["ema_fast"] > latest["ema_slow"]
        momentum = latest["rsi"] > 55
        rising = latest["Close"] > prev["Close"]

        if trend and momentum and rising:
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

                trade_log.append({
                    "symbol": SYMBOL,
                    "type": "BUY",
                    "price": price,
                    "time": str(datetime.now())
                })

        # SELL CONDITIONS (simple exit)
        try:
            positions = client.get_all_positions()
            for p in positions:
                if p.symbol == SYMBOL:
                    entry = float(p.avg_entry_price)
                    qty = int(float(p.qty))

                    stop = entry * (1 - STOP_LOSS_PCT)
                    tp = entry * (1 + TAKE_PROFIT_PCT)

                    if price <= stop or price >= tp:
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

                        trade_log.append({
                            "symbol": SYMBOL,
                            "type": "SELL",
                            "price": price,
                            "time": str(datetime.now())
                        })

        except Exception as e:
            print("Error:", e)

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

        time.sleep(RUN_INTERVAL)

# =====================
# WEB (RENDER)
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
