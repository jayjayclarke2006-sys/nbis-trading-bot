import os

import time

import requests

import pandas as pd

import yfinance as yf

# =========================

# ENV

# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

CHAT_ID = os.getenv("CHAT_ID")

# =========================

# CONFIG

# =========================

CHECK_INTERVAL = 60

HEARTBEAT_SECONDS = 900

LONG_SCORE_THRESHOLD = 65

SHORT_SCORE_THRESHOLD = 65

DEBUG = True

ASSETS = {

    "BTC": "BTC-USD",

    "GOLD": "GC=F",

}

# =========================

# STATE

# =========================

STATE = {

    a: {

        "in_trade": False,

        "side": None,

        "entry": 0,

        "sl": 0,

        "tp": 0,

        "highest": 0,

        "lowest": 0,

        "last_heartbeat": 0,

        "break_even": False,

        "partial": False

    }

    for a in ASSETS

}

# =========================

# TELEGRAM

# =========================

def send(msg):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print(msg)

        return

    for _ in range(3):

        try:

            requests.post(

                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",

                json={"chat_id": CHAT_ID, "text": msg},

                timeout=10

            )

            return

        except:

            time.sleep(2)

# =========================

# DATA

# =========================

def get_data(symbol, interval):

    try:

        df = yf.download(symbol, period="7d", interval=interval, progress=False)

        if df is None or df.empty:

            return pd.DataFrame()

        df.columns = [c.lower() for c in df.columns]

        return df

    except:

        return pd.DataFrame()

# =========================

# INDICATORS

# =========================

def add_indicators(df):

    if df.empty or len(df) < 30:

        return pd.DataFrame()

    df["ema9"] = df["close"].ewm(span=9).mean()

    df["ema21"] = df["close"].ewm(span=21).mean()

    delta = df["close"].diff()

    gain = delta.clip(lower=0).rolling(14).mean()

    loss = delta.clip(upper=0).abs().rolling(14).mean()

    rs = gain / loss.replace(0, pd.NA)

    df["rsi"] = 100 - (100 / (1 + rs))

    df["atr"] = (df["high"] - df["low"]).rolling(14).mean()

    df["vol_ma"] = df["volume"].rolling(20).mean()

    df.dropna(inplace=True)

    return df

# =========================

# TREND + BIAS

# =========================

def trend(df1, df5):

    r1 = df1.iloc[-1]

    r5 = df5.iloc[-1]

    if r5["ema9"] > r5["ema21"] and r1["ema9"] > r1["ema21"]:

        return "BULL"

    if r5["ema9"] < r5["ema21"] and r1["ema9"] < r1["ema21"]:

        return "BEAR"

    return "CHOP"

def htf_bias(df15):

    r = df15.iloc[-1]

    if r["ema9"] > r["ema21"]:

        return "BULL"

    if r["ema9"] < r["ema21"]:

        return "BEAR"

    return "NEUTRAL"

# =========================

# ENTRY TYPES

# =========================

def breakout_long(df):

    r = df.iloc[-1]

    return r["close"] > df["high"].rolling(10).max().iloc[-2]

def breakout_short(df):

    r = df.iloc[-1]

    return r["close"] < df["low"].rolling(10).min().iloc[-2]

def pullback_long(df):

    r = df.iloc[-1]

    return r["close"] <= r["ema9"] * 1.002

def pullback_short(df):

    r = df.iloc[-1]

    return r["close"] >= r["ema9"] * 0.998

# =========================

# SCORING

# =========================

def long_score(df1, df5, df15):

    r = df1.iloc[-1]

    score = 0

    if trend(df1, df5) == "BULL":

        score += 25

    if htf_bias(df15) == "BULL":

        score += 20

    if r["ema9"] > r["ema21"]:

        score += 15

    if 50 < r["rsi"] < 70:

        score += 15

    if r["volume"] > r["vol_ma"]:

        score += 10

    return score

