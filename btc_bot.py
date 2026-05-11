import os
import time
import json
import requests
import numpy as np
import pandas as pd
import ccxt
import yfinance as yf

from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange


# ============================================================
# TELEGRAM
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
SENT_SIGNAL_FILE = "sent_signals.json"


def send_telegram(message: str):
    print(message)
    if BOT_TOKEN == "YOUR_BOT_TOKEN" or CHAT_ID == "YOUR_CHAT_ID":
        print("Telegram not configured.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        print("Telegram error:", e)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ============================================================
# SETTINGS
# ============================================================

LIVE_SCAN_SECONDS = int(os.getenv("LIVE_SCAN_SECONDS", "300"))
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() in ["1", "true", "yes", "y"]
TOP_SIGNALS_TO_SEND = int(os.getenv("TOP_SIGNALS_TO_SEND", "1"))
MIN_SCORE_TO_ALERT = float(os.getenv("MIN_SCORE_TO_ALERT", "62"))
SETUP_COOLDOWN_BARS = int(os.getenv("SETUP_COOLDOWN_BARS", "2"))

MARKETS = ["BTC", "GOLD"]

EXCHANGES = [
    ("coinbase", "BTC/USD"),
    ("kraken", "BTC/USD"),
    ("bybit", "BTC/USDT"),
    ("binanceus", "BTC/USDT"),
]

SUPPORTED_TFS = ["5m", "15m", "30m", "1h", "4h", "1d"]
TF_TO_MINUTES = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}
PANDAS_RULES = {
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
}
YF_INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "60m",
    "1d": "1d",
}
YF_PERIODS = {
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "1h": "730d",
    "1d": "10y",
}

SETUPS = [
    {"name": "fast", "entry_tf": "5m", "confirm_tf": "15m", "bias_tf": "1h", "enabled": True},
    {"name": "intraday", "entry_tf": "15m", "confirm_tf": "1h", "bias_tf": "4h", "enabled": True},
    {"name": "swing", "entry_tf": "1h", "confirm_tf": "4h", "bias_tf": "1d", "enabled": True},
]

PARAMS = {
    "min_adx": float(os.getenv("MIN_ADX", "12")),
    "rsi_bull": float(os.getenv("RSI_BULL", "50")),
    "rsi_bear": float(os.getenv("RSI_BEAR", "50")),
    "volume_mult": float(os.getenv("VOLUME_MULT", "0.85")),
    "atr_stop": float(os.getenv("ATR_STOP", "1.20")),
    "rr": float(os.getenv("RR_TARGET", "1.70")),
    "pullback_buffer_atr": float(os.getenv("PULLBACK_BUFFER_ATR", "0.35")),
    "retest_buffer_atr": float(os.getenv("RETEST_BUFFER_ATR", "0.25")),
    "compression_window": int(os.getenv("COMPRESSION_WINDOW", "12")),
    "range_window": int(os.getenv("RANGE_WINDOW", "20")),
    "sweep_lookback": int(os.getenv("SWEEP_LOOKBACK", "8")),
}


# ============================================================
# DATA FETCH / CACHE
# ============================================================

FETCH_CACHE = {}


def validate_setup(setup):
    e = setup["entry_tf"]
    c = setup["confirm_tf"]
    b = setup["bias_tf"]
    if e not in SUPPORTED_TFS or c not in SUPPORTED_TFS or b not in SUPPORTED_TFS:
        raise ValueError(f"Unsupported timeframe in setup {setup['name']}")
    if not (TF_TO_MINUTES[e] <= TF_TO_MINUTES[c] <= TF_TO_MINUTES[b]):
        raise ValueError(f"Invalid setup order in {setup['name']}")


for _setup in SETUPS:
    validate_setup(_setup)


def get_exchange(name):
    return getattr(ccxt, name)({
        "enableRateLimit": True,
        "timeout": 15000,
    })


def validate_data(df):
    if df is None or len(df) < 250:
        return False, "Not enough candles"
    if df.isna().sum().sum() > 0:
        return False, "NaN values"
    return True, "OK"


