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

def safe_float_env(name, default):
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except Exception:
        print(f"WARNING: {name} must be a number. Got {raw!r}. Using default {default}.")
        return float(default)


def safe_int_env(name, default):
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except Exception:
        print(f"WARNING: {name} must be an integer. Got {raw!r}. Using default {default}.")
        return int(default)


# ============================================================
# CRYPTO / GOLD MERGED LIVE BOT - LOOSER VERSION
# - multi-setup scan
# - ranked best-candidate selection
# - soft edge weighting only (boost-only)
# - market regime detection + soft regime scoring
# - Telegram signals only
# - tuned to fire more often
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
SENT_SIGNAL_FILE = "crypto_gold_sent_signals.json"
EDGE_PROFILE_FILE = os.getenv("CRYPTO_GOLD_EDGE_PROFILE_FILE", "crypto_gold_edge_profile.json")

LIVE_SCAN_SECONDS = safe_int_env("LIVE_SCAN_SECONDS", 60)
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() in ["1", "true", "yes", "y"]
TOP_SIGNALS_TO_SEND = safe_int_env("TOP_SIGNALS_TO_SEND", 1)
MIN_SCORE_TO_ALERT = safe_float_env("MIN_SCORE_TO_ALERT", 54)
SIGNAL_COOLDOWN_MINUTES = safe_int_env("SIGNAL_COOLDOWN_MINUTES", 30)
SAME_DIRECTION_COOLDOWN = os.getenv("SAME_DIRECTION_COOLDOWN", "true").lower() in ["1", "true", "yes", "y"]

# Signal result tracking.
# This tracks the alerts as virtual trades because this BTC/gold bot sends signals only.
TRACK_SIGNAL_RESULTS = os.getenv("TRACK_SIGNAL_RESULTS", "true").lower() in ["1", "true", "yes", "y"]
TRADE_STATE_FILE = os.getenv("CRYPTO_GOLD_TRADE_STATE_FILE", "crypto_gold_signal_trade_state.json")
SIGNAL_RISK_CASH = safe_float_env("SIGNAL_RISK_CASH", 100)
TP_SL_CHECK_TIMEFRAME = os.getenv("TP_SL_CHECK_TIMEFRAME", "5m")
CONSERVATIVE_SAME_CANDLE_EXIT = os.getenv("CONSERVATIVE_SAME_CANDLE_EXIT", "true").lower() in ["1", "true", "yes", "y"]
MAX_OPEN_SIGNAL_TRADES = int(os.getenv("MAX_OPEN_SIGNAL_TRADES", "10"))

# Market regime detection.
# Default is SOFT scoring, not hard blocking.
USE_REGIME_DETECTION = os.getenv("USE_REGIME_DETECTION", "true").lower() in ["1", "true", "yes", "y"]
REGIME_BLOCK_CONFLICTS = os.getenv("REGIME_BLOCK_CONFLICTS", "false").lower() in ["1", "true", "yes", "y"]
REGIME_TREND_ADX = float(os.getenv("REGIME_TREND_ADX", "18"))
REGIME_CHOP_ADX = float(os.getenv("REGIME_CHOP_ADX", "10"))
REGIME_VOL_LOOKBACK = int(os.getenv("REGIME_VOL_LOOKBACK", "100"))
REGIME_HIGH_VOL_MULT = float(os.getenv("REGIME_HIGH_VOL_MULT", "1.30"))
REGIME_LOW_VOL_MULT = float(os.getenv("REGIME_LOW_VOL_MULT", "0.70"))
REGIME_RANGE_WINDOW = int(os.getenv("REGIME_RANGE_WINDOW", "24"))
REGIME_RANGE_ATR_MULT = float(os.getenv("REGIME_RANGE_ATR_MULT", "3.00"))

MARKETS = ["BTC", "GOLD"]

EXCHANGES = [
    ("coinbase", "BTC/USD"),
    ("kraken", "BTC/USD"),
    ("bybit", "BTC/USDT"),
    ("binanceus", "BTC/USDT"),
]

SUPPORTED_TFS = ["5m", "15m", "30m", "1h", "4h", "1d"]
TF_TO_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
PANDAS_RULES = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1D"}
YF_INTERVALS = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d"}
YF_PERIODS = {"5m": "60d", "15m": "60d", "30m": "60d", "1h": "730d", "1d": "10y"}

SETUPS = [
    {"name": "fast", "entry_tf": "5m", "confirm_tf": "15m", "bias_tf": "1h", "enabled": os.getenv("ENABLE_FAST_SETUP", "true").lower() in ["1", "true", "yes", "y"]},
    {"name": "intraday", "entry_tf": "15m", "confirm_tf": "1h", "bias_tf": "4h", "enabled": os.getenv("ENABLE_INTRADAY_SETUP", "true").lower() in ["1", "true", "yes", "y"]},
    {"name": "swing", "entry_tf": "1h", "confirm_tf": "4h", "bias_tf": "1d", "enabled": os.getenv("ENABLE_SWING_SETUP", "false").lower() in ["1", "true", "yes", "y"]},
]

PARAMS = {
    "min_adx": safe_float_env("MIN_ADX", 10),
    "rsi_bull": safe_float_env("RSI_BULL", 48),
    "rsi_bear": safe_float_env("RSI_BEAR", 52),
    "volume_mult": safe_float_env("VOLUME_MULT", 0.70),
    "atr_stop": safe_float_env("ATR_STOP", 1.10),
    "rr": safe_float_env("RR_TARGET", 1.50),
    "pullback_buffer_atr": safe_float_env("PULLBACK_BUFFER_ATR", 0.50),
    "retest_buffer_atr": safe_float_env("RETEST_BUFFER_ATR", 0.40),
    "compression_window": safe_int_env("COMPRESSION_WINDOW", 10),
    "range_window": safe_int_env("RANGE_WINDOW", 16),
    "sweep_lookback": safe_int_env("SWEEP_LOOKBACK", 6),
}

FETCH_CACHE = {}

# ============================================================
# EMOJI CONSTANTS
# Use unicode escapes instead of pasted emoji characters.
# This prevents mojibake like Ã°Å¸Å¡â¬ / Ã°Å¸âÅ  in Telegram.
# ============================================================