def short_score(df1, df5, df15):

    r = df1.iloc[-1]

    score = 0

    if trend(df1, df5) == "BEAR":

        score += 25

    if htf_bias(df15) == "BEAR":

        score += 20

    if r["ema9"] < r["ema21"]:

        score += 15

    if 30 < r["rsi"] < 50:

        score += 15

    if r["volume"] > r["vol_ma"]:

        score += 10

    return score

# =========================

# HEARTBEAT

# =========================

def heartbeat(asset, price):

    now = time.time()

    s = STATE[asset]

    if now - s["last_heartbeat"] < HEARTBEAT_SECONDS:

        return

    send(f"💓 {asset} HEARTBEAT\nPrice: {price if price else 'No data'}")

    s["last_heartbeat"] = now

# =========================

# TRADE MANAGEMENT

# =========================

def manage(asset, price, atr):

    s = STATE[asset]

    if not s["in_trade"]:

        return

    if s["side"] == "LONG":

        s["highest"] = max(s["highest"], price)

        if not s["break_even"] and price >= s["entry"] + atr:

            s["sl"] = s["entry"]

            s["break_even"] = True

            send(f"⚡ {asset} BREAK EVEN")

        trail = s["highest"] - atr * 1.5

        if trail > s["sl"]:

            s["sl"] = trail

            send(f"📈 {asset} TRAILING STOP: {round(s['sl'],2)}")

        if price <= s["sl"]:

            send(f"❌ {asset} EXIT\nPrice: {price}")

            s["in_trade"] = False

    else:

        s["lowest"] = min(s["lowest"], price)

        if not s["break_even"] and price <= s["entry"] - atr:

            s["sl"] = s["entry"]

            s["break_even"] = True

            send(f"⚡ {asset} BREAK EVEN")

        trail = s["lowest"] + atr * 1.5

        if trail < s["sl"]:

            s["sl"] = trail

            send(f"📉 {asset} TRAILING STOP: {round(s['sl'],2)}")

        if price >= s["sl"]:

            send(f"❌ {asset} EXIT\nPrice: {price}")

            s["in_trade"] = False

# =========================

# MAIN LOOP

# =========================

def run():

    time.sleep(5)

    send("🔥 BOT LIVE 🔥")

    while True:

        try:

            for asset, symbol in ASSETS.items():

                df1 = add_indicators(get_data(symbol, "1m"))

                df5 = add_indicators(get_data(symbol, "5m"))

                df15 = add_indicators(get_data(symbol, "15m"))

                if df1.empty or df5.empty or df15.empty:

                    heartbeat(asset, None)

                    continue

                price = df1.iloc[-1]["close"]

                atr = df1.iloc[-1]["atr"]

                heartbeat(asset, price)

                manage(asset, price, atr)

                if STATE[asset]["in_trade"]:

                    continue

                l = long_score(df1, df5, df15)

                s = short_score(df1, df5, df15)

                if DEBUG:

                    print(asset, l, s)

                if l >= LONG_SCORE_THRESHOLD and (pullback_long(df1) or breakout_long(df1)):

                    STATE[asset].update({

                        "in_trade": True,

                        "side": "LONG",

                        "entry": price,

                        "sl": price - atr * 1.5,

                        "tp": price + atr * 3,

                        "highest": price

                    })

                    send(f"🚀 {asset} LONG ENTRY\nPrice: {price}")

                elif s >= SHORT_SCORE_THRESHOLD and (pullback_short(df1) or breakout_short(df1)):

                    STATE[asset].update({

                        "in_trade": True,

                        "side": "SHORT",

                        "entry": price,

                        "sl": price + atr * 1.5,

                        "tp": price - atr * 3,

                        "lowest": price

                    })

                    send(f"📉 {asset} SHORT ENTRY\nPrice: {price}")

            time.sleep(CHECK_INTERVAL)

        except Exception as e:

            send(f"ERROR: {e}")

            time.sleep(10)

if __name__ == "__main__":

    run()
