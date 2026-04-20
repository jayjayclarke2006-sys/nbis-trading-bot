import os
import time
from datetime import datetime

import requests
import yfinance as yf
import pandas as pd

# =========================
# TELEGRAM
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(msg: str) -> None:
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print(msg)
            return

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print("Telegram error:", e)

# =========================
# CONFIG
# =========================
SYMBOL = "BTC-USD"
LOW_TF = "5m"
HIGH_TF = "15m"
PERIOD = "5d"

CHECK_INTERVAL = 300
HEARTBEAT_SECONDS = 1800

EMA_FAST = 20
EMA_SLOW = 50
EMA_TREND = 100
RSI_PERIOD = 14
ATR_PERIOD = 14
VOL_LOOKBACK = 20
STRUCT_LOOKBACK = 12

ALERT_SCORE = 60
RVOL_MIN = 1.2

BREAKOUT_BUFFER = 0.002
BREAKDOWN_BUFFER = 0.002

ATR_SL = 1.5
ATR_TP = 3.0
TRAIL_MULT = 2.0
BREAK_EVEN_R = 1.0

ENABLE_SHORTS = True

# =========================
# STATE
# =========================
in_trade = False
trade_side = None   # "LONG" or "SHORT"
entry = 0.0
stop = 0.0
tp = 0.0
highest = 0.0
lowest = 0.0
break_even_active = False
last_heartbeat_ts = 0.0

# =========================
# INDICATORS
# =========================
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# =========================
# DATA
# =========================
def get_df(interval: str):
    try:
        df = yf.download(SYMBOL, period=PERIOD, interval=interval, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df["ema_fast"] = ema(df["Close"], EMA_FAST)
        df["ema_slow"] = ema(df["Close"], EMA_SLOW)
        df["ema_trend"] = ema(df["Close"], EMA_TREND)
        df["rsi"] = rsi(df["Close"], RSI_PERIOD)
        df["atr"] = atr(df, ATR_PERIOD)
        df["avg_vol"] = df["Volume"].rolling(VOL_LOOKBACK).mean()
        df["recent_high"] = df["High"].rolling(STRUCT_LOOKBACK).max().shift(1)
        df["recent_low"] = df["Low"].rolling(STRUCT_LOOKBACK).min().shift(1)

        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"Data error ({interval}):", e)
        return None

# =========================
# SCORING
# =========================
def long_score(df5: pd.DataFrame, df15: pd.DataFrame) -> int:
    row5 = df5.iloc[-1]
    prev5 = df5.iloc[-2]
    row15 = df15.iloc[-1]

    score = 0

    if row15["ema_fast"] > row15["ema_slow"]:
        score += 25

    if row5["ema_fast"] > row5["ema_slow"]:
        score += 20

    if 50 <= row5["rsi"] <= 75:
        score += 15

    rvol = float(row5["Volume"]) / max(float(row5["avg_vol"]), 1.0)
    if rvol >= 1.8:
        score += 20
    elif rvol >= RVOL_MIN:
        score += 15

    if row5["Close"] > row5["recent_high"] * (1 + BREAKOUT_BUFFER):
        score += 15

    if row5["Close"] > prev5["Close"]:
        score += 10

    return int(score)

def short_score(df5: pd.DataFrame, df15: pd.DataFrame) -> int:
    row5 = df5.iloc[-1]
    prev5 = df5.iloc[-2]
    row15 = df15.iloc[-1]

    score = 0

    if row15["ema_fast"] < row15["ema_slow"]:
        score += 25

    if row5["ema_fast"] < row5["ema_slow"]:
        score += 20

    if 25 <= row5["rsi"] <= 50:
        score += 15

    rvol = float(row5["Volume"]) / max(float(row5["avg_vol"]), 1.0)
    if rvol >= 1.8:
        score += 20
    elif rvol >= RVOL_MIN:
        score += 15

    if row5["Close"] < row5["recent_low"] * (1 - BREAKDOWN_BUFFER):
        score += 15

    if row5["Close"] < prev5["Close"]:
        score += 10

    return int(score)