E_ALERT = "\U0001F6A8"      # ð¨
E_GOLD = "\U0001F7E1"       # ð¡
E_BTC = "\u20BF"            # â¿
E_LONG = "\U0001F680\U0001F7E2"   # ðð¢
E_SHORT = "\U0001F53B\U0001F534"  # ð»ð´
E_BOOM = "\U0001F4A5"       # ð¥
E_TARGET = "\U0001F3AF"     # ð¯
E_RETEST = "\U0001F501"     # ð
E_WATER = "\U0001F4A7"      # ð§
E_BOX = "\U0001F4E6"        # ð¦
E_ZAP = "\u26A1"            # â¡
E_CHART_UP = "\U0001F4C8"   # ð
E_STOP = "\U0001F6D1"       # ð
E_MONEY = "\U0001F4B0"      # ð°
E_SCALE = "\u2696\uFE0F"    # âï¸
E_CLOCK = "\u23F1\uFE0F"    # â±ï¸
E_BRAIN = "\U0001F9E0"      # ð§ 
E_CHART = "\U0001F4CA"      # ð
E_WEATHER = "\U0001F326\uFE0F"  # ð¦ï¸
E_PLUS = "\u2795"           # â
E_TIME = "\U0001F552"       # ð
E_CHECK = "\u2705"          # â
E_CROSS = "\u274C"          # â
E_TROPHY = "\U0001F3C6"     # ð
E_BAG = "\U0001F4B0"        # ð°
E_BOOK = "\U0001F4D2"       # ð
E_WARNING = "\u26A0\uFE0F" # â ï¸



# ============================================================
# HELPERS
# ============================================================

def send_telegram(message: str):
    print(message)
    if BOT_TOKEN == "YOUR_BOT_TOKEN" or CHAT_ID == "YOUR_CHAT_ID":
        print("Telegram not configured.")
        return
    try:
        payload = json.dumps(
            {"chat_id": CHAT_ID, "text": message},
            ensure_ascii=False,
        ).encode("utf-8")

        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
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


def validate_setup(setup):
    for key in ["entry_tf", "confirm_tf", "bias_tf"]:
        if setup[key] not in SUPPORTED_TFS:
            raise ValueError(f"Unsupported timeframe {setup[key]} in setup {setup['name']}")
    e = TF_TO_MINUTES[setup["entry_tf"]]
    c = TF_TO_MINUTES[setup["confirm_tf"]]
    b = TF_TO_MINUTES[setup["bias_tf"]]
    if not (e <= c <= b):
        raise ValueError(f"Invalid setup order in {setup['name']}")


for _setup in SETUPS:
    validate_setup(_setup)


def get_exchange(name):
    return getattr(ccxt, name)({"enableRateLimit": True, "timeout": 15000})


def validate_data(df):
    if df is None or len(df) < 250:
        return False, "Not enough candles"
    if df.isna().sum().sum() > 0:
        return False, "NaN values"
    if "volume" in df.columns and (df["volume"] < 0).any():
        return False, "Negative volume"
    return True, "OK"


def bars_needed(tf):
    return {"5m": 3000, "15m": 2200, "30m": 1800, "1h": 1500, "4h": 1000, "1d": 800}.get(tf, 1200)


# ============================================================
# DATA FETCH
# ============================================================

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

            valid, _ = validate_data(df)
            if valid:
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
        group_by="column",
    )

    if df is None or df.empty:
        raise Exception(f"Gold {tf} returned empty data from Yahoo")

    # yfinance sometimes returns MultiIndex columns like ("Close", "GC=F").
    # Flatten them so the bot always gets timestamp/open/high/low/close/volume.
    if isinstance(df.columns, pd.MultiIndex):
        flattened = []
        for col in df.columns:
            first = str(col[0]).lower()
            last = str(col[-1]).lower()
            if first in ["open", "high", "low", "close", "volume"]:
                flattened.append(first)
            elif last in ["open", "high", "low", "close", "volume"]:
                flattened.append(last)
            else:
                flattened.append(first)
        df.columns = flattened
    else:
        df.columns = [str(c).lower() for c in df.columns]

    df = df.reset_index()
    df.columns = [str(c).lower() for c in df.columns]

    if "datetime" in df.columns:
        df = df.rename(columns={"datetime": "timestamp"})
    elif "date" in df.columns:
        df = df.rename(columns={"date": "timestamp"})
    elif "index" in df.columns:
        df = df.rename(columns={"index": "timestamp"})

    df = df.loc[:, ~df.columns.duplicated()].copy()

    needed = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise Exception(f"Gold {tf} missing columns: {missing}. Got columns: {list(df.columns)}")

    df = df[needed].dropna()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna()

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
# INDICATORS / FRAME BUILD
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
    out["atr_pct"] = out["atr"] / out["close"]
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

    merged = pd.merge_asof(entry_df.sort_values("timestamp"), confirm_df.sort_values("timestamp"), on="timestamp", direction="backward")
    merged = pd.merge_asof(merged.sort_values("timestamp"), bias_df.sort_values("timestamp"), on="timestamp", direction="backward")
    return merged.dropna().reset_index(drop=True)


# ============================================================
# STRUCTURE / LOGIC
# ============================================================

def trend_values(close, ema20, ema50, ema200):
    if pd.isna(close) or pd.isna(ema20) or pd.isna(ema50) or pd.isna(ema200):
        return "NEUTRAL"
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
    return lower > body * 1.4 and upper < body * 1.7 and row["close"] > row["open"]


def bearish_pin(row):
    body = abs(row["close"] - row["open"])
    rng = row["high"] - row["low"]
    if rng <= 0:
        return False
    upper = row["high"] - max(row["open"], row["close"])
    lower = min(row["open"], row["close"]) - row["low"]
    return upper > body * 1.4 and lower < body * 1.7 and row["close"] < row["open"]


def bullish_engulf(prev, curr):
    return bool(prev["close"] < prev["open"] and curr["close"] > curr["open"] and curr["close"] > prev["open"] and curr["open"] <= prev["close"])