def bars_needed(tf):
    if tf == "5m":
        return 3000
    if tf == "15m":
        return 2200
    if tf == "30m":
        return 1800
    if tf == "1h":
        return 1500
    if tf == "4h":
        return 1000
    return 800


def resample_ohlcv(df, tf):
    rule = PANDAS_RULES[tf]
    rdf = (
        df.set_index("timestamp")
        .sort_index()
        .resample(rule)
        .agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        })
        .dropna()
        .reset_index()
    )
    return rdf


def fetch_btc(tf):
    cache_key = ("BTC", tf)
    if cache_key in FETCH_CACHE:
        return FETCH_CACHE[cache_key].copy()

    needed = bars_needed(tf)

    for name, symbol in EXCHANGES:
        try:
            exchange = get_exchange(name)
            exchange_tf = tf
            limit = needed

            if name == "coinbase" and tf == "4h":
                exchange_tf = "1h"
                limit = needed * 4 + 50

            candles = exchange.fetch_ohlcv(symbol, timeframe=exchange_tf, limit=limit)
            df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

            if name == "coinbase" and tf == "4h":
                df = resample_ohlcv(df, "4h")

            valid, reason = validate_data(df)
            if not valid:
                print(f"{name} {tf} rejected: {reason}")
                continue

            df = df.sort_values("timestamp").reset_index(drop=True)
            FETCH_CACHE[cache_key] = df.copy()
            print(f"Using BTC feed: {name} {tf}")
            return df

        except Exception as e:
            print(f"{name} {tf} failed:", e)

    raise Exception(f"No BTC feed available for {tf}")


def fetch_gold(tf):
    cache_key = ("GOLD", tf)
    if cache_key in FETCH_CACHE:
        return FETCH_CACHE[cache_key].copy()

    if tf == "4h":
        base = fetch_gold("1h")
        out = resample_ohlcv(base, "4h")
        FETCH_CACHE[cache_key] = out.copy()
        return out

    if tf not in YF_INTERVALS:
        raise ValueError(f"Gold timeframe unsupported directly: {tf}")

    df = yf.download(
        "GC=F",
        period=YF_PERIODS[tf],
        interval=YF_INTERVALS[tf],
        auto_adjust=True,
        progress=False,
    ).reset_index()

    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "timestamp"})
    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "timestamp"})

    df.columns = [str(c).lower() for c in df.columns]
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].dropna()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    valid, reason = validate_data(df)
    if not valid:
        raise Exception(f"Gold {tf} invalid: {reason}")

    df = df.sort_values("timestamp").reset_index(drop=True)
    FETCH_CACHE[cache_key] = df.copy()
    return df


def fetch_market_tf(market, tf):
    if market == "BTC":
        return fetch_btc(tf)
    return fetch_gold(tf)


# ============================================================
# INDICATORS / STRUCTURE
# ============================================================

def add_indicators(df):
    if df.empty or len(df) < 220:
        return pd.DataFrame()

    out = df.copy()
    out["ema20"] = EMAIndicator(out["close"], 20).ema_indicator()
    out["ema50"] = EMAIndicator(out["close"], 50).ema_indicator()
    out["ema200"] = EMAIndicator(out["close"], 200).ema_indicator()
    out["rsi"] = RSIIndicator(out["close"], 14).rsi()
    out["adx"] = ADXIndicator(out["high"], out["low"], out["close"], 14).adx()
    out["atr"] = AverageTrueRange(out["high"], out["low"], out["close"], 14).average_true_range()
    out["avg_volume"] = out["volume"].rolling(30).mean()
    out["body"] = (out["close"] - out["open"]).abs()
    out["range"] = out["high"] - out["low"]
    return out.dropna().reset_index(drop=True)


def prefix_df(df, prefix):
    return df.rename(columns={c: f"{prefix}{c}" for c in df.columns if c != "timestamp"})