# =========================
# STATUS / HEARTBEAT
# =========================
def market_status(df5: pd.DataFrame, df15: pd.DataFrame) -> str:
    row5 = df5.iloc[-1]
    row15 = df15.iloc[-1]

    if row15["ema_fast"] > row15["ema_slow"] and row5["ema_fast"] > row5["ema_slow"]:
        return "BULLISH"
    if row15["ema_fast"] < row15["ema_slow"] and row5["ema_fast"] < row5["ema_slow"]:
        return "BEARISH"
    return "CHOPPY"

def maybe_send_heartbeat(df5: pd.DataFrame, df15: pd.DataFrame):
    global last_heartbeat_ts

    now_ts = time.time()
    if now_ts - last_heartbeat_ts < HEARTBEAT_SECONDS:
        return

    row5 = df5.iloc[-1]
    status = market_status(df5, df15)
    l_score = long_score(df5, df15)
    s_score = short_score(df5, df15)

    send_telegram(
        f"💓 BTC HEARTBEAT\n\n"
        f"Price: ${float(row5['Close']):.2f}\n"
        f"RSI: {float(row5['rsi']):.1f}\n"
        f"Trend: {status}\n"
        f"Long score: {l_score}\n"
        f"Short score: {s_score}\n"
        f"In trade: {'YES' if in_trade else 'NO'}"
    )
    last_heartbeat_ts = now_ts

# =========================
# ENTRY SIGNALS
# =========================
def long_signal(df5: pd.DataFrame, df15: pd.DataFrame) -> bool:
    row5 = df5.iloc[-1]
    prev5 = df5.iloc[-2]
    row15 = df15.iloc[-1]

    trend_ok = row15["ema_fast"] > row15["ema_slow"] and row5["ema_fast"] > row5["ema_slow"]
    breakout_ok = row5["Close"] > row5["recent_high"] * (1 + BREAKOUT_BUFFER)
    volume_ok = row5["Volume"] > row5["avg_vol"] * RVOL_MIN
    candle_ok = row5["Close"] > prev5["Close"]
    rsi_ok = 50 <= row5["rsi"] <= 75

    return bool(trend_ok and breakout_ok and volume_ok and candle_ok and rsi_ok)

def short_signal(df5: pd.DataFrame, df15: pd.DataFrame) -> bool:
    row5 = df5.iloc[-1]
    prev5 = df5.iloc[-2]
    row15 = df15.iloc[-1]

    trend_ok = row15["ema_fast"] < row15["ema_slow"] and row5["ema_fast"] < row5["ema_slow"]
    breakdown_ok = row5["Close"] < row5["recent_low"] * (1 - BREAKDOWN_BUFFER)
    volume_ok = row5["Volume"] > row5["avg_vol"] * RVOL_MIN
    candle_ok = row5["Close"] < prev5["Close"]
    rsi_ok = 25 <= row5["rsi"] <= 50

    return bool(trend_ok and breakdown_ok and volume_ok and candle_ok and rsi_ok)

# =========================
# TRADE MANAGEMENT
# =========================
def open_long(df5: pd.DataFrame, df15: pd.DataFrame):
    global in_trade, trade_side, entry, stop, tp, highest, lowest, break_even_active

    row = df5.iloc[-1]
    entry = float(row["Close"])
    atr_now = float(row["atr"])
    score = long_score(df5, df15)

    stop = entry - (atr_now * ATR_SL)
    tp = entry + (atr_now * ATR_TP)
    highest = entry
    lowest = entry
    break_even_active = False
    trade_side = "LONG"
    in_trade = True

    send_telegram(
        f"🚀 BTC ELITE LONG ENTRY\n\n"
        f"Price: ${entry:.2f}\n"
        f"Score: {score}\n"
        f"TP: ${tp:.2f}\n"
        f"SL: ${stop:.2f}"
    )

def open_short(df5: pd.DataFrame, df15: pd.DataFrame):
    global in_trade, trade_side, entry, stop, tp, highest, lowest, break_even_active

    row = df5.iloc[-1]
    entry = float(row["Close"])
    atr_now = float(row["atr"])
    score = short_score(df5, df15)

    stop = entry + (atr_now * ATR_SL)
    tp = entry - (atr_now * ATR_TP)
    highest = entry
    lowest = entry
    break_even_active = False
    trade_side = "SHORT"
    in_trade = True

    send_telegram(
        f"📉 BTC ELITE SHORT ENTRY\n\n"
        f"Price: ${entry:.2f}\n"
        f"Score: {score}\n"
        f"TP: ${tp:.2f}\n"
        f"SL: ${stop:.2f}"
    )

