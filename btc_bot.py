import os
import time
import requests
import pandas as pd
import numpy as np
import ccxt
import yfinance as yf

from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange


# =========================
# SETTINGS
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SCAN_EVERY_SECONDS = 300

BTC_SYMBOL = "BTC/USDT"
GOLD_SYMBOL = "GC=F"

LOWER_TIMEFRAME = "15m"
HIGHER_TIMEFRAME = "1h"

BTC_LIMIT = 500
GOLD_PERIOD = "60d"

RISK_REWARD = 2.0
ATR_STOP_MULT = 1.4

MIN_ADX = 18
RSI_BULL_MIN = 52
RSI_BEAR_MAX = 48

JOURNAL_FILE = "trade_alert_journal.csv"


# =========================
# TELEGRAM
# =========================

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        },
        timeout=10
    )


# =========================
# DATA
# =========================

def fetch_btc(timeframe, limit=500):
    exchange = ccxt.binance()
    candles = exchange.fetch_ohlcv(BTC_SYMBOL, timeframe=timeframe, limit=limit)

    df = pd.DataFrame(
        candles,
        columns=["timestamp", "Open", "High", "Low", "Close", "Volume"]
    )

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def fetch_gold(interval):
    df = yf.download(
        GOLD_SYMBOL,
        period=GOLD_PERIOD,
        interval=interval,
        auto_adjust=True,
        progress=False
    ).reset_index()

    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "timestamp"})
    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "timestamp"})

    df = df[["timestamp", "Open", "High", "Low", "Close", "Volume"]].dropna()
    return df


# =========================
# INDICATORS
# =========================

def add_indicators(df):
    df = df.copy()

    df["ema_20"] = EMAIndicator(df["Close"], 20).ema_indicator()
    df["ema_50"] = EMAIndicator(df["Close"], 50).ema_indicator()
    df["ema_200"] = EMAIndicator(df["Close"], 200).ema_indicator()

    df["rsi"] = RSIIndicator(df["Close"], 14).rsi()

    df["adx"] = ADXIndicator(
        df["High"],
        df["Low"],
        df["Close"],
        14
    ).adx()

    df["atr"] = AverageTrueRange(
        df["High"],
        df["Low"],
        df["Close"],
        14
    ).average_true_range()

    df["avg_volume"] = df["Volume"].rolling(30).mean()

    return df.dropna()


# =========================
# MARKET STRUCTURE
# =========================

def trend_direction(row):
    if row["Close"] > row["ema_20"] > row["ema_50"] > row["ema_200"]:
        return "BULLISH"

    if row["Close"] < row["ema_20"] < row["ema_50"] < row["ema_200"]:
        return "BEARISH"

    return "NEUTRAL"


def previous_swing_high(df, lookback=20):
    return df["High"].iloc[-lookback:-2].max()


def previous_swing_low(df, lookback=20):
    return df["Low"].iloc[-lookback:-2].min()


def bullish_liquidity_sweep(df):
    curr = df.iloc[-1]
    swing_low = previous_swing_low(df)

    return curr["Low"] < swing_low and curr["Close"] > swing_low


def bearish_liquidity_sweep(df):
    curr = df.iloc[-1]
    swing_high = previous_swing_high(df)

    return curr["High"] > swing_high and curr["Close"] < swing_high


def displacement_candle(row):
    body = abs(row["Close"] - row["Open"])
    candle_range = row["High"] - row["Low"]

    if candle_range == 0:
        return False

    strong_body = body / candle_range > 0.55
    atr_expansion = candle_range > row["atr"] * 1.1

    return strong_body and atr_expansion


def volume_confirmation(row):
    return row["Volume"] > row["avg_volume"] * 1.2


# =========================
# CANDLE PATTERNS
# =========================

def bullish_engulfing(prev, curr):
    return (
        prev["Close"] < prev["Open"]
        and curr["Close"] > curr["Open"]
        and curr["Close"] > prev["Open"]
        and curr["Open"] < prev["Close"]
    )


def bearish_engulfing(prev, curr):
    return (
        prev["Close"] > prev["Open"]
        and curr["Close"] < curr["Open"]
        and curr["Open"] > prev["Close"]
        and curr["Close"] < prev["Open"]
    )


def bullish_pin_bar(row):
    body = abs(row["Close"] - row["Open"])
    rng = row["High"] - row["Low"]

    if rng == 0:
        return False

    lower_wick = min(row["Open"], row["Close"]) - row["Low"]
    upper_wick = row["High"] - max(row["Open"], row["Close"])

    return lower_wick > body * 2.5 and upper_wick < body * 1.2 and row["Close"] > row["Open"]