def build_mtf_frame(market, setup):
    entry_df = add_indicators(fetch_market_tf(market, setup["entry_tf"]))
    confirm_df = add_indicators(fetch_market_tf(market, setup["confirm_tf"]))
    bias_df = add_indicators(fetch_market_tf(market, setup["bias_tf"]))

    if entry_df.empty or confirm_df.empty or bias_df.empty:
        return pd.DataFrame()

    confirm_df = prefix_df(confirm_df, "c_")
    bias_df = prefix_df(bias_df, "b_")

    merged = pd.merge_asof(
        entry_df.sort_values("timestamp"),
        confirm_df.sort_values("timestamp"),
        on="timestamp",
        direction="backward",
    )
    merged = pd.merge_asof(
        merged.sort_values("timestamp"),
        bias_df.sort_values("timestamp"),
        on="timestamp",
        direction="backward",
    )
    return merged.dropna().reset_index(drop=True)


def trend_values(close, ema20, ema50, ema200):
    if pd.isna(close) or pd.isna(ema20) or pd.isna(ema50) or pd.isna(ema200):
        return "NEUTRAL"
    if close > ema20 > ema50 > ema200:
        return "BULLISH"
    if close < ema20 < ema50 < ema200:
        return "BEARISH"
    return "NEUTRAL"


def entry_trend(row):
    return trend_values(row["close"], row["ema20"], row["ema50"], row["ema200"])


def confirm_trend(row):
    return trend_values(row["c_close"], row["c_ema20"], row["c_ema50"], row["c_ema200"])


def bias_trend(row):
    return trend_values(row["b_close"], row["b_ema20"], row["b_ema50"], row["b_ema200"])


def bullish_pin(row):
    body = abs(row["close"] - row["open"])
    rng = row["high"] - row["low"]
    if rng <= 0:
        return False
    lower = min(row["open"], row["close"]) - row["low"]
    upper = row["high"] - max(row["open"], row["close"])
    return lower > body * 1.8 and upper < body * 1.5 and row["close"] > row["open"]


def bearish_pin(row):
    body = abs(row["close"] - row["open"])
    rng = row["high"] - row["low"]
    if rng <= 0:
        return False
    upper = row["high"] - max(row["open"], row["close"])
    lower = min(row["open"], row["close"]) - row["low"]
    return upper > body * 1.8 and lower < body * 1.5 and row["close"] < row["open"]


def bullish_engulf(prev, curr):
    return bool(
        prev["close"] < prev["open"]
        and curr["close"] > curr["open"]
        and curr["close"] > prev["open"]
        and curr["open"] < prev["close"]
    )


def bearish_engulf(prev, curr):
    return bool(
        prev["close"] > prev["open"]
        and curr["close"] < curr["open"]
        and curr["open"] > prev["close"]
        and curr["close"] < prev["open"]
    )


def liquidity_sweep_low(df, i, lookback):
    if i - lookback < 1:
        return False
    swing_low = df["low"].iloc[i - lookback:i].min()
    return bool(df.iloc[i]["low"] < swing_low and df.iloc[i]["close"] > swing_low)


def liquidity_sweep_high(df, i, lookback):
    if i - lookback < 1:
        return False
    swing_high = df["high"].iloc[i - lookback:i].max()
    return bool(df.iloc[i]["high"] > swing_high and df.iloc[i]["close"] < swing_high)


def volume_ok(row):
    if pd.isna(row["avg_volume"]) or row["avg_volume"] <= 0:
        return True
    return row["volume"] >= row["avg_volume"] * PARAMS["volume_mult"]


def breakout_level_high(df, i, lookback=20):
    if i - lookback < 1:
        return None
    return float(df["high"].iloc[i - lookback:i].max())


def breakout_level_low(df, i, lookback=20):
    if i - lookback < 1:
        return None
    return float(df["low"].iloc[i - lookback:i].min())


def recent_range_width(df, i, window):
    if i - window < 1:
        return None
    return float(df["high"].iloc[i - window:i].max() - df["low"].iloc[i - window:i].min())


