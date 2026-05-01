import os
import time
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

# =========================
# CONFIG
# =========================
TIMEZONE = "Europe/London"
CHECK_INTERVAL = 60

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

ASSETS = ["BTCUSDT"]

RR_TARGET = 2.0
HTF_EMA_LEN = 50
ATR_LEN = 14

MIN_BODY_ATR = 0.35
MAX_BODY_ATR = 1.8
RETEST_BUFFER_ATR = 0.15

# =========================
# STATE
# =========================
STATE = {
    a: {
        "range_high": None,
        "range_low": None,
        "range_set": False,
        "break_side": None,
        "retest_done": False,
        "last_update": None,
        "traded": False,
    }
    for a in ASSETS
}

# =========================
# TELEGRAM
# =========================
def send(msg):
    print(msg)
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print("TELEGRAM ERROR:", e)

# =========================
# DATA
# =========================
def get_binance(symbol, interval="15m", limit=300):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        data = r.json()
        if not isinstance(data, list):
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "ct","q","n","tb","tq","ig"
        ])

        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return df.reset_index(drop=True)

    except Exception as e:
        print("DATA ERROR:", e)
        return pd.DataFrame()

# =========================
# INDICATORS
# =========================
def add_indicators(df):
    if df.empty or len(df) < 60:
        return pd.DataFrame()

    out = df.copy()
    out["ema50"] = out["close"].ewm(span=HTF_EMA_LEN, adjust=False).mean()

    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - out["close"].shift()).abs(),
        (out["low"] - out["close"].shift()).abs(),
    ], axis=1).max(axis=1)

    out["atr"] = tr.rolling(ATR_LEN).mean()
    out["body"] = (out["close"] - out["open"]).abs()
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out.dropna(inplace=True)
    return out.reset_index(drop=True)

# =========================
# TIME
# =========================
def now():
    return datetime.now(ZoneInfo(TIMEZONE))

# =========================
# FILTERS
# =========================
def htf_bias(asset):
    df = add_indicators(get_binance(asset, "1h", 300))
    if df.empty:
        return "NONE"

    r = df.iloc[-1]
    p = df.iloc[-2]

    if r["close"] > r["ema50"] and r["ema50"] > p["ema50"]:
        return "BULL"

    if r["close"] < r["ema50"] and r["ema50"] < p["ema50"]:
        return "BEAR"

    return "CHOP"

def strong_rejection(open_, high, low, close, side):
    body = abs(close - open_)
    if body <= 0:
        return False

    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low

    if side == "LONG":
        return close > open_ and lower_wick >= body * 0.8

    if side == "SHORT":
        return close < open_ and upper_wick >= body * 0.8

    return False

def candle_quality(r):
    atr = r["atr"]
    body = r["body"]

    if atr <= 0:
        return False

    body_atr = body / atr

    if body_atr < MIN_BODY_ATR:
        return False

    if body_atr > MAX_BODY_ATR:
        return False

    return True

def not_choppy(df):
    recent = df.tail(12)
    avg_range = (recent["high"] - recent["low"]).mean()
    atr = df.iloc[-1]["atr"]

    if atr <= 0:
        return False

    return avg_range >= atr * 0.75

# =========================
# RANGE LOGIC
# =========================
def update_range(asset):
    df30 = get_binance(asset, "30m", 100)
    if df30.empty:
        return

    t = now()
    s = STATE[asset]

    # 08:00 candle closes at 08:30
    if t.hour == 8 and t.minute == 30 and s["last_update"] != "08":
        candle = df30.iloc[-1]

        s["range_high"] = candle["high"]
        s["range_low"] = candle["low"]
        s["range_set"] = True
        s["break_side"] = None
        s["retest_done"] = False
        s["traded"] = False
        s["last_update"] = "08"

        send(
            f"🔥 {asset} 08:00 RANGE SET\n\n"
            f"High: {candle['high']:.2f}\n"
            f"Low: {candle['low']:.2f}"
        )

    # 16:00 candle closes at 16:30
    if t.hour == 16 and t.minute == 30 and s["last_update"] != "16":
        candle = df30.iloc[-1]

        s["range_high"] = candle["high"]
        s["range_low"] = candle["low"]
        s["range_set"] = True
        s["break_side"] = None
        s["retest_done"] = False
        s["traded"] = False
        s["last_update"] = "16"

        send(
            f"🔥 {asset} 16:00 RANGE SET\n\n"
            f"High: {candle['high']:.2f}\n"
            f"Low: {candle['low']:.2f}"
        )