def bearish_engulf(prev, curr):
    return bool(prev["close"] > prev["open"] and curr["close"] < curr["open"] and curr["open"] >= prev["close"] and curr["close"] < prev["open"])


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
        score += 12 if ltf == "BULLISH" else 7 if ltf == "NEUTRAL" else 0
        score += 18 if ctf == "BULLISH" else 8 if ctf == "NEUTRAL" else 0
        score += 20 if btf == "BULLISH" else 10 if btf == "NEUTRAL" else 0
    else:
        score += 12 if ltf == "BEARISH" else 7 if ltf == "NEUTRAL" else 0
        score += 18 if ctf == "BEARISH" else 8 if ctf == "NEUTRAL" else 0
        score += 20 if btf == "BEARISH" else 10 if btf == "NEUTRAL" else 0
    return score


def momentum_score(row, direction):
    score = 0
    adx = float(row["adx"])
    rsi = float(row["rsi"])
    score += max(0, min((adx - 8) * 1.4, 18))
    if direction == "LONG":
        score += 12 if rsi >= 58 else 9 if rsi >= 53 else 6 if rsi >= 48 else 0
    else:
        score += 12 if rsi <= 42 else 9 if rsi <= 47 else 6 if rsi <= 52 else 0
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


def edge_adjustment(signal, row):
    profile = load_json(EDGE_PROFILE_FILE, {})
    buckets = profile.get("buckets", {})
    market = signal["market"]
    setup = signal["setup_name"]
    model = signal["model"]
    direction = signal["direction"]
    bucket = time_bucket(signal["time"], signal["entry_tf"])
    zone = price_zone(row)

    keys = [
        f"{market}|{setup}|{model}|{direction}|{bucket}|{zone}",
        f"{market}|{setup}|{model}|{direction}|{bucket}|ALL",
        f"{market}|ALL|{model}|{direction}|{bucket}|ALL",
        f"{market}|ALL|{model}|{direction}|ALL|ALL",
    ]

    chosen = None
    for k in keys:
        info = buckets.get(k)
        if info and info.get("trades", 0) >= 15:
            chosen = info
            break

    if not chosen:
        signal["edge_note"] = "no_profile"
        return signal

    expectancy_r = float(chosen.get("expectancy_r", 0))
    win_rate = float(chosen.get("win_rate", 0))
    trades = int(chosen.get("trades", 0))

    # Boost-only.
    adj = max(0, min(8, expectancy_r * 4 + (win_rate - 0.5) * 8))
    signal["score"] = round(signal["score"] + adj, 1)
    signal["edge_note"] = f"expR={expectancy_r:.2f}, wr={win_rate:.1%}, n={trades}"
    return signal



# ============================================================
# MARKET REGIME DETECTION
# ============================================================

def detect_market_regime(df, i, row):
    """
    Detects the current market regime for the active market/timeframe stack.

    It does NOT predict the future. It classifies the current structure so
    the bot can favor the setup type that fits the environment.
    """
    if not USE_REGIME_DETECTION:
        return {
            "name": "REGIME_OFF",
            "score_bias": 0,
            "note": "regime detection disabled",
        }

    ltf = entry_trend(row)
    ctf = confirm_trend(row)
    btf = bias_trend(row)

    adx = float(row.get("adx", 0))
    close = float(row.get("close", 0))
    open_ = float(row.get("open", 0))
    atr = float(row.get("atr", 0))

    if atr <= 0 or i < max(REGIME_RANGE_WINDOW, 30):
        return {
            "name": "UNKNOWN",
            "score_bias": 0,
            "note": "not enough data",
        }

    vol_start = max(0, i - REGIME_VOL_LOOKBACK)
    atr_med = float(df["atr"].iloc[vol_start:i].median())
    atr_ratio = atr / atr_med if atr_med > 0 else 1.0

    range_width = recent_range_width(df, i, REGIME_RANGE_WINDOW)
    range_atr = range_width / atr if range_width is not None and atr > 0 else 999.0

    high_level = breakout_level_high(df, i, REGIME_RANGE_WINDOW)
    low_level = breakout_level_low(df, i, REGIME_RANGE_WINDOW)

    body = abs(close - open_)
    candle_range = float(row["high"] - row["low"]) if "high" in row.index and "low" in row.index else 0.0
    displacement = candle_range > atr * 1.10 and body >= candle_range * 0.50 if candle_range > 0 else False

    broke_up = high_level is not None and close > high_level
    broke_down = low_level is not None and close < low_level

    trend_bull = ltf == "BULLISH" and ctf in ["BULLISH", "NEUTRAL"] and btf in ["BULLISH", "NEUTRAL"]
    trend_bear = ltf == "BEARISH" and ctf in ["BEARISH", "NEUTRAL"] and btf in ["BEARISH", "NEUTRAL"]

    strong_bull = ltf == "BULLISH" and ctf == "BULLISH" and btf == "BULLISH"
    strong_bear = ltf == "BEARISH" and ctf == "BEARISH" and btf == "BEARISH"

    if atr_ratio >= REGIME_HIGH_VOL_MULT and displacement and broke_up:
        return {
            "name": "HIGH_VOL_BREAKOUT_BULL",
            "score_bias": 8,
            "note": f"high vol bull breakout | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
        }

    if atr_ratio >= REGIME_HIGH_VOL_MULT and displacement and broke_down:
        return {
            "name": "HIGH_VOL_BREAKOUT_BEAR",
            "score_bias": 8,
            "note": f"high vol bear breakout | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
        }

    if strong_bull and adx >= REGIME_TREND_ADX:
        return {
            "name": "STRONG_BULL_TREND",
            "score_bias": 7,
            "note": f"all TF bullish | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
        }

    if strong_bear and adx >= REGIME_TREND_ADX:
        return {
            "name": "STRONG_BEAR_TREND",
            "score_bias": 7,
            "note": f"all TF bearish | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
        }

    if trend_bull and adx >= REGIME_TREND_ADX:
        return {
            "name": "BULL_TREND",
            "score_bias": 5,
            "note": f"bull trend | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
        }

    if trend_bear and adx >= REGIME_TREND_ADX:
        return {
            "name": "BEAR_TREND",
            "score_bias": 5,
            "note": f"bear trend | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
        }

    if atr_ratio <= REGIME_LOW_VOL_MULT:
        return {
            "name": "LOW_VOL_COMPRESSION",
            "score_bias": 2,
            "note": f"low vol compression | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
        }

    if adx <= REGIME_CHOP_ADX and range_atr <= REGIME_RANGE_ATR_MULT:
        return {
            "name": "CHOP_RANGE",
            "score_bias": -2,
            "note": f"chop/range | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
        }

    if range_atr <= REGIME_RANGE_ATR_MULT:
        return {
            "name": "RANGE",
            "score_bias": 1,
            "note": f"range | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
        }

    return {
        "name": "MIXED",
        "score_bias": 0,
        "note": f"mixed | adx={adx:.1f}, atr_ratio={atr_ratio:.2f}, range_atr={range_atr:.2f}",
    }