def trend_score(ltf, ctf, btf, direction):
    score = 0
    if direction == "LONG":
        score += 12 if ltf == "BULLISH" else 5 if ltf == "NEUTRAL" else 0
        score += 18 if ctf == "BULLISH" else 0
        score += 20 if btf == "BULLISH" else 8 if btf == "NEUTRAL" else 0
    else:
        score += 12 if ltf == "BEARISH" else 5 if ltf == "NEUTRAL" else 0
        score += 18 if ctf == "BEARISH" else 0
        score += 20 if btf == "BEARISH" else 8 if btf == "NEUTRAL" else 0
    return score


def momentum_score(row, direction):
    score = 0
    adx = float(row["adx"])
    rsi = float(row["rsi"])

    score += max(0, min((adx - 10) * 1.2, 16))

    if direction == "LONG":
        score += 12 if rsi >= 60 else 9 if rsi >= 55 else 6 if rsi >= 50 else 0
    else:
        score += 12 if rsi <= 40 else 9 if rsi <= 45 else 6 if rsi <= 50 else 0

    if volume_ok(row):
        score += 8

    return score


def risk_levels(curr, next_open, direction):
    atr = float(curr["atr"])
    if direction == "LONG":
        stop = next_open - atr * PARAMS["atr_stop"]
        target = next_open + ((next_open - stop) * PARAMS["rr"])
    else:
        stop = next_open + atr * PARAMS["atr_stop"]
        target = next_open - ((stop - next_open) * PARAMS["rr"])
    return round(float(stop), 2), round(float(target), 2)


# ============================================================
# SETUP DETECTORS
# ============================================================

def detect_breakout_continuation(df, i):
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])

    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    high_level = breakout_level_high(df, i, 20)
    low_level = breakout_level_low(df, i, 20)
    if high_level is None or low_level is None:
        return []

    out = []

    bull = (
        curr["close"] > high_level
        and curr["close"] > prev["high"]
        and curr["close"] > curr["open"]
        and curr["close"] > curr["ema20"]
        and ctf == "BULLISH"
        and btf in ["BULLISH", "NEUTRAL"]
        and float(curr["adx"]) >= PARAMS["min_adx"]
        and float(curr["rsi"]) >= PARAMS["rsi_bull"]
    )
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 18
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({
            "model": "BREAKOUT_CONTINUATION",
            "direction": "LONG",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": f"Breakout above {high_level:.2f} with follow-through",
        })

    bear = (
        ALLOW_SHORTS
        and curr["close"] < low_level
        and curr["close"] < prev["low"]
        and curr["close"] < curr["open"]
        and curr["close"] < curr["ema20"]
        and ctf == "BEARISH"
        and btf in ["BEARISH", "NEUTRAL"]
        and float(curr["adx"]) >= PARAMS["min_adx"]
        and float(curr["rsi"]) <= PARAMS["rsi_bear"]
    )
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 18
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({
            "model": "BREAKOUT_CONTINUATION",
            "direction": "SHORT",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": f"Breakdown below {low_level:.2f} with follow-through",
        })

    return out


def detect_pullback_continuation(df, i):
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])

    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    atr = float(curr["atr"])
    out = []

    touched_long = (
        curr["low"] <= curr["ema20"] + atr * PARAMS["pullback_buffer_atr"]
        or curr["low"] <= curr["ema50"] + atr * 0.15
    )
    reclaimed_long = curr["close"] > curr["open"] and curr["close"] > curr["ema20"]
    bull_reject = bullish_pin(curr) or bullish_engulf(prev, curr)

    if (
        touched_long and reclaimed_long and bull_reject
        and ctf == "BULLISH" and btf in ["BULLISH", "NEUTRAL"]
        and float(curr["rsi"]) >= PARAMS["rsi_bull"]
        and float(curr["adx"]) >= PARAMS["min_adx"]
    ):
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 16
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({
            "model": "PULLBACK_CONTINUATION",
            "direction": "LONG",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": "Trend pullback into value and bullish reclaim",
        })

    touched_short = (
        curr["high"] >= curr["ema20"] - atr * PARAMS["pullback_buffer_atr"]
        or curr["high"] >= curr["ema50"] - atr * 0.15
    )
    reclaimed_short = curr["close"] < curr["open"] and curr["close"] < curr["ema20"]
    bear_reject = bearish_pin(curr) or bearish_engulf(prev, curr)

    if (
        ALLOW_SHORTS
        and touched_short and reclaimed_short and bear_reject
        and ctf == "BEARISH" and btf in ["BEARISH", "NEUTRAL"]
        and float(curr["rsi"]) <= PARAMS["rsi_bear"]
        and float(curr["adx"]) >= PARAMS["min_adx"]
    ):
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 16
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({
            "model": "PULLBACK_CONTINUATION",
            "direction": "SHORT",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": "Trend pullback into value and bearish rejection",
        })

    return out


