import os
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# ============================================================
# BTC + GOLD FULL STRATEGY BOT
# Stable data base + full SMC strategy layered back on top
#
# BTC:
#   - Binance primary
#   - Coinbase fallback
#   - Yahoo fallback
#
# GOLD:
#   - Yahoo Finance GC=F
#
# Strategy:
#   - NO sniper entries
#   - Multi-timeframe bias: 15m + 5m
#   - 1m entry confirmation
#   - Liquidity sweep
#   - BOS / breakout retest
#   - FVG bounce / rejection
#   - Order block bounce / rejection
#   - Healthy pullbacks only
#   - Anti-chase filter
#   - Same-zone re-entry block
#   - Realistic ATR SL / TP1 / TP2
#   - Break-even and trailing runner
#   - Startup + heartbeat + Telegram alerts
# ============================================================

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

# =========================
# CONFIG
# =========================
CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 1800
COOLDOWN_SECONDS = 1800
DEBUG_MODE = True

MIN_SCORE = 82
FULL_SCORE = 92

USE_SESSION_FILTER = False
LONDON_SESSION_START = 7
LONDON_SESSION_END = 11
NY_SESSION_START = 13
NY_SESSION_END = 17

ASSETS = {
    "BTC": {
        "name": "BTC",
        "binance": "BTCUSDT",
        "yf": "BTC-USD",
    },
    "GOLD": {
        "name": "GOLD",
        "binance": None,
        "yf": "GC=F",
    },
}

CFG = {
    "BTC": {
        "SL_ATR": 4.0,
        "TP1_ATR": 4.0,
        "TP2_ATR": 8.0,
        "BE_ATR": 3.0,
        "TRAIL_START_ATR": 4.5,
        "TRAIL_ATR": 3.2,

        "MIN_VOL": 0.0007,
        "MAX_CHASE": 0.0040,
        "MAX_CANDLE_ATR": 1.15,

        "MIN_PULL_ATR": 0.8,
        "MAX_PULL_ATR": 3.2,

        "BOS_LOOKBACK": 20,
        "SWEEP_LOOKBACK": 20,
        "FVG_LOOKBACK": 50,
        "OB_LOOKBACK": 50,

        "RSI_LONG_MIN": 42,
        "RSI_LONG_MAX": 65,
        "RSI_SHORT_MIN": 35,
        "RSI_SHORT_MAX": 58,

        "SAME_ZONE": 0.0030,
    },
    "GOLD": {
        "SL_ATR": 3.2,
        "TP1_ATR": 3.4,
        "TP2_ATR": 7.0,
        "BE_ATR": 2.6,
        "TRAIL_START_ATR": 4.0,
        "TRAIL_ATR": 2.8,

        "MIN_VOL": 0.00012,
        "MAX_CHASE": 0.0028,
        "MAX_CANDLE_ATR": 1.05,

        "MIN_PULL_ATR": 0.8,
        "MAX_PULL_ATR": 2.8,

        "BOS_LOOKBACK": 20,
        "SWEEP_LOOKBACK": 20,
        "FVG_LOOKBACK": 50,
        "OB_LOOKBACK": 50,

        "RSI_LONG_MIN": 42,
        "RSI_LONG_MAX": 64,
        "RSI_SHORT_MIN": 36,
        "RSI_SHORT_MAX": 58,

        "SAME_ZONE": 0.0020,
    },
}

STATE = {
    asset: {
        "IN_TRADE": False,
        "SIDE": None,
        "ENTRY": 0.0,
        "SL": 0.0,
        "TP1": 0.0,
        "TP2": 0.0,
        "TP1_SENT": False,
        "BE_ACTIVE": False,
        "HIGH": 0.0,
        "LOW": 0.0,
        "LAST_TRADE": 0.0,
        "LAST_HEARTBEAT": 0.0,
        "LAST_ENTRY_PRICE": 0.0,
        "LAST_ENTRY_SIDE": None,
        "LAST_TRAIL_SL": 0.0,
        "LAST_DATA_FAIL": 0.0,
    }
    for asset in ASSETS
}

