import os
import time
import threading
from datetime import datetime

import requests
import yfinance as yf
import pandas as pd
from flask import Flask

# =========================
# TELEGRAM
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# BTC CONFIG
# =========================
SYMBOL = "BTC-USD"
CHECK_INTERVAL = 300  # 5 minutes

EMA_FAST = 20
EMA_SLOW = 50
EMA_TREND = 100
RSI_PERIOD = 14
ATR_PERIOD = 14
VOLUME_LOOKBACK = 20
BREAKOUT_LOOKBACK = 10

LONG_RSI_MIN = 55
LONG_RSI_MAX = 70
SHORT_RSI_MIN = 30
SHORT_RSI_MAX = 45

RVOL_MIN = 1.3
BREAKOUT_BUFFER = 0.0025
BREAKDOWN_BUFFER = 0.0025

MIN_LONG_SCORE = 80
MIN_SHORT_SCORE = 80

INITIAL_SL_ATR = 1.5
TP1_R = 1.5
FINAL_TP_R = 3.0
TRAILING_ATR = 2.0

SHORT_INITIAL_SL_ATR = 1.5
SHORT_TP1_R = 1.5
SHORT_FINAL_TP_R = 3.0
SHORT_TRAILING_ATR = 2.0

BREAK_EVEN_TRIGGER_R = 1.2
COOLDOWN_AFTER_EXIT = 1800  # 30 mins
MIN_ALERT_GAP = 900  # 15 mins between fresh entries of same side

# =========================
# APP
# =========================
app = Flask(__name__)

# =========================
# STATE
# =========================
active_trade = None
last_long_alert_ts = 0
last_short_alert_ts = 0
last_exit_ts = 0

# active_trade shape:
# {
#   "side": "LONG" or "SHORT",
#   "entry": float,
#   "sl": float,
#   "initial_sl": float,
#   "tp1": float,
#   "final_tp": float,
#   "trail_active": bool,
#   "be_active": bool,
#   "partial_sent": bool,
#   "highest": float,
#   "lowest": float,
#   "score": int,
#   "opened_at": str,
# }

# =========================
# HELPERS
# =========================
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


def get_data() -> pd.DataFrame | None:
    try:
        df = yf.download(SYMBOL, period="5d", interval="5m", progress=False)

        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df["ema_fast"] = ema(df["Close"], EMA_FAST)
        df["ema_slow"] = ema(df["Close"], EMA_SLOW)
        df["ema_trend"] = ema(df["Close"], EMA_TREND)
        df["rsi"] = rsi(df["Close"], RSI_PERIOD)
        df["atr"] = atr(df, ATR_PERIOD)
        df["avg_volume"] = df["Volume"].rolling(VOLUME_LOOKBACK).mean()
        df["recent_high"] = df["High"].rolling(BREAKOUT_LOOKBACK).max().shift(1)
        df["recent_low"] = df["Low"].rolling(BREAKOUT_LOOKBACK).min().shift(1)

        df.dropna(inplace=True)
        return df
    except Exception as e:
        print("Data error:", e)
        return None


def long_score(df: pd.DataFrame) -> int:
    row = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0

    if row["ema_fast"] > row["ema_slow"] > row["ema_trend"]:
        score += 30
    elif row["ema_fast"] > row["ema_slow"]:
        score += 20

    if LONG_RSI_MIN <= row["rsi"] <= LONG_RSI_MAX:
        score += 20

    rvol = row["Volume"] / max(row["avg_volume"], 1)
    if rvol >= 2.0:
        score += 25
    elif rvol >= RVOL_MIN:
        score += 15

    if row["Close"] > row["recent_high"] * (1 + BREAKOUT_BUFFER):
        score += 15

    if row["Close"] > prev["Close"]:
        score += 10

    return int(score)


def short_score(df: pd.DataFrame) -> int:
    row = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0

    if row["ema_fast"] < row["ema_slow"] < row["ema_trend"]:
        score += 30
    elif row["ema_fast"] < row["ema_slow"]:
        score += 20

    if SHORT_RSI_MIN <= row["rsi"] <= SHORT_RSI_MAX:
        score += 20

    rvol = row["Volume"] / max(row["avg_volume"], 1)
    if rvol >= 2.0:
        score += 25
    elif rvol >= RVOL_MIN:
        score += 15

    if row["Close"] < row["recent_low"] * (1 - BREAKDOWN_BUFFER):
        score += 15

    if row["Close"] < prev["Close"]:
        score += 10

    return int(score)