def detect_breakout_retest_rejection(df, i):
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])

    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    atr = float(curr["atr"])
    out = []

    high_level = breakout_level_high(df, i, 20)
    low_level = breakout_level_low(df, i, 20)
    if high_level is None or low_level is None:
        return out

    recent_bull_break = False
    recent_bear_break = False

    for j in range(max(1, i - 6), i):
        if df.iloc[j]["close"] > high_level:
            recent_bull_break = True
        if df.iloc[j]["close"] < low_level:
            recent_bear_break = True

    bull = (
        recent_bull_break
        and curr["low"] <= high_level + atr * PARAMS["retest_buffer_atr"]
        and curr["close"] > high_level
        and (bullish_pin(curr) or curr["close"] > curr["open"])
        and ctf == "BULLISH"
        and btf in ["BULLISH", "NEUTRAL"]
    )
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 20
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({
            "model": "BREAKOUT_RETEST_REJECTION",
            "direction": "LONG",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": f"Retest/rejection of breakout level {high_level:.2f}",
        })

    bear = (
        ALLOW_SHORTS
        and recent_bear_break
        and curr["high"] >= low_level - atr * PARAMS["retest_buffer_atr"]
        and curr["close"] < low_level
        and (bearish_pin(curr) or curr["close"] < curr["open"])
        and ctf == "BEARISH"
        and btf in ["BEARISH", "NEUTRAL"]
    )
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 20
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({
            "model": "BREAKOUT_RETEST_REJECTION",
            "direction": "SHORT",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": f"Retest/rejection of breakdown level {low_level:.2f}",
        })

    return out


def detect_liquidity_sweep_reversal(df, i):
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])

    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    out = []

    bull = (
        liquidity_sweep_low(df, i, PARAMS["sweep_lookback"])
        and (bullish_pin(curr) or bullish_engulf(prev, curr))
        and ctf != "BEARISH"
        and btf != "BEARISH"
        and float(curr["adx"]) >= PARAMS["min_adx"] - 2
    )
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 14
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({
            "model": "LIQUIDITY_SWEEP_REVERSAL",
            "direction": "LONG",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": "Downside liquidity sweep and reversal candle",
        })

    bear = (
        ALLOW_SHORTS
        and liquidity_sweep_high(df, i, PARAMS["sweep_lookback"])
        and (bearish_pin(curr) or bearish_engulf(prev, curr))
        and ctf != "BULLISH"
        and btf != "BULLISH"
        and float(curr["adx"]) >= PARAMS["min_adx"] - 2
    )
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 14
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({
            "model": "LIQUIDITY_SWEEP_REVERSAL",
            "direction": "SHORT",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": "Upside liquidity sweep and reversal candle",
        })

    return out


def detect_range_rejection(df, i):
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])
    window = PARAMS["range_window"]

    if i - window < 2:
        return []

    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    out = []

    range_high = float(df["high"].iloc[i - window:i].max())
    range_low = float(df["low"].iloc[i - window:i].min())

    bull = (
        curr["low"] <= range_low
        and curr["close"] > range_low
        and bullish_pin(curr)
        and ctf != "BEARISH"
    )
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 10
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({
            "model": "RANGE_REJECTION",
            "direction": "LONG",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": f"Range low rejection near {range_low:.2f}",
        })

    bear = (
        ALLOW_SHORTS
        and curr["high"] >= range_high
        and curr["close"] < range_high
        and bearish_pin(curr)
        and ctf != "BULLISH"
    )
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 10
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({
            "model": "RANGE_REJECTION",
            "direction": "SHORT",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": f"Range high rejection near {range_high:.2f}",
        })

    return out


