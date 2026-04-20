import os
import time
import requests
import pandas as pd
import numpy as np

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

IN_TRADE = False
TRADE_SIDE = None  # LONG / SHORT
ENTRY_PRICE = 0.0
STOP_LOSS = 0.0
TAKE_PROFIT = 0.0
LAST_ALERT_TS = 0

# =========================
# TELEGRAM
# =========================
def send(msg: str):
    try:
        if not TELEGRAM_TOKEN or not CHAT_ID:
            print(msg)
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# =========================
# DATA
# =========================
def get_klines(interval: str, limit: int = 120) -> pd.DataFrame:
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
    data = requests.get(url, timeout=10).json()

    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    return df

# =========================
# INDICATORS
# =========================
def ema(df: pd.DataFrame, span: int) -> pd.Series:
    return df["close"].ewm(span=span, adjust=False).mean()

def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema9"] = ema(df, 9)
    df["ema21"] = ema(df, 21)
    df["ema50"] = ema(df, 50)
    df["rsi"] = rsi(df, 14)
    df["atr"] = atr(df, 14)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["hh10"] = df["high"].rolling(10).max().shift(1)
    df["ll10"] = df["low"].rolling(10).min().shift(1)
    df["hh20"] = df["high"].rolling(20).max().shift(1)
    df["ll20"] = df["low"].rolling(20).min().shift(1)
    df.dropna(inplace=True)
    return df

# =========================
# SMART SCORING
# =========================
def ai_long_score(df1: pd.DataFrame, df5: pd.DataFrame):
    r1 = df1.iloc[-1]
    p1 = df1.iloc[-2]
    r5 = df5.iloc[-1]

    score = 0
    reasons = []

    # higher timeframe trend
    if r5["ema9"] > r5["ema21"] > r5["ema50"]:
        score += 25
        reasons.append("5m uptrend")
    elif r5["ema9"] > r5["ema21"]:
        score += 15
        reasons.append("5m bullish bias")

    # lower timeframe alignment
    if r1["ema9"] > r1["ema21"] > r1["ema50"]:
        score += 20
        reasons.append("1m strong trend")
    elif r1["ema9"] > r1["ema21"]:
        score += 10
        reasons.append("1m bullish bias")

    # momentum
    if 52 <= r1["rsi"] <= 68:
        score += 15
        reasons.append("healthy RSI")
    elif 45 <= r1["rsi"] <= 72:
        score += 8
        reasons.append("acceptable RSI")

    # relative volume
    if r1["volume"] > r1["vol_ma"] * 1.8:
        score += 20
        reasons.append("high volume")
    elif r1["volume"] > r1["vol_ma"] * 1.2:
        score += 10
        reasons.append("volume confirm")

    # price action
    if r1["close"] > p1["close"]:
        score += 10
        reasons.append("bull candle")

    # structure hold
    if r1["close"] > r1["ema9"]:
        score += 10
        reasons.append("holding fast EMA")

    return score, reasons

def ai_short_score(df1: pd.DataFrame, df5: pd.DataFrame):
    r1 = df1.iloc[-1]
    p1 = df1.iloc[-2]
    r5 = df5.iloc[-1]

    score = 0
    reasons = []

    if r5["ema9"] < r5["ema21"] < r5["ema50"]:
        score += 25
        reasons.append("5m downtrend")
    elif r5["ema9"] < r5["ema21"]:
        score += 15
        reasons.append("5m bearish bias")

    if r1["ema9"] < r1["ema21"] < r1["ema50"]:
        score += 20
        reasons.append("1m strong downtrend")
    elif r1["ema9"] < r1["ema21"]:
        score += 10
        reasons.append("1m bearish bias")

    if 32 <= r1["rsi"] <= 48:
        score += 15
        reasons.append("healthy short RSI")
    elif 28 <= r1["rsi"] <= 55:
        score += 8
        reasons.append("acceptable short RSI")

    if r1["volume"] > r1["vol_ma"] * 1.8:
        score += 20
        reasons.append("high volume")
    elif r1["volume"] > r1["vol_ma"] * 1.2:
        score += 10
        reasons.append("volume confirm")

    if r1["close"] < p1["close"]:
        score += 10
        reasons.append("bear candle")

    if r1["close"] < r1["ema9"]:
        score += 10
        reasons.append("below fast EMA")

    return score, reasons

