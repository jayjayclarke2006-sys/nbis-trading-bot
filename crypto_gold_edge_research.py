import os
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
# CRYPTO / GOLD EDGE RESEARCH
# - studies time-of-day + price-zone expectancy
# - produces crypto_gold_edge_profile.json
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OUT_FILE = os.getenv("CRYPTO_GOLD_EDGE_PROFILE_FILE", "crypto_gold_edge_profile.json")

MARKETS = ["BTC", "GOLD"]
EXCHANGES = [
    ("coinbase", "BTC/USD"),
    ("kraken", "BTC/USD"),
    ("bybit", "BTC/USDT"),
    ("binanceus", "BTC/USDT"),
]

SUPPORTED_TFS = ["5m", "15m", "30m", "1h", "4h", "1d"]
TF_TO_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
PANDAS_RULES = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1H", "4h": "4H", "1d": "1D"}
YF_INTERVALS = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d"}
YF_PERIODS = {"5m": "60d", "15m": "60d", "30m": "60d", "1h": "730d", "1d": "10y"}

SETUPS = [
    {"name": "fast", "entry_tf": "5m", "confirm_tf": "15m", "bias_tf": "1h"},
    {"name": "intraday", "entry_tf": "15m", "confirm_tf": "1h", "bias_tf": "4h"},
    {"name": "swing", "entry_tf": "1h", "confirm_tf": "4h", "bias_tf": "1d"},
]

PARAMS = {
    "min_adx": 12,
    "rsi_bull": 50,
    "rsi_bear": 50,
    "volume_mult": 0.85,
    "atr_stop": 1.20,
    "rr": 1.70,
    "pullback_buffer_atr": 0.35,
    "retest_buffer_atr": 0.25,
    "compression_window": 12,
    "range_window": 20,
    "sweep_lookback": 8,
}

FETCH_CACHE = {}


def send(msg: str):
    print(msg)
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception:
        pass


def get_exchange(name):
    return getattr(ccxt, name)({"enableRateLimit": True, "timeout": 15000})


def validate_data(df):
    return df is not None and len(df) >= 250 and df.isna().sum().sum() == 0


def bars_needed(tf):
    return {"5m": 3000, "15m": 2200, "30m": 1800, "1h": 1500, "4h": 1000, "1d": 800}.get(tf, 1200)


def resample_ohlcv(df, tf):
    return (
        df.set_index("timestamp")
        .sort_index()
        .resample(PANDAS_RULES[tf])
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
        .reset_index()
    )


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

            if validate_data(df):
                df = df.sort_values("timestamp").reset_index(drop=True)
                FETCH_CACHE[cache_key] = df.copy()
                return df
        except Exception:
            continue

    return pd.DataFrame()


def fetch_gold(tf):
    cache_key = ("GOLD", tf)
    if cache_key in FETCH_CACHE:
        return FETCH_CACHE[cache_key].copy()

    if tf == "4h":
        base = fetch_gold("1h")
        out = resample_ohlcv(base, "4h")
        FETCH_CACHE[cache_key] = out.copy()
        return out

    df = yf.download("GC=F", period=YF_PERIODS[tf], interval=YF_INTERVALS[tf], auto_adjust=True, progress=False).reset_index()
    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "timestamp"})
    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "timestamp"})

    df.columns = [str(c).lower() for c in df.columns]
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].dropna()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    if validate_data(df):
        df = df.sort_values("timestamp").reset_index(drop=True)
        FETCH_CACHE[cache_key] = df.copy()
        return df

    return pd.DataFrame()


def fetch_market_tf(market, tf):
    return fetch_btc(tf) if market == "BTC" else fetch_gold(tf)


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
    merged = pd.merge_asof(entry_df.sort_values("timestamp"), confirm_df.sort_values("timestamp"), on="timestamp", direction="backward")
    merged = pd.merge_asof(merged.sort_values("timestamp"), bias_df.sort_values("timestamp"), on="timestamp", direction="backward")
    return merged.dropna().reset_index(drop=True)


def trend_values(close, ema20, ema50, ema200):
    if close > ema20 > ema50 > ema200:
        return "BULLISH"
    if close < ema20 < ema50 < ema200:
        return "BEARISH"
    return "NEUTRAL"


def entry_trend(row): return trend_values(row["close"], row["ema20"], row["ema50"], row["ema200"])
def confirm_trend(row): return trend_values(row["c_close"], row["c_ema20"], row["c_ema50"], row["c_ema200"])
def bias_trend(row): return trend_values(row["b_close"], row["b_ema20"], row["b_ema50"], row["b_ema200"])


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
    return bool(prev["close"] < prev["open"] and curr["close"] > curr["open"] and curr["close"] > prev["open"] and curr["open"] < prev["close"])


def bearish_engulf(prev, curr):
    return bool(prev["close"] > prev["open"] and curr["close"] < curr["open"] and curr["open"] > prev["close"] and curr["close"] < prev["open"])


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


