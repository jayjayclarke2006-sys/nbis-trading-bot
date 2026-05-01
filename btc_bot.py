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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ASSETS = ["BTCUSDT"]

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
        )
    except:
        pass

# =========================
# DATA
# =========================
def get_binance(symbol, interval="15m", limit=200):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )

        data = r.json()

        if not isinstance(data, list):
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "t","o","h","l","c","v","ct","q","n","tb","tq","ig"
        ])

        df.columns = ["time","open","high","low","close","volume","x","x2","x3","x4","x5","x6"]

        df = df[["open","high","low","close"]].astype(float)

        return df

    except:
        return pd.DataFrame()

# =========================
# TIME HELPERS
# =========================
def now():
    return datetime.now(ZoneInfo(TIMEZONE))

# =========================
# RANGE LOGIC
# =========================
def update_range(asset):
    df30 = get_binance(asset, "30m")

    if df30.empty:
        return

    t = now()
    s = STATE[asset]

    # =========================
    # 08:00 CANDLE (SET AT 08:30)
    # =========================
    if t.hour == 8 and t.minute == 30 and s["last_update"] != "08":

        candle = df30.iloc[-1]  # 08:00–08:30 candle

        s["range_high"] = candle["high"]
        s["range_low"] = candle["low"]
        s["range_set"] = True
        s["break_side"] = None
        s["retest_done"] = False
        s["last_update"] = "08"

        send(
            f"🔥 {asset} 08:00 RANGE SET\n\n"
            f"High: {candle['high']}\n"
            f"Low: {candle['low']}"
        )

    # =========================
    # 16:00 CANDLE (SET AT 16:30)
    # =========================
    if t.hour == 16 and t.minute == 30 and s["last_update"] != "16":

        candle = df30.iloc[-1]  # 16:00–16:30 candle

        s["range_high"] = candle["high"]
        s["range_low"] = candle["low"]
        s["range_set"] = True
        s["break_side"] = None
        s["retest_done"] = False
        s["last_update"] = "16"

        send(
            f"🔥 {asset} 16:00 RANGE SET\n\n"
            f"High: {candle['high']}\n"
            f"Low: {candle['low']}"
        )

# =========================
# TRADE LOGIC
# =========================
def check_trade(asset):
    s = STATE[asset]

    if not s["range_set"]:
        return

    df15 = get_binance(asset, "15m")

    if df15.empty:
        return

    r = df15.iloc[-1]

    close = r["close"]
    high = r["high"]
    low = r["low"]
    open_ = r["open"]

    # =========================
    # STEP 1: BREAK
    # =========================
    if s["break_side"] is None:

        if close > s["range_high"]:
            s["break_side"] = "LONG"

        elif close < s["range_low"]:
            s["break_side"] = "SHORT"

    # =========================
    # STEP 2: RETEST
    # =========================
    if s["break_side"] == "LONG" and not s["retest_done"]:
        if low <= s["range_high"]:
            s["retest_done"] = True

    if s["break_side"] == "SHORT" and not s["retest_done"]:
        if high >= s["range_low"]:
            s["retest_done"] = True

    # =========================
    # STEP 3: ENTRY
    # =========================
    if s["break_side"] == "LONG" and s["retest_done"]:

        if close > open_:
            entry = close
            sl = low
            tp = entry + (entry - sl) * 2

            send(
                f"🚀 LONG ENTRY\n\n"
                f"Entry: {entry}\n"
                f"SL: {sl}\n"
                f"TP: {tp}"
            )

            s["range_set"] = False

    if s["break_side"] == "SHORT" and s["retest_done"]:

        if close < open_:
            entry = close
            sl = high
            tp = entry - (sl - entry) * 2

            send(
                f"📉 SHORT ENTRY\n\n"
                f"Entry: {entry}\n"
                f"SL: {sl}\n"
                f"TP: {tp}"
            )

            s["range_set"] = False

# =========================
# MAIN LOOP
# =========================
def run():

    send("🔥 BOT LIVE - 08:00 / 16:00 MODEL 🔥")

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