def manage_trade(df5: pd.DataFrame):
    global in_trade, trade_side, entry, stop, tp, highest, lowest, break_even_active

    if not in_trade:
        return

    row = df5.iloc[-1]
    prev = df5.iloc[-2]
    price = float(row["Close"])
    atr_now = float(row["atr"])
    rsi_now = float(row["rsi"])

    if trade_side == "LONG":
        highest = max(highest, price)
        initial_risk = entry - (entry - atr_now * ATR_SL)

        current_r = (price - entry) / max((entry - stop), 0.0001) if stop < entry else (price - entry) / max(atr_now * ATR_SL, 0.0001)

        if (not break_even_active) and price >= entry + (atr_now * BREAK_EVEN_R):
            stop = entry
            break_even_active = True
            send_telegram(f"⚡ BTC LONG MOVE TO BREAK-EVEN\nNew SL: ${stop:.2f}")

        new_trail = highest - (atr_now * TRAIL_MULT)
        if break_even_active and new_trail > stop:
            stop = new_trail
            send_telegram(f"📈 BTC LONG TRAILING STOP UPDATED\nNew SL: ${stop:.2f}")

        if rsi_now > 70 and price < float(prev["Close"]):
            send_telegram("⚠️ BTC LONG MOMENTUM WEAKENING")

        if row["ema_fast"] < row["ema_slow"]:
            send_telegram("⚠️ BTC LONG TREND BREAK WARNING")

        if price <= stop:
            pnl_pct = ((price - entry) / entry) * 100
            send_telegram(f"❌ BTC LONG EXIT\nPrice: ${price:.2f}\nP/L: {pnl_pct:.2f}%")
            in_trade = False
            trade_side = None
            return

        if price >= tp:
            pnl_pct = ((price - entry) / entry) * 100
            send_telegram(f"🎯 BTC LONG TARGET HIT\nPrice: ${price:.2f}\nP/L: {pnl_pct:.2f}%")
            in_trade = False
            trade_side = None
            return

    if trade_side == "SHORT":
        lowest = min(lowest, price)

        if (not break_even_active) and price <= entry - (atr_now * BREAK_EVEN_R):
            stop = entry
            break_even_active = True
            send_telegram(f"⚡ BTC SHORT MOVE TO BREAK-EVEN\nNew SL: ${stop:.2f}")

        new_trail = lowest + (atr_now * TRAIL_MULT)
        if break_even_active and new_trail < stop:
            stop = new_trail
            send_telegram(f"📉 BTC SHORT TRAILING STOP UPDATED\nNew SL: ${stop:.2f}")

        if rsi_now < 30 and price > float(prev["Close"]):
            send_telegram("⚠️ BTC SHORT MOMENTUM WEAKENING")

        if row["ema_fast"] > row["ema_slow"]:
            send_telegram("⚠️ BTC SHORT TREND BREAK WARNING")

        if price >= stop:
            pnl_pct = ((entry - price) / entry) * 100
            send_telegram(f"❌ BTC SHORT EXIT\nPrice: ${price:.2f}\nP/L: {pnl_pct:.2f}%")
            in_trade = False
            trade_side = None
            return

        if price <= tp:
            pnl_pct = ((entry - price) / entry) * 100
            send_telegram(f"🎯 BTC SHORT TARGET HIT\nPrice: ${price:.2f}\nP/L: {pnl_pct:.2f}%")
            in_trade = False
            trade_side = None
            return

# =========================
# MAIN
# =========================
def check():
    df5 = get_df(LOW_TF)
    df15 = get_df(HIGH_TF)

    if df5 is None or df15 is None:
        return

    maybe_send_heartbeat(df5, df15)

    if in_trade:
        manage_trade(df5)
        return

    l_score = long_score(df5, df15)
    s_score = short_score(df5, df15)

    if long_signal(df5, df15) and l_score >= ALERT_SCORE:
        open_long(df5, df15)
        return

    if ENABLE_SHORTS and short_signal(df5, df15) and s_score >= ALERT_SCORE:
        open_short(df5, df15)
        return

def run():
    send_telegram("🔥 BTC ELITE SYSTEM LIVE 🔥")

    while True:
        try:
            check()
        except Exception as e:
            print("BTC bot error:", e)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run()