def time_bucket(ts, tf):
    ts = pd.Timestamp(ts)
    minutes = TF_TO_MINUTES[tf]
    minute = (ts.minute // minutes) * minutes
    return f"{ts.hour:02d}:{minute:02d}"


def price_zone(row):
    close = float(row["close"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])
    ema200 = float(row["ema200"])
    if close > ema20 > ema50 > ema200:
        return "above_all"
    if close > ema20 > ema50 and close < ema200:
        return "bull_below_200"
    if close > ema20 and close < ema50:
        return "between_20_50"
    if close < ema20 < ema50 < ema200:
        return "below_all"
    if close < ema20 and close > ema50:
        return "between_20_50_bear"
    return "mixed"


def risk_levels(curr, next_open, direction):
    atr = float(curr["atr"])
    if direction == "LONG":
        stop = next_open - atr * PARAMS["atr_stop"]
        target = next_open + ((next_open - stop) * PARAMS["rr"])
    else:
        stop = next_open + atr * PARAMS["atr_stop"]
        target = next_open - ((stop - next_open) * PARAMS["rr"])
    return float(stop), float(target)


def trade_outcome(df, i, direction, entry, stop, target, max_hold):
    for j in range(i + 1, min(i + max_hold, len(df))):
        candle = df.iloc[j]
        if direction == "LONG":
            if float(candle["low"]) <= stop:
                return -1.0
            if float(candle["high"]) >= target:
                return (target - entry) / max(entry - stop, 1e-9)
        else:
            if float(candle["high"]) >= stop:
                return -1.0
            if float(candle["low"]) <= target:
                return (entry - target) / max(stop - entry, 1e-9)

    last_close = float(df.iloc[min(i + max_hold, len(df) - 1)]["close"])
    if direction == "LONG":
        return (last_close - entry) / max(entry - stop, 1e-9)
    return (entry - last_close) / max(stop - entry, 1e-9)


def collect_candidates(df, i):
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])
    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)

    out = []

    high_level = breakout_level_high(df, i, 20)
    low_level = breakout_level_low(df, i, 20)

    if high_level is not None:
        bull = curr["close"] > high_level and curr["close"] > prev["high"] and curr["close"] > curr["open"] and curr["close"] > curr["ema20"] and ctf == "BULLISH" and btf in ["BULLISH", "NEUTRAL"] and float(curr["adx"]) >= PARAMS["min_adx"] and float(curr["rsi"]) >= PARAMS["rsi_bull"]
        if bull:
            stop, target = risk_levels(curr, next_open, "LONG")
            out.append(("BREAKOUT_CONTINUATION", "LONG", next_open, stop, target))

    if low_level is not None:
        bear = curr["close"] < low_level and curr["close"] < prev["low"] and curr["close"] < curr["open"] and curr["close"] < curr["ema20"] and ctf == "BEARISH" and btf in ["BEARISH", "NEUTRAL"] and float(curr["adx"]) >= PARAMS["min_adx"] and float(curr["rsi"]) <= PARAMS["rsi_bear"]
        if bear:
            stop, target = risk_levels(curr, next_open, "SHORT")
            out.append(("BREAKOUT_CONTINUATION", "SHORT", next_open, stop, target))

    touched_long = curr["low"] <= curr["ema20"] + float(curr["atr"]) * PARAMS["pullback_buffer_atr"] or curr["low"] <= curr["ema50"] + float(curr["atr"]) * 0.15
    reclaimed_long = curr["close"] > curr["open"] and curr["close"] > curr["ema20"]
    if touched_long and reclaimed_long and (bullish_pin(curr) or bullish_engulf(prev, curr)) and ctf == "BULLISH" and btf in ["BULLISH", "NEUTRAL"]:
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append(("PULLBACK_CONTINUATION", "LONG", next_open, stop, target))

    touched_short = curr["high"] >= curr["ema20"] - float(curr["atr"]) * PARAMS["pullback_buffer_atr"] or curr["high"] >= curr["ema50"] - float(curr["atr"]) * 0.15
    reclaimed_short = curr["close"] < curr["open"] and curr["close"] < curr["ema20"]
    if touched_short and reclaimed_short and (bearish_pin(curr) or bearish_engulf(prev, curr)) and ctf == "BEARISH" and btf in ["BEARISH", "NEUTRAL"]:
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append(("PULLBACK_CONTINUATION", "SHORT", next_open, stop, target))

    recent_bull_break = any(df.iloc[j]["close"] > high_level for j in range(max(1, i - 6), i)) if high_level is not None else False
    recent_bear_break = any(df.iloc[j]["close"] < low_level for j in range(max(1, i - 6), i)) if low_level is not None else False

    if high_level is not None and recent_bull_break and curr["low"] <= high_level + float(curr["atr"]) * PARAMS["retest_buffer_atr"] and curr["close"] > high_level and (bullish_pin(curr) or curr["close"] > curr["open"]) and ctf == "BULLISH":
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append(("BREAKOUT_RETEST_REJECTION", "LONG", next_open, stop, target))

    if low_level is not None and recent_bear_break and curr["high"] >= low_level - float(curr["atr"]) * PARAMS["retest_buffer_atr"] and curr["close"] < low_level and (bearish_pin(curr) or curr["close"] < curr["open"]) and ctf == "BEARISH":
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append(("BREAKOUT_RETEST_REJECTION", "SHORT", next_open, stop, target))

    if liquidity_sweep_low(df, i, PARAMS["sweep_lookback"]) and (bullish_pin(curr) or bullish_engulf(prev, curr)):
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append(("LIQUIDITY_SWEEP_REVERSAL", "LONG", next_open, stop, target))

    if liquidity_sweep_high(df, i, PARAMS["sweep_lookback"]) and (bearish_pin(curr) or bearish_engulf(prev, curr)):
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append(("LIQUIDITY_SWEEP_REVERSAL", "SHORT", next_open, stop, target))

    if i - PARAMS["range_window"] >= 2:
        range_high = float(df["high"].iloc[i - PARAMS["range_window"]:i].max())
        range_low = float(df["low"].iloc[i - PARAMS["range_window"]:i].min())
        if curr["low"] <= range_low and curr["close"] > range_low and bullish_pin(curr):
            stop, target = risk_levels(curr, next_open, "LONG")
            out.append(("RANGE_REJECTION", "LONG", next_open, stop, target))
        if curr["high"] >= range_high and curr["close"] < range_high and bearish_pin(curr):
            stop, target = risk_levels(curr, next_open, "SHORT")
            out.append(("RANGE_REJECTION", "SHORT", next_open, stop, target))

    if i - PARAMS["compression_window"] >= 2:
        width = recent_range_width(df, i, PARAMS["compression_window"])
        if width is not None and width <= float(curr["atr"]) * 2.4:
            ch = breakout_level_high(df, i, PARAMS["compression_window"])
            cl = breakout_level_low(df, i, PARAMS["compression_window"])
            if curr["close"] > ch and curr["close"] > prev["high"] and ctf == "BULLISH":
                stop, target = risk_levels(curr, next_open, "LONG")
                out.append(("COMPRESSION_BREAKOUT", "LONG", next_open, stop, target))
            if curr["close"] < cl and curr["close"] < prev["low"] and ctf == "BEARISH":
                stop, target = risk_levels(curr, next_open, "SHORT")
                out.append(("COMPRESSION_BREAKOUT", "SHORT", next_open, stop, target))

    if prev["close"] < prev["ema20"] and curr["close"] > curr["ema20"] and curr["close"] > curr["open"]:
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append(("EMA_RECLAIM", "LONG", next_open, stop, target))
    if prev["close"] > prev["ema20"] and curr["close"] < curr["ema20"] and curr["close"] < curr["open"]:
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append(("EMA_RECLAIM", "SHORT", next_open, stop, target))

    return out