def bearish_pin_bar(row):
    body = abs(row["Close"] - row["Open"])
    rng = row["High"] - row["Low"]

    if rng == 0:
        return False

    upper_wick = row["High"] - max(row["Open"], row["Close"])
    lower_wick = min(row["Open"], row["Close"]) - row["Low"]

    return upper_wick > body * 2.5 and lower_wick < body * 1.2 and row["Close"] < row["Open"]


# =========================
# SIGNAL ENGINE
# =========================

def institutional_signal(name, lower_df, higher_df):
    lower_df = add_indicators(lower_df)
    higher_df = add_indicators(higher_df)

    curr = lower_df.iloc[-1]
    prev = lower_df.iloc[-2]
    higher = higher_df.iloc[-1]

    htf_trend = trend_direction(higher)

    bullish_pattern = bullish_engulfing(prev, curr) or bullish_pin_bar(curr)
    bearish_pattern = bearish_engulfing(prev, curr) or bearish_pin_bar(curr)

    bullish_setup = (
        htf_trend == "BULLISH"
        and bullish_liquidity_sweep(lower_df)
        and displacement_candle(curr)
        and bullish_pattern
        and curr["rsi"] >= RSI_BULL_MIN
        and curr["adx"] >= MIN_ADX
        and volume_confirmation(curr)
    )

    bearish_setup = (
        htf_trend == "BEARISH"
        and bearish_liquidity_sweep(lower_df)
        and displacement_candle(curr)
        and bearish_pattern
        and curr["rsi"] <= RSI_BEAR_MAX
        and curr["adx"] >= MIN_ADX
        and volume_confirmation(curr)
    )

    if not bullish_setup and not bearish_setup:
        return None

    price = curr["Close"]
    atr = curr["atr"]

    if bullish_setup:
        stop = price - atr * ATR_STOP_MULT
        target = price + ((price - stop) * RISK_REWARD)
        direction = "BUY / LONG"

    else:
        stop = price + atr * ATR_STOP_MULT
        target = price - ((stop - price) * RISK_REWARD)
        direction = "SELL / SHORT"

    confidence_score = 0

    confidence_score += 20 if htf_trend in ["BULLISH", "BEARISH"] else 0
    confidence_score += 20 if displacement_candle(curr) else 0
    confidence_score += 20 if volume_confirmation(curr) else 0
    confidence_score += 15 if curr["adx"] >= 25 else 10
    confidence_score += 15 if bullish_pattern or bearish_pattern else 0
    confidence_score += 10 if bullish_liquidity_sweep(lower_df) or bearish_liquidity_sweep(lower_df) else 0

    return {
        "market": name,
        "time": curr["timestamp"],
        "direction": direction,
        "entry": price,
        "stop": stop,
        "target": target,
        "htf_trend": htf_trend,
        "rsi": curr["rsi"],
        "adx": curr["adx"],
        "atr": atr,
        "confidence": confidence_score
    }


# =========================
# JOURNAL
# =========================

def save_signal(signal):
    df = pd.DataFrame([signal])

    if not os.path.exists(JOURNAL_FILE):
        df.to_csv(JOURNAL_FILE, index=False)
    else:
        df.to_csv(JOURNAL_FILE, mode="a", header=False, index=False)


def format_alert(signal):
    return f"""
*Institutional-Style Signal*

Market: *{signal['market']}*
Direction: *{signal['direction']}*
HTF Trend: `{signal['htf_trend']}`

Entry: `{signal['entry']:.2f}`
Stop Loss: `{signal['stop']:.2f}`
Take Profit: `{signal['target']:.2f}`

RSI: `{signal['rsi']:.2f}`
ADX: `{signal['adx']:.2f}`
ATR: `{signal['atr']:.2f}`

Confidence Score: *{signal['confidence']}/100*
Time: `{signal['time']}`
"""


# =========================
# MAIN LOOP
# =========================

def run():
    sent = set()

    while True:
        try:
            markets = {
                "BTC/USDT": (
                    fetch_btc(LOWER_TIMEFRAME, BTC_LIMIT),
                    fetch_btc(HIGHER_TIMEFRAME, BTC_LIMIT)
                ),
                "Gold Futures": (
                    fetch_gold(LOWER_TIMEFRAME),
                    fetch_gold(HIGHER_TIMEFRAME)
                )
            }

            for name, data in markets.items():
                lower_df, higher_df = data

                signal = institutional_signal(name, lower_df, higher_df)

                if signal:
                    key = f"{signal['market']}-{signal['time']}-{signal['direction']}"

                    if key not in sent:
                        save_signal(signal)
                        send_telegram(format_alert(signal))
                        sent.add(key)

                else:
                    print(f"{name}: no institutional setup.")

        except Exception as e:
            send_telegram(f"Bot error: `{e}`")
            print("Error:", e)

        time.sleep(SCAN_EVERY_SECONDS)


if __name__ == "__main__":
    run()