def detect_compression_breakout(df, i):
    curr = df.iloc[i]
    prev = df.iloc[i - 1]
    next_open = float(df.iloc[i + 1]["open"])

    window = PARAMS["compression_window"]
    if i - window < 2:
        return []

    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    out = []

    recent_width = recent_range_width(df, i, window)
    if recent_width is None:
        return out

    compressed = recent_width <= float(curr["atr"]) * 2.4
    high_level = breakout_level_high(df, i, window)
    low_level = breakout_level_low(df, i, window)

    bull = (
        compressed
        and curr["close"] > high_level
        and curr["close"] > prev["high"]
        and ctf == "BULLISH"
        and btf in ["BULLISH", "NEUTRAL"]
    )
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 15
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({
            "model": "COMPRESSION_BREAKOUT",
            "direction": "LONG",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": "Compression resolved upward",
        })

    bear = (
        ALLOW_SHORTS
        and compressed
        and curr["close"] < low_level
        and curr["close"] < prev["low"]
        and ctf == "BEARISH"
        and btf in ["BEARISH", "NEUTRAL"]
    )
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 15
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({
            "model": "COMPRESSION_BREAKOUT",
            "direction": "SHORT",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": "Compression resolved downward",
        })

    return out


def detect_ema_reclaim(df, i):
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])

    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    out = []

    bull = (
        prev["close"] < prev["ema20"]
        and curr["close"] > curr["ema20"]
        and curr["close"] > curr["open"]
        and ctf == "BULLISH"
        and btf in ["BULLISH", "NEUTRAL"]
        and float(curr["rsi"]) >= PARAMS["rsi_bull"]
    )
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 9
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({
            "model": "EMA_RECLAIM",
            "direction": "LONG",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": "Price reclaimed EMA20 in trend context",
        })

    bear = (
        ALLOW_SHORTS
        and prev["close"] > prev["ema20"]
        and curr["close"] < curr["ema20"]
        and curr["close"] < curr["open"]
        and ctf == "BEARISH"
        and btf in ["BEARISH", "NEUTRAL"]
        and float(curr["rsi"]) <= PARAMS["rsi_bear"]
    )
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 9
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({
            "model": "EMA_RECLAIM",
            "direction": "SHORT",
            "score": round(score, 1),
            "entry": round(next_open, 2),
            "stop": stop,
            "target": target,
            "reason": "Price lost EMA20 in bearish trend context",
        })

    return out


ALL_MODELS = [
    detect_breakout_continuation,
    detect_pullback_continuation,
    detect_breakout_retest_rejection,
    detect_liquidity_sweep_reversal,
    detect_range_rejection,
    detect_compression_breakout,
    detect_ema_reclaim,
]


# ============================================================
# SCAN / SELECT
# ============================================================

def scan_setup_market(market, setup):
    df = build_mtf_frame(market, setup)
    if df.empty or len(df) < 260:
        return []

    i = len(df) - 2
    current = df.iloc[i]

    print(
        f"{market} {setup['name']} | "
        f"{setup['entry_tf']}/{setup['confirm_tf']}/{setup['bias_tf']} | "
        f"LTF={entry_trend(current)} CTF={confirm_trend(current)} BTF={bias_trend(current)} | "
        f"RSI={current['rsi']:.2f} ADX={current['adx']:.2f}"
    )

    all_candidates = []

    for fn in ALL_MODELS:
        try:
            candidates = fn(df, i)
            all_candidates.extend(candidates)
        except Exception as e:
            print(f"{market} {setup['name']} {fn.__name__} error:", e)

    for c in all_candidates:
        c["market"] = market
        c["setup_name"] = setup["name"]
        c["entry_tf"] = setup["entry_tf"]
        c["confirm_tf"] = setup["confirm_tf"]
        c["bias_tf"] = setup["bias_tf"]
        c["time"] = str(df.iloc[i]["timestamp"])
        rr = abs(c["target"] - c["entry"]) / max(abs(c["entry"] - c["stop"]), 1e-9)
        c["rr"] = round(rr, 2)

    return all_candidates