# ============================================================
# TELEGRAM
# ============================================================
def send(msg: str):
    print(msg)

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("TELEGRAM NOT SET")
        return

    for _ in range(5):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )
            if r.status_code == 200:
                return
            print("TELEGRAM FAIL:", r.status_code, r.text)
        except Exception as e:
            print("TELEGRAM ERROR:", e)
        time.sleep(2)

# ============================================================
# SESSION FILTER
# ============================================================
def in_trading_session() -> bool:
    if not USE_SESSION_FILTER:
        return True

    hour = datetime.now().hour
    london = LONDON_SESSION_START <= hour < LONDON_SESSION_END
    new_york = NY_SESSION_START <= hour < NY_SESSION_END
    return london or new_york

# ============================================================
# DATA
# ============================================================
def normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            return pd.DataFrame()

    if "volume" not in df.columns:
        df["volume"] = 1.0

    df = df[["open", "high", "low", "close", "volume"]].copy()

    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(inplace=True)
    return df.reset_index(drop=True)


def get_binance(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        data = r.json()

        if not isinstance(data, list) or len(data) < 120:
            print("BINANCE EMPTY OR BAD:", data if isinstance(data, dict) else "bad length")
            return pd.DataFrame()

        df = pd.DataFrame(
            data,
            columns=[
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_asset_volume",
                "trades",
                "taker_buy_base",
                "taker_buy_quote",
                "ignore",
            ],
        )

        return normalize(df)

    except Exception as e:
        print("BINANCE ERROR:", e)
        return pd.DataFrame()


def get_coinbase(symbol: str, interval: str) -> pd.DataFrame:
    try:
        granularity = {"1m": 60, "5m": 300, "15m": 900}[interval]

        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={"granularity": granularity},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        data = r.json()

        if not isinstance(data, list) or len(data) < 100:
            print("COINBASE EMPTY OR BAD:", data if isinstance(data, dict) else "bad length")
            return pd.DataFrame()

        df = pd.DataFrame(
            data,
            columns=[
                "time",
                "low",
                "high",
                "open",
                "close",
                "volume",
            ],
        )

        df = df.sort_values("time").reset_index(drop=True)
        return normalize(df)

    except Exception as e:
        print("COINBASE ERROR:", e)
        return pd.DataFrame()


def get_yf(symbol: str, interval: str) -> pd.DataFrame:
    try:
        period = {
            "1m": "7d",
            "5m": "30d",
            "15m": "60d",
        }[interval]

        df = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )

        if df is None or df.empty:
            return pd.DataFrame()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        return normalize(df)

    except Exception as e:
        print("YFINANCE ERROR:", e)
        return pd.DataFrame()


def get_klines(asset: str, interval: str):
    cfg = ASSETS[asset]

    if asset == "BTC":
        df = get_binance(cfg["binance"], interval)
        if not df.empty:
            return df, "BINANCE"

        df = get_coinbase("BTC-USD", interval)
        if not df.empty:
            return df, "COINBASE"

        df = get_yf(cfg["yf"], interval)
        if not df.empty:
            return df, "YFINANCE"

        return pd.DataFrame(), "NO_DATA"

    # GOLD
    df = get_yf(cfg["yf"], interval)
    if not df.empty:
        return df, "YFINANCE"

    return pd.DataFrame(), "NONE"

