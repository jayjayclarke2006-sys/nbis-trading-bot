import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from flask import Flask

# =========================
# CONFIG
# =========================
BTC_SYMBOL = "BTC-USD"
TIMEFRAME = "5m"
PERIOD = "5d"

ALERT_SCORE = 65   # 🔥 lowered from 80 → more trades
CHECK_INTERVAL = 300

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

VOLUME_LOOKBACK = 20
BREAKOUT_LOOKBACK = 10

RSI_MIN = 50
RSI_MAX = 75
RVOL_MIN = 1.3

BREAKOUT_BUFFER = 0.002

ATR_SL_MULT = 1.5
ATR_TP_MULT = 2.5
TRAIL_MULT = 1.8

# =========================
# TELEGRAM
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass

# =========================
# INDICATORS
# =========================
def ema(s, n): return s.ewm(span=n).mean()

def rsi(s, n=14):
    d = s.diff()
    gain = d.clip(lower=0)
    loss = -d.clip(upper=0)
    rs = gain.rolling(n).mean() / loss.rolling(n).mean()
    return 100 - (100 / (1 + rs))

def atr(df, n=14):
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()

# =========================
# STATE
# =========================
in_trade = False
entry = 0
stop = 0
tp = 0
highest = 0
break_even = False

# =========================
# DATA
# =========================
def get_data():
    df = yf.download(BTC_SYMBOL, period=PERIOD, interval=TIMEFRAME, progress=False)
    if df is None or df.empty:
        return None

    df["ema_fast"] = ema(df["Close"], EMA_FAST)
    df["ema_slow"] = ema(df["Close"], EMA_SLOW)
    df["rsi"] = rsi(df["Close"], RSI_PERIOD)
    df["atr"] = atr(df, ATR_PERIOD)
    df["avg_vol"] = df["Volume"].rolling(VOLUME_LOOKBACK).mean()
    df["high"] = df["High"].rolling(BREAKOUT_LOOKBACK).max().shift(1)

    df.dropna(inplace=True)
    return df

# =========================
# SIGNAL
# =========================
def check_signal():
    global in_trade, entry, stop, tp, highest, break_even

    df = get_data()
    if df is None:
        return

    row = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(row["Close"])
    atr_now = float(row["atr"])
    rsi_now = float(row["rsi"])
    ema_fast = float(row["ema_fast"])
    ema_slow = float(row["ema_slow"])
    vol = float(row["Volume"])
    avg_vol = float(row["avg_vol"])
    high = float(row["high"])

    score = 0

    if ema_fast > ema_slow:
        score += 25
    if RSI_MIN <= rsi_now <= RSI_MAX:
        score += 20
    if vol > avg_vol * RVOL_MIN:
        score += 20
    if price > high * (1 + BREAKOUT_BUFFER):
        score += 20
    if price > float(prev["Close"]):
        score += 15

    # =========================
    # ENTRY
    # =========================
    if not in_trade and score >= ALERT_SCORE:
        entry = price
        stop = price - atr_now * ATR_SL_MULT
        tp = price + atr_now * ATR_TP_MULT
        highest = price
        break_even = False
        in_trade = True

        send(
            f"🚨 BTC TRADE 🚨\n"
            f"Price: ${price:.2f}\n"
            f"Score: {score}\n\n"
            f"TP: ${tp:.2f}\n"
            f"SL: ${stop:.2f}"
        )
        return

    # =========================
    # TRADE MANAGEMENT
    # =========================
    if not in_trade:
        return

    highest = max(highest, price)

    # 🔥 BREAK EVEN
    if not break_even and price >= entry + atr_now:
        stop = entry
        break_even = True
        send(f"⚡ BTC BREAK EVEN\nNew SL: ${stop:.2f}")

    # 🔥 TRAILING STOP
    new_trail = highest - atr_now * TRAIL_MULT
    if new_trail > stop:
        stop = new_trail
        send(f"📈 TRAILING STOP UPDATED\nSL: ${stop:.2f}")

    # 🔥 WEAKNESS
    if rsi_now > 70 and price < prev["Close"]:
        send("⚠️ BTC MOMENTUM WEAKENING")

    # =========================
    # EXIT
    # =========================
    if price <= stop:
        send(f"❌ BTC STOP HIT\nPrice: ${price:.2f}")
        in_trade = False

    elif price >= tp:
        send(f"🎯 BTC TARGET HIT\nPrice: ${price:.2f}")
        in_trade = False

# =========================
# APP LOOP
# =========================
app = Flask(name)

@app.route("/")
def home():
    return "BTC ELITE BOT RUNNING"

def run():
    send("BTC ELITE MODE ACTIVATED 🚀")

    while True:
        try:
            check_signal()
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print("Error:", e)
            time.sleep(CHECK_INTERVAL)

if name == "main":
    run()