def long_signal(df: pd.DataFrame) -> bool:
    row = df.iloc[-1]
    prev = df.iloc[-2]

    breakout = row["Close"] > row["recent_high"] * (1 + BREAKOUT_BUFFER)
    trend = row["ema_fast"] > row["ema_slow"] > row["ema_trend"]
    momentum = LONG_RSI_MIN <= row["rsi"] <= LONG_RSI_MAX
    volume_ok = row["Volume"] >= row["avg_volume"] * RVOL_MIN
    candle_ok = row["Close"] > prev["Close"]

    return bool(breakout and trend and momentum and volume_ok and candle_ok)


def short_signal(df: pd.DataFrame) -> bool:
    row = df.iloc[-1]
    prev = df.iloc[-2]

    breakdown = row["Close"] < row["recent_low"] * (1 - BREAKDOWN_BUFFER)
    trend = row["ema_fast"] < row["ema_slow"] < row["ema_trend"]
    momentum = SHORT_RSI_MIN <= row["rsi"] <= SHORT_RSI_MAX
    volume_ok = row["Volume"] >= row["avg_volume"] * RVOL_MIN
    candle_ok = row["Close"] < prev["Close"]

    return bool(breakdown and trend and momentum and volume_ok and candle_ok)


# =========================
# TRADE STATE MANAGEMENT
# =========================
def open_long(df: pd.DataFrame) -> None:
    global active_trade, last_long_alert_ts

    row = df.iloc[-1]
    entry = float(row["Close"])
    atr_now = float(row["atr"])
    score = long_score(df)

    risk = atr_now * INITIAL_SL_ATR
    sl = entry - risk
    tp1 = entry + (risk * TP1_R)
    final_tp = entry + (risk * FINAL_TP_R)

    active_trade = {
        "side": "LONG",
        "entry": entry,
        "sl": sl,
        "initial_sl": sl,
        "tp1": tp1,
        "final_tp": final_tp,
        "trail_active": False,
        "be_active": False,
        "partial_sent": False,
        "highest": entry,
        "lowest": entry,
        "score": score,
        "opened_at": datetime.utcnow().isoformat(),
    }

    last_long_alert_ts = time.time()

    send_telegram(
        f"🚨 ELITE BTC LONG 🚨\n\n"
        f"Price: ${entry:.2f}\n"
        f"Score: {score}\n"
        f"TP1: ${tp1:.2f}\n"
        f"Final TP: ${final_tp:.2f}\n"
        f"SL: ${sl:.2f}"
    )


def open_short(df: pd.DataFrame) -> None:
    global active_trade, last_short_alert_ts

    row = df.iloc[-1]
    entry = float(row["Close"])
    atr_now = float(row["atr"])
    score = short_score(df)

    risk = atr_now * SHORT_INITIAL_SL_ATR
    sl = entry + risk
    tp1 = entry - (risk * SHORT_TP1_R)
    final_tp = entry - (risk * SHORT_FINAL_TP_R)

    active_trade = {
        "side": "SHORT",
        "entry": entry,
        "sl": sl,
        "initial_sl": sl,
        "tp1": tp1,
        "final_tp": final_tp,
        "trail_active": False,
        "be_active": False,
        "partial_sent": False,
        "highest": entry,
        "lowest": entry,
        "score": score,
        "opened_at": datetime.utcnow().isoformat(),
    }

    last_short_alert_ts = time.time()

    send_telegram(
        f"🚨 ELITE BTC SHORT 🚨\n\n"
        f"Price: ${entry:.2f}\n"
        f"Score: {score}\n"
        f"TP1: ${tp1:.2f}\n"
        f"Final TP: ${final_tp:.2f}\n"
        f"SL: ${sl:.2f}"
    )