# ============================================================
# INDICATORS
# ============================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 120:
        return pd.DataFrame()

    out = df.copy()

    out["ema9"] = out["close"].ewm(span=9, adjust=False).mean()
    out["ema21"] = out["close"].ewm(span=21, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean()

    delta = out["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    out["rsi"] = 100 - (100 / (1 + rs))

    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - out["close"].shift()).abs(),
            (out["low"] - out["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    out["atr"] = tr.rolling(14).mean()
    out["vol_ma"] = out["volume"].rolling(20).mean()

    out["body"] = (out["close"] - out["open"]).abs()
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["move"] = (out["close"] - out["close"].shift()).abs()
    out["range"] = out["high"] - out["low"]

    out.dropna(inplace=True)
    return out.reset_index(drop=True)

# ============================================================
# STRUCTURE HELPERS
# ============================================================
def swing_high(df: pd.DataFrame, lookback: int) -> float:
    return float(df["high"].iloc[-lookback - 1:-1].max())


def swing_low(df: pd.DataFrame, lookback: int) -> float:
    return float(df["low"].iloc[-lookback - 1:-1].min())


def market_bias(df5: pd.DataFrame, df15: pd.DataFrame) -> str:
    r5 = df5.iloc[-1]
    r15 = df15.iloc[-1]

    strong_bull = (
        r15["ema9"] > r15["ema21"] > r15["ema50"]
        and r15["close"] > r15["ema50"]
        and r5["ema9"] > r5["ema21"]
        and r5["close"] > r5["ema50"]
    )

    strong_bear = (
        r15["ema9"] < r15["ema21"] < r15["ema50"]
        and r15["close"] < r15["ema50"]
        and r5["ema9"] < r5["ema21"]
        and r5["close"] < r5["ema50"]
    )

    weak_bull = r15["ema9"] > r15["ema21"] and r5["close"] > r5["ema21"]
    weak_bear = r15["ema9"] < r15["ema21"] and r5["close"] < r5["ema21"]

    if strong_bull:
        return "STRONG_BULL"
    if strong_bear:
        return "STRONG_BEAR"
    if weak_bull:
        return "BULL"
    if weak_bear:
        return "BEAR"
    return "CHOPPY"


def liquidity_sweep_low(df: pd.DataFrame, cfg: dict) -> bool:
    r = df.iloc[-1]
    prev_low = swing_low(df, cfg["SWEEP_LOOKBACK"])

    swept = r["low"] < prev_low
    closed_back_above = r["close"] > prev_low
    rejection = r["lower_wick"] > r["body"] * 0.8

    return bool(swept and closed_back_above and rejection)


def liquidity_sweep_high(df: pd.DataFrame, cfg: dict) -> bool:
    r = df.iloc[-1]
    prev_high = swing_high(df, cfg["SWEEP_LOOKBACK"])

    swept = r["high"] > prev_high
    closed_back_below = r["close"] < prev_high
    rejection = r["upper_wick"] > r["body"] * 0.8

    return bool(swept and closed_back_below and rejection)


def bos_long(df: pd.DataFrame, cfg: dict) -> bool:
    r = df.iloc[-1]
    high = swing_high(df, cfg["BOS_LOOKBACK"])
    return bool(r["close"] > high and r["close"] > r["ema9"])


def bos_short(df: pd.DataFrame, cfg: dict) -> bool:
    r = df.iloc[-1]
    low = swing_low(df, cfg["BOS_LOOKBACK"])
    return bool(r["close"] < low and r["close"] < r["ema9"])


def breakout_retest_long(df: pd.DataFrame, cfg: dict) -> bool:
    r = df.iloc[-1]
    p = df.iloc[-2]
    level = swing_high(df.iloc[:-1], cfg["BOS_LOOKBACK"])

    broke_previous = p["close"] > level
    retested = r["low"] <= level and r["close"] > level
    bullish_candle = r["close"] > r["open"]

    return bool(broke_previous and retested and bullish_candle)


def breakout_retest_short(df: pd.DataFrame, cfg: dict) -> bool:
    r = df.iloc[-1]
    p = df.iloc[-2]
    level = swing_low(df.iloc[:-1], cfg["BOS_LOOKBACK"])

    broke_previous = p["close"] < level
    retested = r["high"] >= level and r["close"] < level
    bearish_candle = r["close"] < r["open"]

    return bool(broke_previous and retested and bearish_candle)

# ============================================================
# FVG
# ============================================================
def find_bullish_fvg(df: pd.DataFrame, lookback: int):
    start = max(2, len(df) - lookback)

    for i in range(len(df) - 2, start, -1):
        left = df.iloc[i - 2]
        right = df.iloc[i]

        if right["low"] > left["high"]:
            return {
                "low": float(left["high"]),
                "high": float(right["low"]),
                "index": i,
            }

    return None


def find_bearish_fvg(df: pd.DataFrame, lookback: int):
    start = max(2, len(df) - lookback)

    for i in range(len(df) - 2, start, -1):
        left = df.iloc[i - 2]
        right = df.iloc[i]

        if right["high"] < left["low"]:
            return {
                "low": float(right["high"]),
                "high": float(left["low"]),
                "index": i,
            }

    return None


def fvg_bounce_long(df: pd.DataFrame, cfg: dict) -> bool:
    zone = find_bullish_fvg(df, cfg["FVG_LOOKBACK"])
    if not zone:
        return False

    r = df.iloc[-1]

    touched = r["low"] <= zone["high"] and r["close"] >= zone["low"]
    rejection = r["close"] > r["open"] and r["lower_wick"] >= r["body"] * 0.5
    reclaim = r["close"] > r["ema9"]

    return bool(touched and rejection and reclaim)


def fvg_reject_short(df: pd.DataFrame, cfg: dict) -> bool:
    zone = find_bearish_fvg(df, cfg["FVG_LOOKBACK"])
    if not zone:
        return False

    r = df.iloc[-1]

    touched = r["high"] >= zone["low"] and r["close"] <= zone["high"]
    rejection = r["close"] < r["open"] and r["upper_wick"] >= r["body"] * 0.5
    reject = r["close"] < r["ema9"]

    return bool(touched and rejection and reject)

# ============================================================
# ORDER BLOCKS
# ============================================================
def find_bullish_ob(df: pd.DataFrame, cfg: dict):
    start = max(3, len(df) - cfg["OB_LOOKBACK"])

    for i in range(len(df) - 3, start, -1):
        candle = df.iloc[i]
        n1 = df.iloc[i + 1]
        n2 = df.iloc[i + 2]

        bearish_candle = candle["close"] < candle["open"]
        displacement_up = (
            n1["close"] > n1["open"]
            and n2["close"] > n2["open"]
            and n2["close"] > candle["high"]
        )

        if bearish_candle and displacement_up:
            return {
                "low": float(candle["low"]),
                "high": float(candle["high"]),
                "index": i,
            }

    return None


def find_bearish_ob(df: pd.DataFrame, cfg: dict):
    start = max(3, len(df) - cfg["OB_LOOKBACK"])

    for i in range(len(df) - 3, start, -1):
        candle = df.iloc[i]
        n1 = df.iloc[i + 1]
        n2 = df.iloc[i + 2]

        bullish_candle = candle["close"] > candle["open"]
        displacement_down = (
            n1["close"] < n1["open"]
            and n2["close"] < n2["open"]
            and n2["close"] < candle["low"]
        )

        if bullish_candle and displacement_down:
            return {
                "low": float(candle["low"]),
                "high": float(candle["high"]),
                "index": i,
            }

    return None


def ob_bounce_long(df: pd.DataFrame, cfg: dict) -> bool:
    zone = find_bullish_ob(df, cfg)
    if not zone:
        return False

    r = df.iloc[-1]

    touched = r["low"] <= zone["high"] and r["close"] >= zone["low"]
    rejection = r["close"] > r["open"] and r["lower_wick"] >= r["body"] * 0.6
    reclaim = r["close"] > r["ema9"]

    return bool(touched and rejection and reclaim)


def ob_reject_short(df: pd.DataFrame, cfg: dict) -> bool:
    zone = find_bearish_ob(df, cfg)
    if not zone:
        return False

    r = df.iloc[-1]

    touched = r["high"] >= zone["low"] and r["close"] <= zone["high"]
    rejection = r["close"] < r["open"] and r["upper_wick"] >= r["body"] * 0.6
    reject = r["close"] < r["ema9"]

    return bool(touched and rejection and reject)

# ============================================================
# CANDLE CONFIRMATION / PULLBACK
# ============================================================
def bullish_confirmation_candle(df: pd.DataFrame) -> bool:
    r = df.iloc[-1]
    p = df.iloc[-2]

    engulf = r["close"] > r["open"] and r["close"] > p["open"] and r["open"] < p["close"]
    pin = r["lower_wick"] > r["body"] * 1.5 and r["close"] > r["open"]
    reclaim_ema = r["close"] > r["ema9"]

    return bool((engulf or pin) and reclaim_ema)


def bearish_confirmation_candle(df: pd.DataFrame) -> bool:
    r = df.iloc[-1]
    p = df.iloc[-2]

    engulf = r["close"] < r["open"] and r["close"] < p["open"] and r["open"] > p["close"]
    pin = r["upper_wick"] > r["body"] * 1.5 and r["close"] < r["open"]
    reject_ema = r["close"] < r["ema9"]

    return bool((engulf or pin) and reject_ema)


def healthy_pullback_long(asset: str, df1: pd.DataFrame, df5: pd.DataFrame) -> bool:
    cfg = CFG[asset]
    r = df1.iloc[-1]

    pull_depth = swing_high(df1, 6) - r["low"]

    if r["atr"] <= 0:
        return False

    depth_atr = pull_depth / r["atr"]
    good_depth = cfg["MIN_PULL_ATR"] <= depth_atr <= cfg["MAX_PULL_ATR"]
    near_value = r["low"] <= r["ema21"] or r["low"] <= df5.iloc[-1]["ema9"]
    confirmed = r["close"] > r["ema9"] and r["close"] > r["open"]

    return bool(good_depth and near_value and confirmed)


def healthy_pullback_short(asset: str, df1: pd.DataFrame, df5: pd.DataFrame) -> bool:
    cfg = CFG[asset]
    r = df1.iloc[-1]

    pull_depth = r["high"] - swing_low(df1, 6)

    if r["atr"] <= 0:
        return False

    depth_atr = pull_depth / r["atr"]
    good_depth = cfg["MIN_PULL_ATR"] <= depth_atr <= cfg["MAX_PULL_ATR"]
    near_value = r["high"] >= r["ema21"] or r["high"] >= df5.iloc[-1]["ema9"]
    confirmed = r["close"] < r["ema9"] and r["close"] < r["open"]

    return bool(good_depth and near_value and confirmed)

# ============================================================
# RISK FILTERS
# ============================================================
def not_chasing(asset: str, df: pd.DataFrame, side: str) -> bool:
    cfg = CFG[asset]
    r = df.iloc[-1]

    if r["atr"] <= 0:
        return False

    ema_distance = abs(r["close"] - r["ema9"]) / max(r["close"], 1.0)
    if ema_distance > cfg["MAX_CHASE"]:
        return False

    if r["move"] > r["atr"] * cfg["MAX_CANDLE_ATR"]:
        return False

    if side == "LONG":
        if not (cfg["RSI_LONG_MIN"] <= r["rsi"] <= cfg["RSI_LONG_MAX"]):
            return False

    if side == "SHORT":
        if not (cfg["RSI_SHORT_MIN"] <= r["rsi"] <= cfg["RSI_SHORT_MAX"]):
            return False

    return True


def same_zone_block(asset: str, side: str, price: float) -> bool:
    state = STATE[asset]
    cfg = CFG[asset]

    last = state["LAST_ENTRY_PRICE"]
    last_side = state["LAST_ENTRY_SIDE"]

    if last <= 0 or last_side != side:
        return False

    distance = abs(price - last) / max(price, 1.0)
    return distance < cfg["SAME_ZONE"]

# ============================================================
# SCORING
# ============================================================
def score_long(asset: str, df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame):
    cfg = CFG[asset]
    b = market_bias(df5, df15)
    score = 0
    reasons = []

    if b == "STRONG_BULL":
        score += 28
        reasons.append("15m/5m strong bull")
    elif b == "BULL":
        score += 15
        reasons.append("bull bias")
    elif b in ["BEAR", "STRONG_BEAR"]:
        score -= 35
        reasons.append("against HTF")

    checks = [
        (liquidity_sweep_low(df1, cfg), 20, "liquidity sweep low"),
        (bos_long(df1, cfg), 18, "BOS up"),
        (breakout_retest_long(df1, cfg), 24, "breakout retest"),
        (fvg_bounce_long(df1, cfg), 24, "FVG bounce"),
        (ob_bounce_long(df1, cfg), 24, "OB bounce"),
        (healthy_pullback_long(asset, df1, df5), 22, "healthy pullback"),
        (bullish_confirmation_candle(df1), 14, "bull candle confirm"),
    ]

    for ok, pts, reason in checks:
        if ok:
            score += pts
            reasons.append(reason)

    if not not_chasing(asset, df1, "LONG"):
        score -= 35
        reasons.append("chase blocked")

    return max(0, min(int(score), 100)), reasons, b


def score_short(asset: str, df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame):
    cfg = CFG[asset]
    b = market_bias(df5, df15)
    score = 0
    reasons = []

    if b == "STRONG_BEAR":
        score += 28
        reasons.append("15m/5m strong bear")
    elif b == "BEAR":
        score += 15
        reasons.append("bear bias")
    elif b in ["BULL", "STRONG_BULL"]:
        score -= 35
        reasons.append("against HTF")

    checks = [
        (liquidity_sweep_high(df1, cfg), 20, "liquidity sweep high"),
        (bos_short(df1, cfg), 18, "BOS down"),
        (breakout_retest_short(df1, cfg), 24, "breakdown retest"),
        (fvg_reject_short(df1, cfg), 24, "FVG reject"),
        (ob_reject_short(df1, cfg), 24, "OB reject"),
        (healthy_pullback_short(asset, df1, df5), 22, "healthy pullback"),
        (bearish_confirmation_candle(df1), 14, "bear candle confirm"),
    ]

    for ok, pts, reason in checks:
        if ok:
            score += pts
            reasons.append(reason)

    if not not_chasing(asset, df1, "SHORT"):
        score -= 35
        reasons.append("chase blocked")

    return max(0, min(int(score), 100)), reasons, b


def confidence(score: int) -> str:
    if score >= 96:
        return "S"
    if score >= 92:
        return "A+"
    if score >= 86:
        return "A"
    if score >= 82:
        return "B+"
    return "B"

# ============================================================
# SIGNAL
# ============================================================
def maybe_alert_data_fail(asset: str, feed: str):
    state = STATE[asset]
    now = time.time()

    if feed in ["NONE", "NO_DATA"]:
        if now - state["LAST_DATA_FAIL"] > HEARTBEAT_SECONDS:
            send(
                f"â ï¸ {asset} DATA ISSUE\n"
                f"Feed: {feed}\n"
                f"All available feeds failed."
            )
            state["LAST_DATA_FAIL"] = now


def get_signal(asset: str):
    df1_raw, src1 = get_klines(asset, "1m")
    df5_raw, src5 = get_klines(asset, "5m")
    df15_raw, src15 = get_klines(asset, "15m")

    df1 = add_indicators(df1_raw)
    df5 = add_indicators(df5_raw)
    df15 = add_indicators(df15_raw)

    sources = [s for s in [src1, src5, src15] if s not in ["NONE", "NO_DATA"]]
    feed = "/".join(sorted(set(sources))) if sources else src1

    maybe_alert_data_fail(asset, feed)

    if df1.empty or df5.empty or df15.empty:
        return None, feed

    r = df1.iloc[-1]
    price = float(r["close"])
    atr = float(r["atr"])

    if atr <= 0:
        return None, feed

    if (atr / max(price, 1.0)) < CFG[asset]["MIN_VOL"]:
        return None, feed

    long_score, long_reasons, b = score_long(asset, df1, df5, df15)
    short_score, short_reasons, _ = score_short(asset, df1, df5, df15)

    return {
        "asset": asset,
        "price": price,
        "atr": atr,
        "df1": df1,
        "df5": df5,
        "df15": df15,
        "bias": b,
        "long_score": long_score,
        "short_score": short_score,
        "long_reasons": long_reasons,
        "short_reasons": short_reasons,
        "feed": feed,
    }, feed

# ============================================================
# HEARTBEAT
# ============================================================
def heartbeat(asset: str, sig, feed: str):
    state = STATE[asset]

    if time.time() - state["LAST_HEARTBEAT"] < HEARTBEAT_SECONDS:
        return

    if sig is None:
        send(
            f"ð {asset} HEARTBEAT\n\n"
            f"Status: NO DATA / WAITING\n"
            f"In trade: {'YES' if state['IN_TRADE'] else 'NO'}\n"
            f"Feed: {feed}"
        )
    else:
        r = sig["df1"].iloc[-1]
        send(
            f"ð {asset} HEARTBEAT\n\n"
            f"Price: ${sig['price']:.2f}\n"
            f"RSI: {float(r['rsi']):.1f}\n"
            f"Bias: {sig['bias']}\n"
            f"Long: {sig['long_score']} | Short: {sig['short_score']}\n"
            f"In trade: {'YES' if state['IN_TRADE'] else 'NO'}\n"
            f"Feed: {sig['feed']}"
        )

    state["LAST_HEARTBEAT"] = time.time()

# ============================================================
# TRADE MANAGEMENT
# ============================================================
def start_trade(asset: str, side: str, sig: dict):
    state = STATE[asset]
    cfg = CFG[asset]

    price = sig["price"]
    atr = sig["atr"]

    state["IN_TRADE"] = True
    state["SIDE"] = side
    state["ENTRY"] = price
    state["HIGH"] = price
    state["LOW"] = price
    state["TP1_SENT"] = False
    state["BE_ACTIVE"] = False
    state["LAST_ENTRY_PRICE"] = price
    state["LAST_ENTRY_SIDE"] = side
    state["LAST_TRAIL_SL"] = 0.0

    if side == "LONG":
        score = sig["long_score"]
        reasons = sig["long_reasons"]
        state["SL"] = price - atr * cfg["SL_ATR"]
        state["TP1"] = price + atr * cfg["TP1_ATR"]
        state["TP2"] = price + atr * cfg["TP2_ATR"]
        icon = "ð"
    else:
        score = sig["short_score"]
        reasons = sig["short_reasons"]
        state["SL"] = price + atr * cfg["SL_ATR"]
        state["TP1"] = price - atr * cfg["TP1_ATR"]
        state["TP2"] = price - atr * cfg["TP2_ATR"]
        icon = "ð"

    send(
        f"{icon} {asset} {side} ENTRY\n\n"
        f"Style: FULL SMC STRATEGY\n"
        f"Size: {'FULL' if score >= FULL_SCORE else 'STANDARD'}\n"
        f"Confidence: {confidence(score)}\n"
        f"Price: ${price:.2f}\n"
        f"Score: {score}\n"
        f"Reasons: {', '.join(reasons[:6])}\n\n"
        f"SL: ${state['SL']:.2f}\n"
        f"TP1: ${state['TP1']:.2f}\n"
        f"TP2: ${state['TP2']:.2f}"
    )


def reset_trade(asset: str):
    state = STATE[asset]

    state["IN_TRADE"] = False
    state["SIDE"] = None
    state["ENTRY"] = 0.0
    state["SL"] = 0.0
    state["TP1"] = 0.0
    state["TP2"] = 0.0
    state["TP1_SENT"] = False
    state["BE_ACTIVE"] = False
    state["HIGH"] = 0.0
    state["LOW"] = 0.0
    state["LAST_TRADE"] = time.time()
    state["LAST_TRAIL_SL"] = 0.0


def manage_trade(asset: str, sig: dict):
    state = STATE[asset]
    cfg = CFG[asset]

    price = sig["price"]
    atr = sig["atr"]
    entry = state["ENTRY"]

    if state["SIDE"] == "LONG":
        state["HIGH"] = max(state["HIGH"], price)

        if not state["BE_ACTIVE"] and price >= entry + atr * cfg["BE_ATR"]:
            state["SL"] = max(state["SL"], entry)
            state["BE_ACTIVE"] = True
            send(f"â¡ {asset} LONG BREAK-EVEN\nNew SL: ${state['SL']:.2f}")

        if not state["TP1_SENT"] and price >= state["TP1"]:
            state["TP1_SENT"] = True
            send(f"ð° {asset} LONG TP1 / PARTIAL ZONE\nPrice: ${price:.2f}")

        if price >= entry + atr * cfg["TRAIL_START_ATR"]:
            new_sl = state["HIGH"] - atr * cfg["TRAIL_ATR"]
            if new_sl > state["SL"] and abs(new_sl - state["LAST_TRAIL_SL"]) >= atr * 0.25:
                state["SL"] = new_sl
                state["LAST_TRAIL_SL"] = new_sl
                send(f"ð {asset} LONG TRAILING STOP\nNew SL: ${new_sl:.2f}")

        if price <= state["SL"]:
            send(f"â {asset} LONG STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset)
            return

        if price >= state["TP2"]:
            send(f"ð¯ {asset} LONG TP2 HIT\nExit: ${price:.2f}")
            reset_trade(asset)
            return

    elif state["SIDE"] == "SHORT":
        state["LOW"] = min(state["LOW"], price)

        if not state["BE_ACTIVE"] and price <= entry - atr * cfg["BE_ATR"]:
            state["SL"] = min(state["SL"], entry)
            state["BE_ACTIVE"] = True
            send(f"â¡ {asset} SHORT BREAK-EVEN\nNew SL: ${state['SL']:.2f}")

        if not state["TP1_SENT"] and price <= state["TP1"]:
            state["TP1_SENT"] = True
            send(f"ð° {asset} SHORT TP1 / PARTIAL ZONE\nPrice: ${price:.2f}")

        if price <= entry - atr * cfg["TRAIL_START_ATR"]:
            new_sl = state["LOW"] + atr * cfg["TRAIL_ATR"]
            if new_sl < state["SL"] and abs(new_sl - state["LAST_TRAIL_SL"]) >= atr * 0.25:
                state["SL"] = new_sl
                state["LAST_TRAIL_SL"] = new_sl
                send(f"ð {asset} SHORT TRAILING STOP\nNew SL: ${new_sl:.2f}")

        if price >= state["SL"]:
            send(f"â {asset} SHORT STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset)
            return

        if price <= state["TP2"]:
            send(f"ð¯ {asset} SHORT TP2 HIT\nExit: ${price:.2f}")
            reset_trade(asset)
            return

# ============================================================
# ENTRY DECISION
# ============================================================
def try_enter(asset: str, sig: dict):
    state = STATE[asset]

    if state["IN_TRADE"]:
        return

    if time.time() - state["LAST_TRADE"] < COOLDOWN_SECONDS:
        return

    if not in_trading_session():
        return

    price = sig["price"]

    long_ok = (
        sig["long_score"] >= MIN_SCORE
        and sig["bias"] in ["BULL", "STRONG_BULL"]
        and not_chasing(asset, sig["df1"], "LONG")
        and not same_zone_block(asset, "LONG", price)
    )

    short_ok = (
        sig["short_score"] >= MIN_SCORE
        and sig["bias"] in ["BEAR", "STRONG_BEAR"]
        and not_chasing(asset, sig["df1"], "SHORT")
        and not same_zone_block(asset, "SHORT", price)
    )

    if long_ok and sig["long_score"] >= sig["short_score"]:
        start_trade(asset, "LONG", sig)
        return

    if short_ok and sig["short_score"] > sig["long_score"]:
        start_trade(asset, "SHORT", sig)
        return

# ============================================================
# MAIN LOOP
# ============================================================
def run():
    time.sleep(8)
    send("â BOT STARTING...")
    time.sleep(2)
    send(f"ð¥ BTC + GOLD FULL STRATEGY BOT LIVE ð¥\nTime: {time.strftime('%H:%M:%S')}")

    while True:
        try:
            for asset in ASSETS:
                sig, feed = get_signal(asset)
                heartbeat(asset, sig, feed)

                if DEBUG_MODE:
                    if sig is None:
                        print(asset, "NO DATA / WAITING", feed)
                    else:
                        print(
                            asset,
                            sig["bias"],
                            "L:", sig["long_score"],
                            "S:", sig["short_score"],
                            "FEED:", feed,
                        )

                if sig is None:
                    continue

                if STATE[asset]["IN_TRADE"]:
                    manage_trade(asset, sig)
                    continue

                try_enter(asset, sig)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"ð¨ BOT ERROR:\n{e}")
            time.sleep(10)


if __name__ == "__main__":
    run()