def run():
    FETCH_CACHE.clear()
    bucket_stats = {}

    for market in MARKETS:
        for setup in SETUPS:
            print(f"Researching {market} {setup['name']} ...")
            df = build_mtf_frame(market, setup)
            if df.empty or len(df) < 320:
                continue

            max_hold = 24 if setup["name"] == "fast" else 16 if setup["name"] == "intraday" else 12

            for i in range(260, len(df) - max_hold - 1):
                row = df.iloc[i]
                zone = price_zone(row)
                bucket = time_bucket(row["timestamp"], setup["entry_tf"])
                candidates = collect_candidates(df, i)

                for model, direction, entry, stop, target in candidates:
                    r_mult = trade_outcome(df, i, direction, entry, stop, target, max_hold)
                    keys = [
                        f"{market}|{setup['name']}|{model}|{direction}|{bucket}|{zone}",
                        f"{market}|{setup['name']}|{model}|{direction}|{bucket}|ALL",
                        f"{market}|ALL|{model}|{direction}|{bucket}|ALL",
                        f"{market}|ALL|{model}|{direction}|ALL|ALL",
                    ]
                    for k in keys:
                        bucket_stats.setdefault(k, []).append(float(r_mult))

    profile = {"updated_at": pd.Timestamp.utcnow().isoformat(), "buckets": {}}
    for k, vals in bucket_stats.items():
        arr = np.array(vals, dtype=float)
        if len(arr) < 8:
            continue
        profile["buckets"][k] = {
            "trades": int(len(arr)),
            "expectancy_r": float(arr.mean()),
            "win_rate": float((arr > 0).mean()),
            "median_r": float(np.median(arr)),
        }

    with open(OUT_FILE, "w") as f:
        json.dump(profile, f, indent=2)

    lines = []
    for k, info in profile["buckets"].items():
        market, setup, model, direction, bucket, zone = k.split("|")
        if setup != "ALL" or bucket == "ALL":
            continue
        lines.append(f"{market} {model} {direction}: expR {info['expectancy_r']:.2f}, WR {info['win_rate']:.1%}, n {info['trades']}")

    send("CRYPTO/GOLD EDGE RESEARCH COMPLETE\n\n" + ("\n".join(lines[:15]) if lines else "No robust buckets found."))


if __name__ == "__main__":
    run()