def manage_active_trade(df: pd.DataFrame) -> None:
    global active_trade, last_exit_ts

    if active_trade is None:
        return

    row = df.iloc[-1]
    price = float(row["Close"])
    atr_now = float(row["atr"])

    side = active_trade["side"]
    entry = active_trade["entry"]
    sl = active_trade["sl"]

    active_trade["highest"] = max(active_trade["highest"], price)
    active_trade["lowest"] = min(active_trade["lowest"], price)

    if side == "LONG":
        risk = entry - active_trade["initial_sl"]
        current_r = (price - entry) / risk if risk > 0 else 0

        # break-even
        if (not active_trade["be_active"]) and current_r >= BREAK_EVEN_TRIGGER_R:
            active_trade["sl"] = entry
            active_trade["be_active"] = True
            send_telegram(
                f"🟢 BTC LONG UPDATE\n\n"
                f"Price: ${price:.2f}\n"
                f"SL moved to break-even: ${entry:.2f}"
            )

        # partial
        if (not active_trade["partial_sent"]) and price >= active_trade["tp1"]:
            active_trade["partial_sent"] = True
            active_trade["trail_active"] = True
            send_telegram(
                f"💰 BTC LONG TP1 HIT\n\n"
                f"Price: ${price:.2f}\n"
                f"Partial profit zone reached\n"
                f"Trail now active"
            )

        # trailing
        if active_trade["trail_active"]:
            new_sl = active_trade["highest"] - (atr_now * TRAILING_ATR)
            if new_sl > active_trade["sl"]:
                active_trade["sl"] = new_sl
                send_telegram(
                    f"📈 BTC LONG TRAIL UPDATE\n\n"
                    f"Price: ${price:.2f}\n"
                    f"New trailing SL: ${new_sl:.2f}"
                )

        # stop hit
        if price <= active_trade["sl"]:
            pnl_pct = ((price - entry) / entry) * 100
            send_telegram(
                f"❌ BTC LONG EXIT\n\n"
                f"Exit: ${price:.2f}\n"
                f"P/L: {pnl_pct:.2f}%"
            )
            active_trade = None
            last_exit_ts = time.time()
            return

        # final target
        if price >= active_trade["final_tp"]:
            pnl_pct = ((price - entry) / entry) * 100
            send_telegram(
                f"🏁 BTC LONG FINAL TP HIT\n\n"
                f"Exit: ${price:.2f}\n"
                f"P/L: {pnl_pct:.2f}%"
            )
            active_trade = None
            last_exit_ts = time.time()
            return

    else:
        risk = active_trade["initial_sl"] - entry
        current_r = (entry - price) / risk if risk > 0 else 0

        # break-even
        if (not active_trade["be_active"]) and current_r >= BREAK_EVEN_TRIGGER_R:
            active_trade["sl"] = entry
            active_trade["be_active"] = True
            send_telegram(
                f"🟢 BTC SHORT UPDATE\n\n"
                f"Price: ${price:.2f}\n"
                f"SL moved to break-even: ${entry:.2f}"
            )

        # partial
        if (not active_trade["partial_sent"]) and price <= active_trade["tp1"]:
            active_trade["partial_sent"] = True
            active_trade["trail_active"] = True
            send_telegram(
                f"💰 BTC SHORT TP1 HIT\n\n"
                f"Price: ${price:.2f}\n"
                f"Partial profit zone reached\n"
                f"Trail now active"
            )

        # trailing
        if active_trade["trail_active"]:
            new_sl = active_trade["lowest"] + (atr_now * SHORT_TRAILING_ATR)
            if new_sl < active_trade["sl"]:
                active_trade["sl"] = new_sl
                send_telegram(
                    f"📉 BTC SHORT TRAIL UPDATE\n\n"
                    f"Price: ${price:.2f}\n"
                    f"New trailing SL: ${new_sl:.2f}"
                )

        # stop hit
        if price >= active_trade["sl"]:
            pnl_pct = ((entry - price) / entry) * 100
            send_telegram(
                f"❌ BTC SHORT EXIT\n\n"
                f"Exit: ${price:.2f}\n"
                f"P/L: {pnl_pct:.2f}%"
            )
            active_trade = None
            last_exit_ts = time.time()
            return

        # final target
        if price <= active_trade["final_tp"]:
            pnl_pct = ((entry - price) / entry) * 100
            send_telegram(
                f"🏁 BTC SHORT FINAL TP HIT\n\n"
                f"Exit: ${price:.2f}\n"
                f"P/L: {pnl_pct:.2f}%"
            )
            active_trade = None
            last_exit_ts = time.time()
            return


# =========================
# MAIN BOT LOGIC
# =========================
def check_signal() -> None:
    global last_long_alert_ts, last_short_alert_ts

    df = get_data()
    if df is None:
        return

    if active_trade is not None:
        manage_active_trade(df)
        return

    if time.time() - last_exit_ts < COOLDOWN_AFTER_EXIT:
        return

    long_ok = long_signal(df)
    short_ok = short_signal(df)

    l_score = long_score(df)
    s_score = short_score(df)

    if long_ok and l_score >= MIN_LONG_SCORE:
        if time.time() - last_long_alert_ts >= MIN_ALERT_GAP:
            open_long(df)
        return

    if short_ok and s_score >= MIN_SHORT_SCORE:
        if time.time() - last_short_alert_ts >= MIN_ALERT_GAP:
            open_short(df)
        return


def run_bot() -> None:
    send_telegram(f"BTC God Mode bot started 🚀\nSymbol: {SYMBOL}")

    while True:
        try:
            check_signal()
        except Exception as e:
            print("Loop error:", e)

        time.sleep(CHECK_INTERVAL)


# =========================
# WEB
# =========================
@app.route("/")
def home():
    return "BTC God Mode bot running"


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
