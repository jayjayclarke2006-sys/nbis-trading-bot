import os
import time
import requests
import pandas as pd
import yfinance as yf

# =========================
# ENV
# =========================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# =========================
# CONFIG
# =========================
CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 1800
COOLDOWN_SECONDS = 600

LONG_SCORE_TRIGGER = 65
SHORT_SCORE_TRIGGER = 65

# =========================
# ASSETS
# =========================
ASSETS = {
    "BTC": {"binance": "BTCUSDT", "yf": "BTC-USD"},
    "GOLD": {"yf": "GC=F"},
}

# =========================
# STATE
# =========================
STATE = {
    a: {
        "IN_TRADE": False,
        "SIDE": None,
        "ENTRY": 0,
        "SL": 0,
        "TP": 0,
        "LAST_TRADE": 0,
        "HB": 0,
    }
    for a in ASSETS
}

# =========================
# TELEGRAM
# =========================
def send(msg):
    print(msg)

    if not TOKEN or not CHAT_ID:
        return

    for _ in range(5):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )
            return
        except:
            time.sleep(2)

# =========================
# DATA (FIXED)
# =========================
def get_binance(symbol):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 500},
            timeout=10,
        )
        d = r.json()
        df = pd.DataFrame(d)[[1,2,3,4,5]]
        df.columns = ["open","high","low","close","volume"]
        return df.astype(float)
    except:
        return pd.DataFrame()

def get_yf(symbol):
    try:
        df = yf.download(symbol, period="5d", interval="1m", progress=False)
        df.columns = [c.lower() for c in df.columns]
        return df[["open","high","low","close","volume"]]
    except:
        return pd.DataFrame()

def get_data(asset):
    if asset == "BTC":
        df = get_binance("BTCUSDT")
        if not df.empty:
            return df, "BINANCE"

    df = get_yf(ASSETS[asset]["yf"])
    if not df.empty:
        return df, "YF"

    return pd.DataFrame(), "NONE"

# =========================
# INDICATORS
# =========================
def indicators(df):
    if len(df) < 50:
        return None

    df["ema9"] = df["close"].ewm(span=9).mean()
    df["ema21"] = df["close"].ewm(span=21).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()

    df["rsi"] = df["close"].pct_change().rolling(14).mean() * 100
    df["atr"] = (df["high"] - df["low"]).rolling(14).mean()

    df["hh"] = df["high"].rolling(10).max().shift(1)
    df["ll"] = df["low"].rolling(10).min().shift(1)

    df.dropna(inplace=True)
    return df

# =========================
# TREND
# =========================
def trend(df):
    r = df.iloc[-1]
    if r["ema9"] > r["ema21"]:
        return "BULL"
    if r["ema9"] < r["ema21"]:
        return "BEAR"
    return "CHOP"

# =========================
# SIGNAL ENGINE
# =========================
def get_signal(asset):
    df, feed = get_data(asset)
    df = indicators(df)

    if df is None:
        return None, feed

    r = df.iloc[-1]
    t = trend(df)

    breakout_long = r["close"] > r["hh"]
    breakout_short = r["close"] < r["ll"]

    sniper_long = r["close"] > r["ema9"] and r["rsi"] > 45
    sniper_short = r["close"] < r["ema9"] and r["rsi"] < 55

    pullback_long = r["close"] <= r["ema9"]
    pullback_short = r["close"] >= r["ema9"]

    score_long = 0
    score_short = 0

    if t == "BULL":
        score_long += 30
    if breakout_long:
        score_long += 30
    if sniper_long:
        score_long += 20
    if pullback_long:
        score_long += 10

    if t == "BEAR":
        score_short += 30
    if breakout_short:
        score_short += 30
    if sniper_short:
        score_short += 20
    if pullback_short:
        score_short += 10

    return {
        "price": r["close"],
        "atr": r["atr"],
        "trend": t,
        "long_score": score_long,
        "short_score": score_short,
    }, feed

# =========================
# TRADE
# =========================
def start_trade(asset, side, sig):
    s = STATE[asset]
    price = sig["price"]
    atr = sig["atr"]

    if side == "LONG":
        sl = price - atr * 2.5
        tp = price + atr * 5
    else:
        sl = price + atr * 2.5
        tp = price - atr * 5

    s.update({
        "IN_TRADE": True,
        "SIDE": side,
        "ENTRY": price,
        "SL": sl,
        "TP": tp,
        "LAST_TRADE": time.time(),
    })

    send(
        f"🚀 {asset} {side}\n\n"
        f"Entry: {price:.2f}\n"
        f"SL: {sl:.2f}\n"
        f"TP: {tp:.2f}"
    )

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset, sig):
    s = STATE[asset]

    if time.time() - s["HB"] < HEARTBEAT_SECONDS:
        return

    if sig:
        send(f"💓 {asset} | {sig['trend']} | {sig['price']:.2f}")
    else:
        send(f"💓 {asset} | NO DATA")

    s["HB"] = time.time()

# =========================
# MAIN
# =========================
def run():
    time.sleep(8)
    send("🔥 BTC + GOLD BOT LIVE 🔥")

    while True:
        try:
            for a in ASSETS:
                sig, feed = get_signal(a)

                heartbeat(a, sig)

                if sig is None:
                    continue

                if STATE[a]["IN_TRADE"]:
                    continue

                if time.time() - STATE[a]["LAST_TRADE"] < COOLDOWN_SECONDS:
                    continue

                if sig["long_score"] >= LONG_SCORE_TRIGGER:
                    start_trade(a, "LONG", sig)

                elif sig["short_score"] >= SHORT_SCORE_TRIGGER:
                    start_trade(a, "SHORT", sig)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