# =========================
# BREAKOUT / SNIPER LOGIC
# =========================
def breakout_long(df1: pd.DataFrame):
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    clean_break = r["close"] > r["hh10"] * 1.0015
    strong_close = r["close"] > p["close"]
    not_too_extended = (r["close"] - r["ema9"]) / max(r["ema9"], 1) < 0.008

    return clean_break and strong_close and not_too_extended

def breakout_short(df1: pd.DataFrame):
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    clean_break = r["close"] < r["ll10"] * 0.9985
    strong_close = r["close"] < p["close"]
    not_too_extended = (r["ema9"] - r["close"]) / max(r["ema9"], 1) < 0.008

    return clean_break and strong_close and not_too_extended

def sniper_long(df1: pd.DataFrame):
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    ema_reclaim = p["close"] < p["ema9"] and r["close"] > r["ema9"]
    rsi_reclaim = p["rsi"] < 45 and r["rsi"] > 50
    higher_low = r["low"] > p2["low"]

    return ema_reclaim and rsi_reclaim and higher_low

def sniper_short(df1: pd.DataFrame):
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    ema_reject = p["close"] > p["ema9"] and r["close"] < r["ema9"]
    rsi_reject = p["rsi"] > 55 and r["rsi"] < 50
    lower_high = r["high"] < p2["high"]

    return ema_reject and rsi_reject and lower_high

# =========================
# STATUS / HEARTBEAT
# =========================
def market_trend(df1: pd.DataFrame, df5: pd.DataFrame):
    r1 = df1.iloc[-1]
    r5 = df5.iloc[-1]

    if r5["ema9"] > r5["ema21"] and r1["ema9"] > r1["ema21"]:
        return "BULLISH"
    if r5["ema9"] < r5["ema21"] and r1["ema9"] < r1["ema21"]:
        return "BEARISH"
    return "CHOPPY"

def send_heartbeat(df1: pd.DataFrame, df5: pd.DataFrame):
    long_score, _ = ai_long_score(df1, df5)
    short_score, _ = ai_short_score(df1, df5)
    r1 = df1.iloc[-1]

    send(
        f"💓 BTC HEARTBEAT\n\n"
        f"Price: ${float(r1['close']):.2f}\n"
        f"RSI: {float(r1['rsi']):.1f}\n"
        f"Trend: {market_trend(df1, df5)}\n"
        f"Long score: {long_score}\n"
        f"Short score: {short_score}\n"
        f"In trade: {'YES' if IN_TRADE else 'NO'}"
    )

# =========================
# MAIN SIGNAL ENGINE
# =========================
def get_signal():
    df1 = add_indicators(get_klines("1m"))
    df5 = add_indicators(get_klines("5m"))

    price = float(df1.iloc[-1]["close"])
    atr_now = float(df1.iloc[-1]["atr"])

    long_score, long_reasons = ai_long_score(df1, df5)
    short_score, short_reasons = ai_short_score(df1, df5)

    long_breakout = breakout_long(df1)
    short_breakout = breakout_short(df1)
    long_sniper = sniper_long(df1)
    short_sniper = sniper_short(df1)

    return {
        "price": price,
        "atr": atr_now,
        "df1": df1,
        "df5": df5,
        "long_score": long_score,
        "short_score": short_score,
        "long_reasons": long_reasons,
        "short_reasons": short_reasons,
        "long_breakout": long_breakout,
        "short_breakout": short_breakout,
        "long_sniper": long_sniper,
        "short_sniper": short_sniper,
    }