def apply_regime_adjustment(signal, regime):
    """
    Soft regime-based scoring. This makes the bot favor the setups that fit
    the market environment without killing trade frequency.

    REGIME_BLOCK_CONFLICTS can be enabled, but it is false by default.
    """
    if not USE_REGIME_DETECTION:
        signal["regime"] = "REGIME_OFF"
        signal["regime_note"] = "regime detection disabled"
        return signal

    name = regime.get("name", "UNKNOWN")
    model = signal.get("model", "")
    direction = signal.get("direction", "")

    adj = 0
    blocked = False

    trend_models = ["BREAKOUT_CONTINUATION", "PULLBACK_CONTINUATION", "BREAKOUT_RETEST_REJECTION", "EMA_RECLAIM"]
    reversal_models = ["LIQUIDITY_SWEEP_REVERSAL", "RANGE_REJECTION"]
    compression_models = ["COMPRESSION_BREAKOUT"]

    if name in ["STRONG_BULL_TREND", "BULL_TREND"]:
        if direction == "LONG" and model in trend_models:
            adj += 8
        elif direction == "LONG" and model in reversal_models:
            adj += 3
        elif direction == "SHORT":
            adj -= 6
            blocked = REGIME_BLOCK_CONFLICTS and name == "STRONG_BULL_TREND"

    elif name in ["STRONG_BEAR_TREND", "BEAR_TREND"]:
        if direction == "SHORT" and model in trend_models:
            adj += 8
        elif direction == "SHORT" and model in reversal_models:
            adj += 3
        elif direction == "LONG":
            adj -= 6
            blocked = REGIME_BLOCK_CONFLICTS and name == "STRONG_BEAR_TREND"

    elif name == "HIGH_VOL_BREAKOUT_BULL":
        if direction == "LONG" and model in ["BREAKOUT_CONTINUATION", "BREAKOUT_RETEST_REJECTION", "EMA_RECLAIM"]:
            adj += 10
        elif direction == "SHORT":
            adj -= 5

    elif name == "HIGH_VOL_BREAKOUT_BEAR":
        if direction == "SHORT" and model in ["BREAKOUT_CONTINUATION", "BREAKOUT_RETEST_REJECTION", "EMA_RECLAIM"]:
            adj += 10
        elif direction == "LONG":
            adj -= 5

    elif name in ["RANGE", "CHOP_RANGE"]:
        if model in reversal_models:
            adj += 8
        elif model in compression_models:
            adj += 4
        elif model == "BREAKOUT_CONTINUATION":
            adj -= 4

    elif name == "LOW_VOL_COMPRESSION":
        if model in compression_models:
            adj += 8
        elif model in reversal_models:
            adj += 3
        else:
            adj -= 2

    # Mild global regime bias
    adj += float(regime.get("score_bias", 0)) * 0.25

    signal["score"] = round(float(signal["score"]) + adj, 1)
    signal["regime"] = name
    signal["regime_note"] = regime.get("note", "")
    signal["regime_adjustment"] = round(adj, 1)
    signal["blocked_by_regime"] = blocked
    return signal

# ============================================================
# MODELS
# ============================================================

