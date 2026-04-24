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
HEARTBEAT = 1800
COOLDOWN = 600

# =========================
# ASSETS
# =========================
ASSETS = {
    "BTC": {"binance": "BTCUSDT", "yf": "BTC-USD"},
    "GOLD": {"yf": "GC=F"}
}

# =========================
# STATE
# =========================
STATE = {
    a: {"trade": False, "last": 0, "hb": 0}
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
# DATA
# =========================
def get_binance(symbol):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 200},
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
            return df

    return get_yf(ASSETS[asset]["yf"])

# =========================
# INDICATORS
# =========================
def indicators(df):
    if len(df) < 50:
        return None

    df["ema9"] = df["close"].ewm(span=9).mean()
    df["ema21"] = df["close"].ewm(span=21).mean()

    df["rsi"] = df["close"].pct_change().rolling(14).mean() * 100
    df["atr"] = (df["high"] - df["low"]).rolling(14).mean()

    df["hh"] = df["high"].rolling(10).max().shift(1)
    df["ll"] = df["low"].rolling(10).min().shift(1)

    df.dropna(inplace=True)
    return df

# =========================
# SIGNAL LOGIC
# =========================
def signal(asset):
    df = get_data(asset)
    df = indicators(df)

    if df is None:
        return None

    r = df.iloc[-1]

    trend = "BULL" if r["ema9"] > r["ema21"] else "BEAR"

    breakout_long = r["close"] > r["hh"]
    breakout_short = r["close"] < r["ll"]

    sniper_long = r["close"] > r["ema9"] and r["rsi"] > 50
    sniper_short = r["close"] < r["ema9"] and r["rsi"] < 50

    if trend == "BULL" and (breakout_long or sniper_long):
        return {"side": "LONG", "price": r["close"], "atr": r["atr"]}

    if trend == "BEAR" and (breakout_short or sniper_short):
        return {"side": "SHORT", "price": r["close"], "atr": r["atr"]}

    return None

# =========================
# TRADE
# =========================
def trade(asset, sig):
    price = sig["price"]
    atr = sig["atr"]

    if sig["side"] == "LONG":
        sl = price - atr * 2.5
        tp = price + atr * 5
    else:
        sl = price + atr * 2.5
        tp = price - atr * 5

    send(f"{asset} {sig['side']}\nEntry:{price:.2f}\nSL:{sl:.2f}\nTP:{tp:.2f}")

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset):
    s = STATE[asset]
    if time.time() - s["hb"] < HEARTBEAT:
        return
    send(f"💓 {asset} ALIVE")
    s["hb"] = time.time()

# =========================
# MAIN
# =========================
def run():
    time.sleep(8)
    send("🔥 BTC + GOLD BOT LIVE 🔥")

    while True:
        try:
            for a in ASSETS:
                heartbeat(a)

                if STATE[a]["trade"]:
                    continue

                if time.time() - STATE[a]["last"] < COOLDOWN:
                    continue

                sig = signal(a)

                if sig:
                    trade(a, sig)
                    STATE[a]["last"] = time.time()

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