# =========================
# BOT LOOP
# =========================
def run():
    global IN_TRADE, TRADE_SIDE, ENTRY_PRICE, STOP_LOSS, TAKE_PROFIT, LAST_ALERT_TS

    send("🔥 BTC ELITE SNIPER V3 LIVE 🔥")

    last_heartbeat = 0

    while True:
        try:
            sig = get_signal()
            now = time.time()

            if now - last_heartbeat > 1800:
                send_heartbeat(sig["df1"], sig["df5"])
                last_heartbeat = now

            if not IN_TRADE:
                # long entries
                if (
                    sig["long_score"] >= 60 and
                    (sig["long_breakout"] or sig["long_sniper"])
                ):
                    ENTRY_PRICE = sig["price"]
                    STOP_LOSS = ENTRY_PRICE - (sig["atr"] * 1.5)
                    TAKE_PROFIT = ENTRY_PRICE + (sig["atr"] * 3.0)
                    TRADE_SIDE = "LONG"
                    IN_TRADE = True

                    trigger = "BREAKOUT" if sig["long_breakout"] else "SNIPER"

                    send(
                        f"🚀 BTC LONG ENTRY\n\n"
                        f"Trigger: {trigger}\n"
                        f"Price: ${ENTRY_PRICE:.2f}\n"
                        f"Score: {sig['long_score']}\n"
                        f"Reasons: {', '.join(sig['long_reasons'][:4])}\n\n"
                        f"SL: ${STOP_LOSS:.2f}\n"
                        f"TP: ${TAKE_PROFIT:.2f}"
                    )

                # short entries
                elif (
                    sig["short_score"] >= 60 and
                    (sig["short_breakout"] or sig["short_sniper"])
                ):
                    ENTRY_PRICE = sig["price"]
                    STOP_LOSS = ENTRY_PRICE + (sig["atr"] * 1.5)
                    TAKE_PROFIT = ENTRY_PRICE - (sig["atr"] * 3.0)
                    TRADE_SIDE = "SHORT"
                    IN_TRADE = True

                    trigger = "BREAKDOWN" if sig["short_breakout"] else "SNIPER"

                    send(
                        f"📉 BTC SHORT ENTRY\n\n"
                        f"Trigger: {trigger}\n"
                        f"Price: ${ENTRY_PRICE:.2f}\n"
                        f"Score: {sig['short_score']}\n"
                        f"Reasons: {', '.join(sig['short_reasons'][:4])}\n\n"
                        f"SL: ${STOP_LOSS:.2f}\n"
                        f"TP: ${TAKE_PROFIT:.2f}"
                    )

            else:
                price = sig["price"]

                # break-even
                if TRADE_SIDE == "LONG" and price >= ENTRY_PRICE + sig["atr"]:
                    if STOP_LOSS < ENTRY_PRICE:
                        STOP_LOSS = ENTRY_PRICE
                        send(f"⚡ BTC LONG BREAK-EVEN\nNew SL: ${STOP_LOSS:.2f}")

                if TRADE_SIDE == "SHORT" and price <= ENTRY_PRICE - sig["atr"]:
                    if STOP_LOSS > ENTRY_PRICE:
                        STOP_LOSS = ENTRY_PRICE
                        send(f"⚡ BTC SHORT BREAK-EVEN\nNew SL: ${STOP_LOSS:.2f}")

                # trailing
                if TRADE_SIDE == "LONG" and price > ENTRY_PRICE + (sig["atr"] * 1.5):
                    new_sl = price - (sig["atr"] * 1.5)
                    if new_sl > STOP_LOSS:
                        STOP_LOSS = new_sl
                        send(f"📈 BTC LONG TRAILING STOP\nNew SL: ${STOP_LOSS:.2f}")

                if TRADE_SIDE == "SHORT" and price < ENTRY_PRICE - (sig["atr"] * 1.5):
                    new_sl = price + (sig["atr"] * 1.5)
                    if new_sl < STOP_LOSS:
                        STOP_LOSS = new_sl
                        send(f"📉 BTC SHORT TRAILING STOP\nNew SL: ${STOP_LOSS:.2f}")

                # exits
                if TRADE_SIDE == "LONG":
                    if price <= STOP_LOSS:
                        send(f"❌ BTC LONG STOP HIT\nExit: ${price:.2f}")
                        IN_TRADE = False
                        TRADE_SIDE = None
                    elif price >= TAKE_PROFIT:
                        send(f"🎯 BTC LONG TARGET HIT\nExit: ${price:.2f}")
                        IN_TRADE = False
                        TRADE_SIDE = None

                if TRADE_SIDE == "SHORT":
                    if price >= STOP_LOSS:
                        send(f"❌ BTC SHORT STOP HIT\nExit: ${price:.2f}")
                        IN_TRADE = False
                        TRADE_SIDE = None
                    elif price <= TAKE_PROFIT:
                        send(f"🎯 BTC SHORT TARGET HIT\nExit: ${price:.2f}")
                        IN_TRADE = False
                        TRADE_SIDE = None

            time.sleep(60)

        except Exception as e:
            send(f"BTC BOT ERROR: {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