# =========================
# TRADE LOGIC
# =========================
def check_trade(asset):
    s = STATE[asset]

    if not s["range_set"] or s["traded"]:
        return

    df15 = add_indicators(get_binance(asset, "15m", 300))
    if df15.empty:
        return

    bias = htf_bias(asset)
    if bias == "CHOP" or bias == "NONE":
        return

    if not not_choppy(df15):
        return

    r = df15.iloc[-1]

    close = r["close"]
    high = r["high"]
    low = r["low"]
    open_ = r["open"]
    atr = r["atr"]

    # STEP 1: 15m break with HTF alignment
    if s["break_side"] is None:
        if close > s["range_high"] and bias == "BULL" and candle_quality(r):
            s["break_side"] = "LONG"
            send(f"✅ {asset} 15M BREAK ABOVE RANGE\nWaiting for retest.")

        elif close < s["range_low"] and bias == "BEAR" and candle_quality(r):
            s["break_side"] = "SHORT"
            send(f"✅ {asset} 15M BREAK BELOW RANGE\nWaiting for retest.")

        return

    # STEP 2: retest with buffer
    if s["break_side"] == "LONG" and not s["retest_done"]:
        if low <= s["range_high"] + atr * RETEST_BUFFER_ATR:
            s["retest_done"] = True
            send(f"📍 {asset} LONG RETEST HIT\nWaiting for rejection.")

    if s["break_side"] == "SHORT" and not s["retest_done"]:
        if high >= s["range_low"] - atr * RETEST_BUFFER_ATR:
            s["retest_done"] = True
            send(f"📍 {asset} SHORT RETEST HIT\nWaiting for rejection.")

    # STEP 3: rejection entry
    if s["break_side"] == "LONG" and s["retest_done"]:
        if bias == "BULL" and strong_rejection(open_, high, low, close, "LONG") and candle_quality(r):
            entry = close
            sl = min(low, s["range_low"])
            risk = entry - sl

            if risk <= 0:
                return

            tp = entry + risk * RR_TARGET

            send(
                f"🚀 {asset} LONG ENTRY\n\n"
                f"Model: 08/16 Range Break + Retest\n"
                f"HTF Bias: {bias}\n"
                f"Entry: {entry:.2f}\n"
                f"SL: {sl:.2f}\n"
                f"TP: {tp:.2f}\n"
                f"RR: 1:{RR_TARGET}"
            )

            s["traded"] = True
            s["range_set"] = False

    if s["break_side"] == "SHORT" and s["retest_done"]:
        if bias == "BEAR" and strong_rejection(open_, high, low, close, "SHORT") and candle_quality(r):
            entry = close
            sl = max(high, s["range_high"])
            risk = sl - entry

            if risk <= 0:
                return

            tp = entry - risk * RR_TARGET

            send(
                f"📉 {asset} SHORT ENTRY\n\n"
                f"Model: 08/16 Range Break + Retest\n"
                f"HTF Bias: {bias}\n"
                f"Entry: {entry:.2f}\n"
                f"SL: {sl:.2f}\n"
                f"TP: {tp:.2f}\n"
                f"RR: 1:{RR_TARGET}"
            )

            s["traded"] = True
            s["range_set"] = False

# =========================
# LOOP
# =========================
def run():
    send("🔥 BOT LIVE - HIGH PROBABILITY 08:00 / 16:00 MODEL 🔥")

    while True:
        try:
            for asset in ASSETS:
                update_range(asset)
                check_trade(asset)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"⚠️ ERROR:\n{e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