def detect_breakout_continuation(df, i):
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])
    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    high_level = breakout_level_high(df, i, 18)
    low_level = breakout_level_low(df, i, 18)
    out = []

    if high_level is not None:
        bull = (
            curr["close"] > high_level
            and curr["close"] > prev["high"]
            and curr["close"] >= curr["open"]
            and ctf in ["BULLISH", "NEUTRAL"]
            and btf in ["BULLISH", "NEUTRAL"]
            and float(curr["adx"]) >= PARAMS["min_adx"]
            and float(curr["rsi"]) >= PARAMS["rsi_bull"]
        )
        if bull:
            score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 18
            stop, target = risk_levels(curr, next_open, "LONG")
            out.append({"model": "BREAKOUT_CONTINUATION", "direction": "LONG", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": f"Breakout above {high_level:.2f} with follow-through"})

    if low_level is not None and ALLOW_SHORTS:
        bear = (
            curr["close"] < low_level
            and curr["close"] < prev["low"]
            and curr["close"] <= curr["open"]
            and ctf in ["BEARISH", "NEUTRAL"]
            and btf in ["BEARISH", "NEUTRAL"]
            and float(curr["adx"]) >= PARAMS["min_adx"]
            and float(curr["rsi"]) <= PARAMS["rsi_bear"]
        )
        if bear:
            score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 18
            stop, target = risk_levels(curr, next_open, "SHORT")
            out.append({"model": "BREAKOUT_CONTINUATION", "direction": "SHORT", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": f"Breakdown below {low_level:.2f} with follow-through"})

    return out


def detect_pullback_continuation(df, i):
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])
    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    atr = float(curr["atr"])
    out = []

    touched_long = curr["low"] <= curr["ema20"] + atr * PARAMS["pullback_buffer_atr"] or curr["low"] <= curr["ema50"] + atr * 0.25
    reclaimed_long = curr["close"] >= curr["open"] and curr["close"] > curr["ema20"]
    bull_reject = bullish_pin(curr) or bullish_engulf(prev, curr)
    if touched_long and reclaimed_long and bull_reject and ctf in ["BULLISH", "NEUTRAL"] and btf in ["BULLISH", "NEUTRAL"] and float(curr["rsi"]) >= PARAMS["rsi_bull"] and float(curr["adx"]) >= PARAMS["min_adx"]:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 16
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({"model": "PULLBACK_CONTINUATION", "direction": "LONG", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": "Trend pullback into value and bullish reclaim"})

    touched_short = curr["high"] >= curr["ema20"] - atr * PARAMS["pullback_buffer_atr"] or curr["high"] >= curr["ema50"] - atr * 0.25
    reclaimed_short = curr["close"] <= curr["open"] and curr["close"] < curr["ema20"]
    bear_reject = bearish_pin(curr) or bearish_engulf(prev, curr)
    if ALLOW_SHORTS and touched_short and reclaimed_short and bear_reject and ctf in ["BEARISH", "NEUTRAL"] and btf in ["BEARISH", "NEUTRAL"] and float(curr["rsi"]) <= PARAMS["rsi_bear"] and float(curr["adx"]) >= PARAMS["min_adx"]:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 16
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({"model": "PULLBACK_CONTINUATION", "direction": "SHORT", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": "Trend pullback into value and bearish rejection"})

    return out


def detect_breakout_retest_rejection(df, i):
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])
    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    atr = float(curr["atr"])
    out = []

    high_level = breakout_level_high(df, i, 18)
    low_level = breakout_level_low(df, i, 18)
    if high_level is None or low_level is None:
        return out

    recent_bull_break = any(df.iloc[j]["close"] > high_level for j in range(max(1, i - 8), i))
    recent_bear_break = any(df.iloc[j]["close"] < low_level for j in range(max(1, i - 8), i))

    bull = recent_bull_break and curr["low"] <= high_level + atr * PARAMS["retest_buffer_atr"] and curr["close"] > high_level and (bullish_pin(curr) or curr["close"] > curr["open"]) and ctf in ["BULLISH", "NEUTRAL"] and btf in ["BULLISH", "NEUTRAL"]
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 20
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({"model": "BREAKOUT_RETEST_REJECTION", "direction": "LONG", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": f"Retest/rejection of breakout level {high_level:.2f}"})

    bear = ALLOW_SHORTS and recent_bear_break and curr["high"] >= low_level - atr * PARAMS["retest_buffer_atr"] and curr["close"] < low_level and (bearish_pin(curr) or curr["close"] < curr["open"]) and ctf in ["BEARISH", "NEUTRAL"] and btf in ["BEARISH", "NEUTRAL"]
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 20
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({"model": "BREAKOUT_RETEST_REJECTION", "direction": "SHORT", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": f"Retest/rejection of breakdown level {low_level:.2f}"})

    return out


def detect_liquidity_sweep_reversal(df, i):
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])
    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    out = []

    bull = liquidity_sweep_low(df, i, PARAMS["sweep_lookback"]) and (bullish_pin(curr) or bullish_engulf(prev, curr)) and ctf != "BEARISH"
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 14
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({"model": "LIQUIDITY_SWEEP_REVERSAL", "direction": "LONG", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": "Downside liquidity sweep and reversal candle"})

    bear = ALLOW_SHORTS and liquidity_sweep_high(df, i, PARAMS["sweep_lookback"]) and (bearish_pin(curr) or bearish_engulf(prev, curr)) and ctf != "BULLISH"
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 14
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({"model": "LIQUIDITY_SWEEP_REVERSAL", "direction": "SHORT", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": "Upside liquidity sweep and reversal candle"})

    return out


def detect_range_rejection(df, i):
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])
    if i - PARAMS["range_window"] < 2:
        return []
    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    out = []

    range_high = float(df["high"].iloc[i - PARAMS["range_window"]:i].max())
    range_low = float(df["low"].iloc[i - PARAMS["range_window"]:i].min())

    bull = curr["low"] <= range_low and curr["close"] > range_low and bullish_pin(curr)
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 10
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({"model": "RANGE_REJECTION", "direction": "LONG", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": f"Range low rejection near {range_low:.2f}"})

    bear = ALLOW_SHORTS and curr["high"] >= range_high and curr["close"] < range_high and bearish_pin(curr)
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 10
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({"model": "RANGE_REJECTION", "direction": "SHORT", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": f"Range high rejection near {range_high:.2f}"})

    return out


def detect_compression_breakout(df, i):
    curr = df.iloc[i]
    prev = df.iloc[i - 1]
    next_open = float(df.iloc[i + 1]["open"])
    if i - PARAMS["compression_window"] < 2:
        return []
    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    out = []

    recent_width = recent_range_width(df, i, PARAMS["compression_window"])
    if recent_width is None:
        return out

    compressed = recent_width <= float(curr["atr"]) * 2.8
    high_level = breakout_level_high(df, i, PARAMS["compression_window"])
    low_level = breakout_level_low(df, i, PARAMS["compression_window"])

    bull = compressed and curr["close"] > high_level and curr["close"] >= prev["high"] and ctf in ["BULLISH", "NEUTRAL"] and btf in ["BULLISH", "NEUTRAL"]
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 15
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({"model": "COMPRESSION_BREAKOUT", "direction": "LONG", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": "Compression resolved upward"})

    bear = ALLOW_SHORTS and compressed and curr["close"] < low_level and curr["close"] <= prev["low"] and ctf in ["BEARISH", "NEUTRAL"] and btf in ["BEARISH", "NEUTRAL"]
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 15
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({"model": "COMPRESSION_BREAKOUT", "direction": "SHORT", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": "Compression resolved downward"})

    return out


def detect_ema_reclaim(df, i):
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    next_open = float(df.iloc[i + 1]["open"])
    ltf, ctf, btf = entry_trend(curr), confirm_trend(curr), bias_trend(curr)
    out = []

    bull = prev["close"] < prev["ema20"] and curr["close"] > curr["ema20"] and curr["close"] >= curr["open"] and ctf in ["BULLISH", "NEUTRAL"] and btf in ["BULLISH", "NEUTRAL"] and float(curr["rsi"]) >= PARAMS["rsi_bull"]
    if bull:
        score = trend_score(ltf, ctf, btf, "LONG") + momentum_score(curr, "LONG") + 9
        stop, target = risk_levels(curr, next_open, "LONG")
        out.append({"model": "EMA_RECLAIM", "direction": "LONG", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": "Price reclaimed EMA20 in trend context"})

    bear = ALLOW_SHORTS and prev["close"] > prev["ema20"] and curr["close"] < curr["ema20"] and curr["close"] <= curr["open"] and ctf in ["BEARISH", "NEUTRAL"] and btf in ["BEARISH", "NEUTRAL"] and float(curr["rsi"]) <= PARAMS["rsi_bear"]
    if bear:
        score = trend_score(ltf, ctf, btf, "SHORT") + momentum_score(curr, "SHORT") + 9
        stop, target = risk_levels(curr, next_open, "SHORT")
        out.append({"model": "EMA_RECLAIM", "direction": "SHORT", "score": round(score, 1), "entry": round(next_open, 2), "stop": stop, "target": target, "reason": "Price lost EMA20 in bearish context"})

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


def scan_setup_market(market, setup):
    df = build_mtf_frame(market, setup)
    if df.empty or len(df) < 260:
        return []

    i = len(df) - 2
    row = df.iloc[i]
    regime = detect_market_regime(df, i, row)

    print(
        f"{market} {setup['name']} | "
        f"{setup['entry_tf']}/{setup['confirm_tf']}/{setup['bias_tf']} | "
        f"LTF={entry_trend(row)} CTF={confirm_trend(row)} BTF={bias_trend(row)} | "
        f"RSI={row['rsi']:.2f} ADX={row['adx']:.2f} | "
        f"REGIME={regime['name']}"
    )

    candidates = []
    for fn in ALL_MODELS:
        try:
            candidates.extend(fn(df, i))
        except Exception as e:
            print(f"{market} {setup['name']} {fn.__name__} error:", e)

    out = []
    for c in candidates:
        c["market"] = market
        c["setup_name"] = setup["name"]
        c["entry_tf"] = setup["entry_tf"]
        c["confirm_tf"] = setup["confirm_tf"]
        c["bias_tf"] = setup["bias_tf"]
        c["time"] = str(df.iloc[i]["timestamp"])
        c["rr"] = round(abs(c["target"] - c["entry"]) / max(abs(c["entry"] - c["stop"]), 1e-9), 2)
        c = edge_adjustment(c, row)
        c = apply_regime_adjustment(c, regime)

        if c.get("blocked_by_regime", False):
            print(
                f"{market} {setup['name']} {c['model']} {c['direction']} "
                f"blocked_by_regime={c.get('regime')} score={c['score']}"
            )
            continue

        out.append(c)
        print(
            f"{market} {setup['name']} {c['model']} {c['direction']} "
            f"score={c['score']} edge={c.get('edge_note', 'none')} "
            f"regime={c.get('regime', 'none')} reg_adj={c.get('regime_adjustment', 0)}"
        )

    return out


def dedupe_candidates(candidates):
    """
    Keep the best version of the same idea.

    This stops fast + intraday from both alerting the same GOLD short,
    while still allowing different markets or opposite directions.
    """
    kept = []
    seen = set()

    for c in sorted(candidates, key=lambda x: x["score"], reverse=True):
        market = c["market"]
        direction = c["direction"]

        # Bucket entry so tiny price differences do not create duplicate alerts.
        entry = float(c["entry"])
        if market == "BTC":
            entry_bucket = round(entry / 50) * 50
        elif market == "GOLD":
            entry_bucket = round(entry / 2) * 2
        else:
            entry_bucket = round(entry, 1)

        key = (market, direction, entry_bucket)

        if key in seen:
            continue

        # Scores can exceed 100 after regime boost. Cap display at 100.
        c["score"] = min(round(float(c["score"]), 1), 100.0)

        kept.append(c)
        seen.add(key)

    return kept

def format_signal(signal):
    market = signal["market"]
    direction = signal["direction"]
    model = signal["model"]

    market_emoji = E_GOLD if market == "GOLD" else E_BTC
    side_emoji = E_LONG if direction == "LONG" else E_SHORT

    model_emojis = {
        "BREAKOUT_CONTINUATION": E_BOOM,
        "PULLBACK_CONTINUATION": E_TARGET,
        "BREAKOUT_RETEST_REJECTION": E_RETEST,
        "LIQUIDITY_SWEEP_REVERSAL": E_WATER,
        "RANGE_REJECTION": E_BOX,
        "COMPRESSION_BREAKOUT": E_ZAP,
        "EMA_RECLAIM": E_CHART_UP,
    }

    model_emoji = model_emojis.get(model, E_CHART)

    return (
        f"{E_ALERT} {market_emoji} {market} {side_emoji} SIGNAL {E_ALERT}\n\n"
        f"{model_emoji} Model: {model}\n"
        f"Direction: {direction}\n"
        f"Score: {signal['score']}/100\n\n"
        f"{E_TARGET} Entry: {signal['entry']}\n"
        f"{E_STOP} Stop Loss: {signal['stop']}\n"
        f"{E_MONEY} Take Profit: {signal['target']}\n"
        f"{E_SCALE} R:R: {signal['rr']}\n\n"
        f"{E_CLOCK} Setup: {signal['setup_name']} ({signal['entry_tf']} / {signal['confirm_tf']} / {signal['bias_tf']})\n"
        f"{E_BRAIN} Reason: {signal['reason']}\n"
        f"{E_CHART} Edge: {signal.get('edge_note', 'no_profile')}\n"
        f"{E_WEATHER} Regime: {signal.get('regime', 'unknown')}\n"
        f"{E_PLUS} Regime adj: {signal.get('regime_adjustment', 0)}\n"
        f"{E_TIME} Time: {signal['time']}"
    )

def maybe_send_signal(signal):
    sent = load_json(SENT_SIGNAL_FILE, {})
    now_ts = time.time()

    market = signal["market"]
    direction = signal["direction"]
    setup = signal["setup_name"]
    model = signal["model"]
    signal_time = signal["time"]

    # Exact candle duplicate block.
    exact_key = f"exact:{market}:{setup}:{model}:{direction}:{signal_time}"
    if sent.get(exact_key):
        print("Duplicate exact signal blocked:", exact_key)
        return

    # Cooldown block.
    # Stops repeated GOLD SHORT / BTC LONG spam while the same move continues.
    if SAME_DIRECTION_COOLDOWN:
        cooldown_key = f"cooldown:{market}:{direction}"
        last = sent.get(cooldown_key)

        if isinstance(last, dict):
            elapsed = now_ts - float(last.get("ts", 0))
            if elapsed < SIGNAL_COOLDOWN_MINUTES * 60:
                remaining = int((SIGNAL_COOLDOWN_MINUTES * 60 - elapsed) / 60) + 1
                print(
                    f"Cooldown blocked {market} {direction}. "
                    f"Last alert {elapsed/60:.1f} min ago. Remaining ~{remaining} min."
                )
                return

    send_telegram(format_signal(signal))

    sent[exact_key] = True
    sent[f"cooldown:{market}:{direction}"] = {
        "ts": now_ts,
        "entry": signal["entry"],
        "model": model,
        "setup": setup,
        "time": signal_time,
    }

    save_json(SENT_SIGNAL_FILE, sent)


# ============================================================
# SIGNAL TRADE RESULT TRACKER
# ============================================================

def utc_day_key():
    return pd.Timestamp.utcnow().strftime("%Y-%m-%d")


def empty_daily_stats():
    return {
        "signals": 0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "total_r": 0.0,
        "total_points": 0.0,
        "cash_pnl": 0.0,
    }


def load_trade_state():
    state = load_json(TRADE_STATE_FILE, {})
    today = utc_day_key()

    if not isinstance(state, dict) or not state:
        state = {
            "date": today,
            "open_trades": [],
            "closed_trades": [],
            "daily": empty_daily_stats(),
        }

    state.setdefault("open_trades", [])
    state.setdefault("closed_trades", [])
    state.setdefault("daily", empty_daily_stats())

    if state.get("date") != today:
        # Keep open trades across days, but reset the daily scoreboard.
        state["date"] = today
        state["daily"] = empty_daily_stats()

    return state


def save_trade_state(state):
    # Keep history from growing forever.
    state["closed_trades"] = state.get("closed_trades", [])[-300:]
    save_json(TRADE_STATE_FILE, state)


def trade_id_from_signal(signal):
    return (
        f"{signal['market']}|{signal['setup_name']}|{signal['model']}|"
        f"{signal['direction']}|{signal['time']}|{signal['entry']}"
    )


def format_daily_scoreboard(daily):
    wins = int(daily.get("wins", 0))
    losses = int(daily.get("losses", 0))
    signals = int(daily.get("signals", 0))
    total_r = float(daily.get("total_r", 0.0))
    points = float(daily.get("total_points", 0.0))
    cash = float(daily.get("cash_pnl", 0.0))
    return (
        f"{E_BOOK} Today: {wins}W / {losses}L | Signals: {signals}\n"
        f"{E_CHART} P/L: {total_r:+.2f}R | {points:+.2f} pts | ${cash:+.2f}"
    )


def register_signal_trade(signal):
    if not TRACK_SIGNAL_RESULTS:
        return

    state = load_trade_state()
    tid = trade_id_from_signal(signal)

    for t in state.get("open_trades", []):
        if t.get("id") == tid:
            return

    for t in state.get("closed_trades", []):
        if t.get("id") == tid:
            return

    if len(state.get("open_trades", [])) >= MAX_OPEN_SIGNAL_TRADES:
        print("Max open signal trades reached; signal not tracked:", tid)
        return

    trade = {
        "id": tid,
        "status": "OPEN",
        "market": signal["market"],
        "model": signal["model"],
        "setup_name": signal["setup_name"],
        "entry_tf": signal["entry_tf"],
        "direction": signal["direction"],
        "entry": float(signal["entry"]),
        "stop": float(signal["stop"]),
        "target": float(signal["target"]),
        "rr": float(signal.get("rr", 0)),
        "opened_at": str(signal["time"]),
        "created_at_utc": pd.Timestamp.utcnow().isoformat(),
        "reason": signal.get("reason", ""),
        "score": float(signal.get("score", 0)),
    }

    state["open_trades"].append(trade)
    state["daily"]["signals"] = int(state["daily"].get("signals", 0)) + 1
    save_trade_state(state)


def get_signal_monitor_df(market):
    # 5m is used so TP/SL detection is faster even for 15m or 1h alerts.
    df = fetch_market_tf(market, TP_SL_CHECK_TIMEFRAME)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values("timestamp").reset_index(drop=True)


def evaluate_trade_exit(trade, df):
    opened_at = pd.to_datetime(trade["opened_at"], utc=True)
    after = df[df["timestamp"] > opened_at].copy()

    if after.empty:
        return None

    direction = trade["direction"]
    entry = float(trade["entry"])
    stop = float(trade["stop"])
    target = float(trade["target"])

    for _, candle in after.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        ts = str(candle["timestamp"])

        if direction == "LONG":
            tp_hit = high >= target
            sl_hit = low <= stop

            if tp_hit and sl_hit:
                if CONSERVATIVE_SAME_CANDLE_EXIT:
                    return {"result": "SL", "exit_price": stop, "exit_time": ts, "same_candle": True}
                return {"result": "TP", "exit_price": target, "exit_time": ts, "same_candle": True}

            if sl_hit:
                return {"result": "SL", "exit_price": stop, "exit_time": ts, "same_candle": False}
            if tp_hit:
                return {"result": "TP", "exit_price": target, "exit_time": ts, "same_candle": False}

        else:
            tp_hit = low <= target
            sl_hit = high >= stop

            if tp_hit and sl_hit:
                if CONSERVATIVE_SAME_CANDLE_EXIT:
                    return {"result": "SL", "exit_price": stop, "exit_time": ts, "same_candle": True}
                return {"result": "TP", "exit_price": target, "exit_time": ts, "same_candle": True}

            if sl_hit:
                return {"result": "SL", "exit_price": stop, "exit_time": ts, "same_candle": False}
            if tp_hit:
                return {"result": "TP", "exit_price": target, "exit_time": ts, "same_candle": False}

    return None


def close_trade_result(trade, exit_info):
    direction = trade["direction"]
    entry = float(trade["entry"])
    stop = float(trade["stop"])
    target = float(trade["target"])
    rr = float(trade.get("rr", 0))

    if exit_info["result"] == "TP":
        if rr <= 0:
            if direction == "LONG":
                rr = abs(target - entry) / max(abs(entry - stop), 1e-9)
            else:
                rr = abs(entry - target) / max(abs(stop - entry), 1e-9)
        r_mult = rr
        if direction == "LONG":
            points = target - entry
        else:
            points = entry - target
    else:
        r_mult = -1.0
        if direction == "LONG":
            points = stop - entry
        else:
            points = entry - stop

    cash_pnl = r_mult * SIGNAL_RISK_CASH

    closed = dict(trade)
    closed.update({
        "status": exit_info["result"],
        "exit_price": float(exit_info["exit_price"]),
        "exit_time": exit_info["exit_time"],
        "r_multiple": float(r_mult),
        "points_pnl": float(points),
        "cash_pnl": float(cash_pnl),
        "same_candle": bool(exit_info.get("same_candle", False)),
    })
    return closed


def format_trade_close_message(closed, daily):
    win = closed["status"] == "TP"
    icon = E_CHECK if win else E_CROSS
    title = "TP HIT" if win else "SL HIT"
    direction_icon = E_LONG if closed["direction"] == "LONG" else E_SHORT
    same_candle_note = "\nâ ï¸ TP and SL touched in same candle. Counted conservatively." if closed.get("same_candle") else ""

    return (
        f"{icon} {closed['market']} {direction_icon} {title}\n\n"
        f"Model: {closed['model']}\n"
        f"Direction: {closed['direction']}\n"
        f"Entry: {closed['entry']:.2f}\n"
        f"Exit: {closed['exit_price']:.2f}\n"
        f"SL: {closed['stop']:.2f}\n"
        f"TP: {closed['target']:.2f}\n\n"
        f"{E_BAG} Result: {closed['r_multiple']:+.2f}R | "
        f"{closed['points_pnl']:+.2f} pts | ${closed['cash_pnl']:+.2f}"
        f"{same_candle_note}\n\n"
        f"{format_daily_scoreboard(daily)}\n"
        f"{E_TIME} Closed: {closed['exit_time']}"
    )


def check_signal_trade_results():
    if not TRACK_SIGNAL_RESULTS:
        return

    state = load_trade_state()
    open_trades = state.get("open_trades", [])

    if not open_trades:
        return

    dfs = {}
    still_open = []
    closed_now = []

    for trade in open_trades:
        market = trade["market"]
        try:
            if market not in dfs:
                dfs[market] = get_signal_monitor_df(market)

            df = dfs.get(market, pd.DataFrame())
            if df.empty:
                still_open.append(trade)
                continue

            exit_info = evaluate_trade_exit(trade, df)
            if not exit_info:
                still_open.append(trade)
                continue

            closed = close_trade_result(trade, exit_info)
            closed_now.append(closed)

        except Exception as e:
            print(f"Trade tracking error for {market}:", e)
            still_open.append(trade)

    if not closed_now:
        return

    daily = state.get("daily", empty_daily_stats())

    for closed in closed_now:
        if closed["status"] == "TP":
            daily["wins"] = int(daily.get("wins", 0)) + 1
        elif closed["status"] == "SL":
            daily["losses"] = int(daily.get("losses", 0)) + 1
        else:
            daily["breakeven"] = int(daily.get("breakeven", 0)) + 1

        daily["total_r"] = float(daily.get("total_r", 0.0)) + float(closed["r_multiple"])
        daily["total_points"] = float(daily.get("total_points", 0.0)) + float(closed["points_pnl"])
        daily["cash_pnl"] = float(daily.get("cash_pnl", 0.0)) + float(closed["cash_pnl"])

        state["closed_trades"].append(closed)
        send_telegram(format_trade_close_message(closed, daily))

    state["daily"] = daily
    state["open_trades"] = still_open
    save_trade_state(state)


# ============================================================
# MAIN
# ============================================================

def run():
    enabled_setups = [s for s in SETUPS if s.get("enabled", True)]
    if not enabled_setups:
        raise ValueError("No setups enabled.")

    send_telegram(
        "CRYPTO/GOLD MERGED LIVE BOT - LOOSER VERSION STARTED\n\n"
        "Models:\n"
        "- breakout continuation\n"
        "- pullback continuation\n"
        "- breakout retest rejection\n"
        "- liquidity sweep reversal\n"
        "- range rejection\n"
        "- compression breakout\n"
        "- EMA reclaim\n\n"
        "Soft edge weighting only.\n"
        "Market regime detection enabled.\n"
        "Swing disabled by default.\n\n"
        "Enabled setups:\n" +
        "\n".join(f"- {s['name']}: {s['entry_tf']} / {s['confirm_tf']} / {s['bias_tf']}" for s in enabled_setups) +
        f"\n\nMin score to alert: {MIN_SCORE_TO_ALERT}\nSignals per cycle: {TOP_SIGNALS_TO_SEND}\nCooldown: {SIGNAL_COOLDOWN_MINUTES} min\nTP/SL tracking: {TRACK_SIGNAL_RESULTS}\nRisk cash per signal: ${SIGNAL_RISK_CASH:.2f}"
    )

    while True:
        try:
            FETCH_CACHE.clear()
            check_signal_trade_results()
            all_candidates = []

            for market in MARKETS:
                for setup in enabled_setups:
                    try:
                        all_candidates.extend(scan_setup_market(market, setup))
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
                        print(f"{signal['market']} {signal['setup_name']} {signal['model']} {signal['direction']} score={signal['score']}")
                        maybe_send_signal(signal)

        except Exception as e:
            send_telegram(f"Bot error: {e}")

        time.sleep(LIVE_SCAN_SECONDS)


if __name__ == "__main__":
    run()
