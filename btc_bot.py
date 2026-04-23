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

LONG_SCORE_THRESHOLD = 70
SHORT_SCORE_THRESHOLD = 70

DEBUG = True

ASSETS = {
    "BTC": "BTC-USD",
    "GOLD": "GC=F",  # FIXED GOLD
}

# =========================
# STATE
# =========================
STATE = {
    a: {"last_heartbeat": 0}
    for a in ASSETS
}

# =========================
# TELEGRAM
# =========================
def send(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        print("Telegram error")

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

    df["rsi"] = 100 - (100 / (1 + (
        df["close"].diff().clip(lower=0).rolling(14).mean() /
        df["close"].diff().clip(upper=0).abs().rolling(14).mean()
    )))

    df["atr"] = (df["high"] - df["low"]).rolling(14).mean()
    df["vol_ma"] = df["volume"].rolling(20).mean()

    df.dropna(inplace=True)
    return df

# =========================
# TREND
# =========================
def get_trend(df1, df5):
    r1 = df1.iloc[-1]
    r5 = df5.iloc[-1]

    if r5["ema9"] > r5["ema21"] and r1["ema9"] > r1["ema21"]:
        return "BULLISH"

    if r5["ema9"] < r5["ema21"] and r1["ema9"] < r1["ema21"]:
        return "BEARISH"

    return "CHOPPY"

# =========================
# FILTER (ANTI BAD ENTRY)
# =========================
def clean_entry(df):
    r = df.iloc[-1]
    p = df.iloc[-2]

    move = abs(r["close"] - p["close"])

    if r["atr"] > 0 and move > r["atr"] * 1.2:
        return False  # prevents chasing

    return True

# =========================
# SCORING
# =========================
def long_score(df1, df5):
    r = df1.iloc[-1]
    score = 0

    if get_trend(df1, df5) == "BULLISH":
        score += 30

    if r["ema9"] > r["ema21"]:
        score += 20

    if 50 < r["rsi"] < 70:
        score += 15

    if r["volume"] > r["vol_ma"]:
        score += 15

    if r["close"] > r["ema9"]:
        score += 10

    return score

def short_score(df1, df5):
    r = df1.iloc[-1]
    score = 0

    if get_trend(df1, df5) == "BEARISH":
        score += 30

    if r["ema9"] < r["ema21"]:
        score += 20

    if 30 < r["rsi"] < 50:
        score += 15

    if r["volume"] > r["vol_ma"]:
        score += 15

    if r["close"] < r["ema9"]:
        score += 10

    return score

# =========================
# ENTRY RULES (FIXED)
# =========================
def long_entry(df):
    r = df.iloc[-1]
    return r["close"] <= r["ema9"] * 1.002  # ONLY pullbacks

def short_entry(df):
    r = df.iloc[-1]
    return r["close"] >= r["ema9"] * 0.998

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset, price):
    now = time.time()

    if now - STATE[asset]["last_heartbeat"] < HEARTBEAT_SECONDS:
        return

    if price is None:
        send(f"💓 {asset} HEARTBEAT\nStatus: NO DATA")
    else:
        send(f"💓 {asset} HEARTBEAT\nPrice: {round(price,2)}")

    STATE[asset]["last_heartbeat"] = now

# =========================
# MAIN LOOP
# =========================
def run():
    send("🔥 BTC + GOLD BOT LIVE 🔥")

    while True:
        try:
            for asset, symbol in ASSETS.items():

                df1 = add_indicators(get_data(symbol, "1m"))
                df5 = add_indicators(get_data(symbol, "5m"))

                if df1.empty or df5.empty:
                    heartbeat(asset, None)
                    continue

                price = df1.iloc[-1]["close"]
                heartbeat(asset, price)

                if not clean_entry(df1):
                    continue

                l = long_score(df1, df5)
                s = short_score(df1, df5)

                if DEBUG:
                    print(asset, "L:", l, "S:", s)

                if l >= LONG_SCORE_THRESHOLD and long_entry(df1):
                    send(f"🚀 {asset} LONG\nPrice: {round(price,2)}")

                elif s >= SHORT_SCORE_THRESHOLD and short_entry(df1):
                    send(f"📉 {asset} SHORT\nPrice: {round(price,2)}")

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
