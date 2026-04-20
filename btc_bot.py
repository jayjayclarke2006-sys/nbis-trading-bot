import os
import time
import requests
import pandas as pd

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

IN_TRADE = False
TRADE_SIDE = None
ENTRY_PRICE = 0.0
STOP_LOSS = 0.0
TAKE_PROFIT = 0.0

def send(msg):
    try:
        if not TELEGRAM_TOKEN or not CHAT_ID:
            print(msg)
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

def get_klines(interval):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit=120"
        r = requests.get(url, timeout=10)
        data = r.json()

        if not isinstance(data, list) or len(data) < 30:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "time", "open", "high", "low", "close", "volume",
            "ct", "qav", "nt", "tbv", "tqv", "ignore"
        ])

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df.dropna(inplace=True)
        return df
    except Exception:
        return pd.DataFrame()

def ema(df, span):
    return df["close"].ewm(span=span, adjust=False).mean()

def rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return out

def atr(df, period=14):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def add_indicators(df):
    if df.empty or len(df) < 30:
        return pd.DataFrame()

    out = df.copy()
    out["ema9"] = ema(out, 9)
    out["ema21"] = ema(out, 21)
    out["ema50"] = ema(out, 50)
    out["rsi"] = rsi(out, 14)
    out["atr"] = atr(out, 14)
    out.dropna(inplace=True)

    if len(out) < 30:
        return pd.DataFrame()

    return out

def get_signal():
    df1 = add_indicators(get_klines("1m"))
    df5 = add_indicators(get_klines("5m"))

    if df1.empty or df5.empty:
        return None
    if len(df1) < 25 or len(df5) < 25:
        return None

    latest1 = df1.iloc[-1]
    prev1 = df1.iloc[-2]
    latest5 = df5.iloc[-1]

    price = float(latest1["close"])
    atr_val = float(latest1["atr"])

    long_score = 0
    short_score = 0

    if latest5["ema9"] > latest5["ema21"]:
        long_score += 30
    else:
        short_score += 30

    if latest1["rsi"] > 55:
        long_score += 20
    if latest1["rsi"] < 45:
        short_score += 20

    sniper_long = bool(prev1["ema9"] < prev1["ema21"] and latest1["ema9"] > latest1["ema21"])
    sniper_short = bool(prev1["ema9"] > prev1["ema21"] and latest1["ema9"] < latest1["ema21"])

    hh10 = df1["high"].rolling(10).max().shift(1)
    ll10 = df1["low"].rolling(10).min().shift(1)

    if len(hh10.dropna()) < 2 or len(ll10.dropna()) < 2:
        return None

    breakout_long = bool(price > float(hh10.iloc[-1]))
    breakout_short = bool(price < float(ll10.iloc[-1]))

    return {
        "price": price,
        "atr": atr_val,
        "long_score": long_score,
        "short_score": short_score,
        "sniper_long": sniper_long,
        "sniper_short": sniper_short,
        "breakout_long": breakout_long,
        "breakout_short": breakout_short
    }

def run():
    global IN_TRADE, TRADE_SIDE, ENTRY_PRICE, STOP_LOSS, TAKE_PROFIT

    send("🔥 BTC SAFE BOT LIVE 🔥")

    while True:
        try:
            sig = get_signal()

            if sig is None:
                time.sleep(15)
                continue

            price = sig["price"]

            if not IN_TRADE:
                if sig["long_score"] >= 60 and (sig["sniper_long"] or sig["breakout_long"]):
                    IN_TRADE = True
                    TRADE_SIDE = "LONG"
                    ENTRY_PRICE = price
                    STOP_LOSS = price - sig["atr"] * 1.5
                    TAKE_PROFIT = price + sig["atr"] * 3.0

                    send(
                        f"🚀 BTC LONG ENTRY\n\n"
                        f"Price: {price:.2f}\n"
                        f"Score: {sig['long_score']}\n"
                        f"SL: {STOP_LOSS:.2f}\n"
                        f"TP: {TAKE_PROFIT:.2f}"
                    )

                elif sig["short_score"] >= 60 and (sig["sniper_short"] or sig["breakout_short"]):
                    IN_TRADE = True
                    TRADE_SIDE = "SHORT"
                    ENTRY_PRICE = price
                    STOP_LOSS = price + sig["atr"] * 1.5
                    TAKE_PROFIT = price - sig["atr"] * 3.0

                    send(
                        f"📉 BTC SHORT ENTRY\n\n"
                        f"Price: {price:.2f}\n"
                        f"Score: {sig['short_score']}\n"
                        f"SL: {STOP_LOSS:.2f}\n"
                        f"TP: {TAKE_PROFIT:.2f}"
                    )

            else:
                if TRADE_SIDE == "LONG":
                    if price <= STOP_LOSS:
                        send(f"❌ BTC LONG STOP HIT\nExit: {price:.2f}")
                        IN_TRADE = False
                        TRADE_SIDE = None
                    elif price >= TAKE_PROFIT:
                        send(f"🎯 BTC LONG TARGET HIT\nExit: {price:.2f}")
                        IN_TRADE = False
                        TRADE_SIDE = None

                elif TRADE_SIDE == "SHORT":
                    if price >= STOP_LOSS:
                        send(f"❌ BTC SHORT STOP HIT\nExit: {price:.2f}")
                        IN_TRADE = False
                        TRADE_SIDE = None
                    elif price <= TAKE_PROFIT:
                        send(f"🎯 BTC SHORT TARGET HIT\nExit: {price:.2f}")
                        IN_TRADE = False
                        TRADE_SIDE = None

            time.sleep(60)

        except Exception as e:
            send(f"BTC BOT ERROR: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run()
