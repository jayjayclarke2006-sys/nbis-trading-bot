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
SYMBOL = "BTC-USD"

RR = 2.0
CHECK_INTERVAL = 60

MODE = "BACKTEST"  # CHANGE TO "LIVE" WHEN READY

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
# TIME
# =========================
def now():
    return datetime.now(ZoneInfo(TIMEZONE))

# =========================
# DATA
# =========================
def get_data():
    df = yf.download(SYMBOL, period="30d", interval="15m", progress=False)
    df = df.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close"
    })
    df = df.dropna()
    return df

# =========================
# BACKTEST ENGINE
# =========================
def backtest():
    df = get_data()

    trades = []

    range_high = None
    range_low = None
    break_side = None

    for i in range(2, len(df)):
        candle = df.iloc[i]
        prev = df.iloc[i-1]

        t = df.index[i]
        hour = t.tz_localize(None).hour
        minute = t.tz_localize(None).minute

        # SET RANGE (8:30 + 16:30)
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

        # RETEST + REJECTION
        if break_side == "LONG":
            if candle["low"] <= range_high and close > open_:

                entry = close
                sl = range_low
                tp = entry + (entry - sl) * RR

                result = simulate_trade(df, i, entry, sl, tp)

                trades.append(result)
                break_side = None

        if break_side == "SHORT":
            if candle["high"] >= range_low and close < open_:

                entry = close
                sl = range_high
                tp = entry - (sl - entry) * RR

                result = simulate_trade(df, i, entry, sl, tp)

                trades.append(result)
                break_side = None

    analyze(trades)

# =========================
# TRADE SIMULATION
# =========================
def simulate_trade(df, start_index, entry, sl, tp):
    for j in range(start_index+1, len(df)):
        candle = df.iloc[j]

        if candle["low"] <= sl:
            return -1  # loss

        if candle["high"] >= tp:
            return 2   # win (RR=2)

    return 0

# =========================
# ANALYSIS
# =========================
def analyze(trades):
    wins = trades.count(2)
    losses = trades.count(-1)

    total = len(trades)

    if total == 0:
        print("NO TRADES")
        return

    winrate = (wins / total) * 100
    profit = sum(trades)

    print("\n===== BACKTEST RESULTS =====")
    print(f"Trades: {total}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(f"Winrate: {winrate:.2f}%")
    print(f"Profit (R): {profit}")

# =========================
# LIVE MODE
# =========================
STATE = {
    "range_high": None,
    "range_low": None,
    "break_side": None,
    "in_trade": False
}

def live():
    send("BOT LIVE")

    while True:
        df = get_data()

        candle = df.iloc[-1]
        t = df.index[-1].tz_localize(None)

        hour = t.hour
        minute = t.minute

        # SET RANGE
        if (hour == 8 and minute == 30) or (hour == 16 and minute == 30):
            STATE["range_high"] = candle["high"]
            STATE["range_low"] = candle["low"]
            STATE["break_side"] = None

            send(f"Range set\nHigh: {STATE['range_high']}\nLow: {STATE['range_low']}")

        if STATE["range_high"] is None:
            time.sleep(CHECK_INTERVAL)
            continue

        close = candle["close"]
        open_ = candle["open"]

        # BREAK
        if STATE["break_side"] is None:
            if close > STATE["range_high"]:
                STATE["break_side"] = "LONG"
                send("BREAK UP")

            elif close < STATE["range_low"]:
                STATE["break_side"] = "SHORT"
                send("BREAK DOWN")

        # ENTRY
        if not STATE["in_trade"]:
            if STATE["break_side"] == "LONG":
                if candle["low"] <= STATE["range_high"] and close > open_:

                    entry = close
                    sl = STATE["range_low"]
                    tp = entry + (entry - sl) * RR

                    send(f"LONG\nEntry: {entry}\nSL: {sl}\nTP: {tp}")
                    STATE["in_trade"] = True

            if STATE["break_side"] == "SHORT":
                if candle["high"] >= STATE["range_low"] and close < open_:

                    entry = close
                    sl = STATE["range_high"]
                    tp = entry - (sl - entry) * RR

                    send(f"SHORT\nEntry: {entry}\nSL: {sl}\nTP: {tp}")
                    STATE["in_trade"] = True

        time.sleep(CHECK_INTERVAL)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    if MODE == "BACKTEST":
        backtest()
    else:
        live()
