import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np

# =========================
# CONFIG
# =========================
SYMBOL = "BTC-USD"
LOW_TF = "5m"
HIGH_TF = "15m"
PERIOD = "5d"

ALERT_SCORE = 65
CHECK_INTERVAL = 300

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

VOL_LOOKBACK = 20
STRUCT_LOOKBACK = 12

ATR_SL = 1.5
ATR_TP = 3.0
TRAIL_MULT = 2.0

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
def get_df(tf):
    df = yf.download(SYMBOL, period=PERIOD, interval=tf, progress=False)
    if df is None or df.empty:
        return None

    df["ema_fast"] = ema(df["Close"], EMA_FAST)
    df["ema_slow"] = ema(df["Close"], EMA_SLOW)
    df["rsi"] = rsi(df["Close"], RSI_PERIOD)
    df["atr"] = atr(df, ATR_PERIOD)
    df["avg_vol"] = df["Volume"].rolling(VOL_LOOKBACK).mean()
    df["high"] = df["High"].rolling(STRUCT_LOOKBACK).max().shift(1)
    df["low"] = df["Low"].rolling(STRUCT_LOOKBACK).min().shift(1)

    df.dropna(inplace=True)
    return df

# =========================
# MAIN LOGIC
# =========================
def check():
    global in_trade, entry, stop, tp, highest, break_even

    df5 = get_df(LOW_TF)
    df15 = get_df(HIGH_TF)

    if df5 is None or df15 is None:
        return

    r5 = df5.iloc[-1]
    p5 = df5.iloc[-2]
    r15 = df15.iloc[-1]

    price = float(r5["Close"])
    atr_now = float(r5["atr"])
    rsi_now = float(r5["rsi"])

    # =========================
    # TREND FILTER (15m)
    # =========================
    trend_up = r15["ema_fast"] > r15["ema_slow"]

    # =========================
    # SCORING SYSTEM
    # =========================
    score = 0

    if trend_up:
        score += 25

    if r5["ema_fast"] > r5["ema_slow"]:
        score += 20

    if 52 <= rsi_now <= 70:
        score += 15

    if r5["Volume"] > r5["avg_vol"] * 1.3:
        score += 15

    breakout = price > r5["high"] * 1.002
    if breakout:
        score += 15

    if price > p5["Close"]:
        score += 10

    # =========================
    # ENTRY
    # =========================
    if not in_trade and score >= ALERT_SCORE and trend_up:
        entry = price
        stop = price - atr_now * ATR_SL
        tp = price + atr_now * ATR_TP
        highest = price
        break_even = False
        in_trade = True

        send(
            f"🚀 BTC ELITE ENTRY\n"
            f"Price: ${price:.2f}\n"
            f"Score: {score}\n"
            f"Trend: BULLISH\n\n"
            f"TP: ${tp:.2f}\n"
            f"SL: ${stop:.2f}"
        )
        return

    # =========================
    # MANAGEMENT
    # =========================
    if not in_trade:
        return

    highest = max(highest, price)

    # BREAK EVEN
    if not break_even and price >= entry + atr_now:
        stop = entry
        break_even = True
        send(f"⚡ MOVE TO BREAK EVEN\nSL: ${stop:.2f}")

    # TRAILING
    trail = highest - atr_now * TRAIL_MULT
    if trail > stop:
        stop = trail
        send(f"📈 TRAILING STOP\nSL: ${stop:.2f}")

    # WEAKNESS
    if rsi_now > 70 and price < p5["Close"]:
        send("⚠️ BTC TOPPING WARNING")

    # TREND LOSS EXIT
    if not (r5["ema_fast"] > r5["ema_slow"]):
        send("⚠️ TREND BREAK — EXIT SOON")

    # =========================
    # EXIT
    # =========================
    if price <= stop:
        send(f"❌ STOP HIT\nPrice: ${price:.2f}")
        in_trade = False

    elif price >= tp:
        send(f"🎯 TARGET HIT\nPrice: ${price:.2f}")
        in_trade = False

# =========================
# RUN LOOP
# =========================
def run():
    send("🔥 BTC ELITE SYSTEM LIVE 🔥")

    while True:
        try:
            check()
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print("Error:", e)
            time.sleep(CHECK_INTERVAL)

if name == "main":
    run(