def dedupe_candidates(candidates):
    kept = []
    seen = set()

    for c in sorted(candidates, key=lambda x: x["score"], reverse=True):
        key = (c["market"], c["direction"], c["time"])
        if key in seen:
            continue
        kept.append(c)
        seen.add(key)

    return kept


def format_signal(signal):
    return (
        "ð¨ BEST LIVE SIGNAL ð¨\n\n"
        f"Market: {signal['market']}\n"
        f"Model: {signal['model']}\n"
        f"Direction: {signal['direction']}\n"
        f"Score: {signal['score']}/100\n\n"
        f"Entry: {signal['entry']}\n"
        f"Stop Loss: {signal['stop']}\n"
        f"Take Profit: {signal['target']}\n"
        f"R:R: {signal['rr']}\n\n"
        f"Setup: {signal['setup_name']} "
        f"({signal['entry_tf']} / {signal['confirm_tf']} / {signal['bias_tf']})\n"
        f"Reason: {signal['reason']}\n"
        f"Time: {signal['time']}"
    )


def maybe_send_signal(signal):
    sent = load_json(SENT_SIGNAL_FILE, {})
    key = (
        f"{signal['market']}_"
        f"{signal['setup_name']}_"
        f"{signal['model']}_"
        f"{signal['direction']}_"
        f"{signal['time']}"
    )

    if sent.get(key):
        print("Duplicate signal blocked:", key)
        return

    send_telegram(format_signal(signal))
    sent[key] = True
    save_json(SENT_SIGNAL_FILE, sent)


# ============================================================
# MAIN LOOP
# ============================================================

def run():
    enabled_setups = [s for s in SETUPS if s.get("enabled", True)]
    if not enabled_setups:
        raise ValueError("No setups enabled.")

    send_telegram(
        "CRYPTO/GOLD BEST-SETUP BOT STARTED\n\n"
        "It scans multiple timeframe structures and picks the strongest setup.\n\n"
        "Models:\n"
        "- breakout continuation\n"
        "- pullback continuation\n"
        "- breakout retest rejection\n"
        "- liquidity sweep reversal\n"
        "- range rejection\n"
        "- compression breakout\n"
        "- EMA reclaim\n\n"
        "Enabled setups:\n" +
        "\n".join(
            f"- {s['name']}: {s['entry_tf']} / {s['confirm_tf']} / {s['bias_tf']}"
            for s in enabled_setups
        ) +
        f"\n\nMin score to alert: {MIN_SCORE_TO_ALERT}\n"
        f"Signals sent each cycle: {TOP_SIGNALS_TO_SEND}"
    )

    while True:
        try:
            FETCH_CACHE.clear()
            all_candidates = []

            for market in MARKETS:
                for setup in enabled_setups:
                    try:
                        candidates = scan_setup_market(market, setup)
                        all_candidates.extend(candidates)
                    except Exception as e:
                        print(f"{market} {setup['name']} scan error:", e)

            if not all_candidates:
                print("No candidates this cycle.")
            else:
                ranked = dedupe_candidates(all_candidates)
                ranked = [r for r in ranked if r["score"] >= MIN_SCORE_TO_ALERT]

                if not ranked:
                    print("Candidates found but none passed alert threshold.")
                else:
                    top = ranked[:TOP_SIGNALS_TO_SEND]

                    print("Top candidates:")
                    for signal in top:
                        print(
                            f"{signal['market']} {signal['setup_name']} {signal['model']} "
                            f"{signal['direction']} score={signal['score']}"
                        )
                        maybe_send_signal(signal)

        except Exception as e:
            send_telegram(f"Bot error: {e}")

        time.sleep(LIVE_SCAN_SECONDS)


if __name__ == "__main__":
    run()
