import pandas as pd
import yfinance as yf
import requests
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# =========================
# CONFIG
# =========================
TIMEZONE = "Europe/London"

SYMBOLS = {
    "BTC": "BTC-USD",
    "GOLD": "GC=F"
}

RR = 2.0
CHECK_INTERVAL = 60
MODE = "BACKTEST"  # CHANGE TO LIVE

# TELEGRAM
TOKEN = "YOUR_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"

# =========================
# TELEGRAM
# =========================
def send(msg):
    print(msg)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg}
        )
    except:
        pass

# =========================
# DATA
# =========================
def get_data(symbol):
    df = yf.download(symbol, period="30d", interval="15m", progress=False)

    df = df.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close"
    })

    df = df.dropna()
    return df

# =========================
# BACKTEST
# =========================
def backtest_asset(name, symbol):
    df = get_data(symbol)

    trades = []

    range_high = None
    range_low = None
    break_side = None

    for i in range(2, len(df)):
        candle = df.iloc[i]
        t = df.index[i].tz_localize(None)

        hour = t.hour
        minute = t.minute

        # 8:30 + 16:30 candles
        if (hour == 8 and minute == 30) or (hour == 16 and minute == 30):
            range_high = candle["high"]
            range_low = candle["low"]
            break_side = None

        if range_high is None:
            continue

        close = candle["close"]
        open_ = candle["open"]

        # BREAK
        if break_side is None:
            if close > range_high:
                break_side = "LONG"
                continue
            elif close < range_low:
                break_side = "SHORT"
                continue

        # RETEST ENTRY
        if break_side == "LONG":
            if candle["low"] <= range_high and close > open_:
                entry = close
                sl = range_low
                tp = entry + (entry - sl) * RR

                trades.append(sim_trade(df, i, entry, sl, tp))
                break_side = None

        if break_side == "SHORT":
            if candle["high"] >= range_low and close < open_:
                entry = close
                sl = range_high
                tp = entry - (sl - entry) * RR

                trades.append(sim_trade(df, i, entry, sl, tp))
                break_side = None

    analyze(name, trades)

def sim_trade(df, start, entry, sl, tp):
    for j in range(start+1, len(df)):
        candle = df.iloc[j]

        if candle["low"] <= sl:
            return -1

        if candle["high"] >= tp:
            return 2

    return 0

def analyze(name, trades):
    wins = trades.count(2)
    losses = trades.count(-1)
    total = len(trades)

    if total == 0:
        print(f"{name}: NO TRADES")
        return

    winrate = (wins / total) * 100
    profit = sum(trades)

    print(f"\n===== {name} RESULTS =====")
    print(f"Trades: {total}")
    print(f"Winrate: {winrate:.2f}%")
    print(f"Profit (R): {profit}")

# =========================
# LIVE
# =========================
STATE = {
    name: {
        "range_high": None,
        "range_low": None,
        "break_side": None,
        "in_trade": False
    } for name in SYMBOLS
}

def live():
    send("BOT LIVE BTC + GOLD")

    while True:
        for name, symbol in SYMBOLS.items():

            df = get_data(symbol)
            candle = df.iloc[-1]

            t = df.index[-1].tz_localize(None)
            hour = t.hour
            minute = t.minute

            s = STATE[name]

            # SET RANGE
            if (hour == 8 and minute == 30) or (hour == 16 and minute == 30):
                s["range_high"] = candle["high"]
                s["range_low"] = candle["low"]
                s["break_side"] = None
                s["in_trade"] = False

                send(f"{name} RANGE SET\nHigh: {s['range_high']}\nLow: {s['range_low']}")

            if s["range_high"] is None:
                continue

            close = candle["close"]
            open_ = candle["open"]

            # BREAK
            if s["break_side"] is None:
                if close > s["range_high"]:
                    s["break_side"] = "LONG"
                    send(f"{name} BREAK UP")

                elif close < s["range_low"]:
                    s["break_side"] = "SHORT"
                    send(f"{name} BREAK DOWN")

            # ENTRY
            if not s["in_trade"]:
                if s["break_side"] == "LONG":
                    if candle["low"] <= s["range_high"] and close > open_:

                        entry = close
                        sl = s["range_low"]
                        tp = entry + (entry - sl) * RR

                        send(f"{name} LONG\nEntry: {entry}\nSL: {sl}\nTP: {tp}")
                        s["in_trade"] = True

                if s["break_side"] == "SHORT":
                    if candle["high"] >= s["range_low"] and close < open_:

                        entry = close
                        sl = s["range_high"]
                        tp = entry - (sl - entry) * RR

                        send(f"{name} SHORT\nEntry: {entry}\nSL: {sl}\nTP: {tp}")
                        s["in_trade"] = True

        time.sleep(CHECK_INTERVAL)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    if MODE == "BACKTEST":
        for name, symbol in SYMBOLS.items():
            backtest_asset(name, symbol)
    else:
        live()
