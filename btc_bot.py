import os
import time
import math
import requests
import pandas as pd
import yfinance as yf

# ============================================================
# BTC + GOLD TELEGRAM SIGNAL BOT
# Full old-style strategy rebuilt clean:
# - BTC + GOLD data
# - Telegram startup + heartbeat
# - Breakout / pullback / sniper / hybrid scoring
# - SL / TP / break-even / trailing / partial alerts
# - Scale-in alerts
# - Data fail alerts
# - 30 minute heartbeats
# ============================================================

# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================
TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
)

CHAT_ID = (
    os.getenv("CHAT_ID")
    or os.getenv("TELEGRAM_CHAT_ID")
)

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

# ============================================================
# GLOBAL CONFIG
# ============================================================
CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 1800
COOLDOWN_SECONDS = 600
DATA_FAIL_ALERT_COOLDOWN = 1800
DEBUG_MODE = True

LONG_ALERT_SCORE = 65
SHORT_ALERT_SCORE = 65
A_SETUP_SCORE = 70
FULL_SIZE_SCORE = 82

MAX_SCALE_INS = 2
SCALE_IN_COOLDOWN_SECONDS = 180

# ============================================================
# ASSETS
# ============================================================
ASSETS = {
    "BTC": {
        "name": "BTC",
        "binance_symbol": "BTCUSDT",
        "td_symbol": "BTC/USD",
        "yf_symbol": "BTC-USD",
    },
    "GOLD": {
        "name": "GOLD",
        "binance_symbol": None,
        "td_symbol": "XAU/USD",
        "yf_symbol": "GC=F",
    },
}

# ============================================================
# PER-ASSET STRATEGY SETTINGS
# ============================================================
ASSET_CONFIG = {
    "BTC": {
        "ATR_SL_MULT": 3.2,
        "ATR_TP_MULT": 7.0,
        "ATR_TRAIL_MULT": 3.0,
        "BREAK_EVEN_ATR_TRIGGER": 2.2,
        "PARTIAL_ATR_TRIGGER": 3.5,
        "TRAILING_ACTIVATION_ATR": 3.0,
        "TRAIL_UPDATE_MIN_ATR": 0.25,
        "MIN_VOLATILITY_PCT": 0.0008,
        "MAX_EMA9_DISTANCE_PCT": 0.0070,
        "SCALE_IN_ATR_STEP": 1.0,
        "LONG_RSI_MAX": 70.0,
        "SHORT_RSI_MIN": 30.0,
        "MAX_BODY_ATR_MULT": 1.15,
        "BREAKOUT_BUFFER_LONG": 1.0010,
        "BREAKOUT_BUFFER_SHORT": 0.9990,
        "VOL_CONFIRM_MULT": 1.00,
        "PULLBACK_LONG_EMA9_MAX": 1.0020,
        "PULLBACK_SHORT_EMA9_MIN": 0.9980,
        "SNIPER_LONG_RSI_RECLAIM": 48.0,
        "SNIPER_SHORT_RSI_REJECT": 52.0,
    },
    "GOLD": {
        "ATR_SL_MULT": 2.6,
        "ATR_TP_MULT": 6.0,
        "ATR_TRAIL_MULT": 2.8,
        "BREAK_EVEN_ATR_TRIGGER": 2.0,
        "PARTIAL_ATR_TRIGGER": 3.0,
        "TRAILING_ACTIVATION_ATR": 2.6,
        "TRAIL_UPDATE_MIN_ATR": 0.18,
        "MIN_VOLATILITY_PCT": 0.00015,
        "MAX_EMA9_DISTANCE_PCT": 0.0048,
        "SCALE_IN_ATR_STEP": 0.6,
        "LONG_RSI_MAX": 68.0,
        "SHORT_RSI_MIN": 32.0,
        "MAX_BODY_ATR_MULT": 0.95,
        "BREAKOUT_BUFFER_LONG": 1.0005,
        "BREAKOUT_BUFFER_SHORT": 0.9995,
        "VOL_CONFIRM_MULT": 1.00,
        "PULLBACK_LONG_EMA9_MAX": 1.0015,
        "PULLBACK_SHORT_EMA9_MIN": 0.9985,
        "SNIPER_LONG_RSI_RECLAIM": 48.0,
        "SNIPER_SHORT_RSI_REJECT": 52.0,
    },
}

# ============================================================
# STATE
# ============================================================
STATE = {
    key: {
        "IN_TRADE": False,
        "TRADE_SIDE": None,
        "ENTRY_PRICE": 0.0,
        "AVG_ENTRY_PRICE": 0.0,
        "STOP_LOSS": 0.0,
        "TAKE_PROFIT": 0.0,
        "PARTIAL_SENT": False,
        "BREAK_EVEN_ACTIVE": False,
        "LAST_HEARTBEAT_TS": 0.0,
        "HIGHEST_PRICE": 0.0,
        "LOWEST_PRICE": 0.0,
        "LAST_TRADE_TIME": 0.0,
        "LAST_SCALE_TIME": 0.0,
        "SCALE_COUNT": 0,
        "ENTRY_TYPE": None,
        "CONFIDENCE_LABEL": None,
        "LAST_TRAIL_SENT_SL": 0.0,
        "DATA_SOURCE": "UNKNOWN",
        "LAST_DATA_FAIL_ALERT_TS": 0.0,
        "LAST_KNOWN_PRICE": None,
        "LAST_KNOWN_RSI": None,
        "LAST_KNOWN_TREND": "UNKNOWN",
        "LAST_KNOWN_LONG_SCORE": None,
        "LAST_KNOWN_SHORT_SCORE": None,
        "LAST_HTF_BIAS": "UNKNOWN",
    }
    for key in ASSETS
}

# ============================================================
# TELEGRAM
# ============================================================
def send(msg: str) -> None:
    print(msg)

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("TELEGRAM NOT SET")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    for attempt in range(5):
        try:
            response = requests.post(
                url,
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )

            if response.status_code == 200:
                return

            print("TELEGRAM FAIL:", response.status_code, response.text)

        except Exception as e:
            print("TELEGRAM ERROR:", e)

        time.sleep(2)

# ============================================================
# DATA HELPERS
# ============================================================
def td_interval(interval: str) -> str:
    mapping = {
        "1m": "1min",
        "5m": "5min",
        "15m": "15min",
    }
    return mapping[interval]


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]

    needed = ["open", "high", "low", "close", "volume"]

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            return pd.DataFrame()

    if "volume" not in df.columns:
        df["volume"] = 1.0

    df = df[needed].copy()

    for col in needed:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)

    return df


def get_binance_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    for _ in range(3):
        try:
            response = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            if response.status_code != 200:
                time.sleep(1)
                continue

            data = response.json()

            if not isinstance(data, list) or len(data) < 50:
                time.sleep(1)
                continue

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

            df = normalize_df(df)

            if len(df) >= 50:
                return df

        except Exception as e:
            if DEBUG_MODE:
                print("BINANCE ERROR:", e)
            time.sleep(1)

    return pd.DataFrame()


def get_twelvedata_klines(symbol: str, interval: str, outputsize: int = 500) -> pd.DataFrame:
    try:
        if not TWELVEDATA_API_KEY or not symbol:
            return pd.DataFrame()

        response = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": symbol,
                "interval": td_interval(interval),
                "apikey": TWELVEDATA_API_KEY,
                "outputsize": outputsize,
                "format": "JSON",
            },
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        data = response.json()

        if not isinstance(data, dict) or "values" not in data:
            return pd.DataFrame()

        values = data["values"]

        if not isinstance(values, list) or len(values) < 50:
            return pd.DataFrame()

        df = pd.DataFrame(values)
        df = df.iloc[::-1].reset_index(drop=True)
        df = normalize_df(df)

        if len(df) >= 50:
            return df

    except Exception as e:
        if DEBUG_MODE:
            print("TWELVEDATA ERROR:", e)

    return pd.DataFrame()


def get_yfinance_klines(symbol: str, interval: str) -> pd.DataFrame:
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

        df = normalize_df(df)

        if len(df) >= 50:
            return df

    except Exception as e:
        if DEBUG_MODE:
            print("YFINANCE ERROR:", e)

    return pd.DataFrame()


def get_coingecko_btc() -> pd.DataFrame:
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": "1"},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        data = response.json()
        prices = data.get("prices", [])

        if len(prices) < 50:
            return pd.DataFrame()

        df = pd.DataFrame(prices, columns=["time", "close"])
        df["open"] = df["close"].shift(1).fillna(df["close"])
        df["high"] = df[["open", "close"]].max(axis=1)
        df["low"] = df[["open", "close"]].min(axis=1)
        df["volume"] = 1.0

        return normalize_df(df)

    except Exception as e:
        if DEBUG_MODE:
            print("COINGECKO ERROR:", e)

    return pd.DataFrame()


def get_klines(asset_key: str, interval: str):
    asset = ASSETS[asset_key]

    if asset_key == "BTC":
        df = get_binance_klines(asset["binance_symbol"], interval)
        if not df.empty:
            return df, "BINANCE"

        df = get_coingecko_btc()
        if not df.empty:
            return df, "COINGECKO"

        df = get_yfinance_klines(asset["yf_symbol"], interval)
        if not df.empty:
            return df, "YFINANCE"

        df = get_twelvedata_klines(asset["td_symbol"], interval)
        if not df.empty:
            return df, "TWELVEDATA"

        return pd.DataFrame(), "NONE"

    if asset_key == "GOLD":
        df = get_twelvedata_klines(asset["td_symbol"], interval)
        if not df.empty:
            return df, "TWELVEDATA"

        df = get_yfinance_klines(asset["yf_symbol"], interval)
        if not df.empty:
            return df, "YFINANCE"

        return pd.DataFrame(), "NONE"

    return pd.DataFrame(), "NONE"

# ============================================================
# INDICATORS
# ============================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 60:
        return pd.DataFrame()

    out = df.copy()

    out["ema9"] = out["close"].ewm(span=9, adjust=False).mean()
    out["ema21"] = out["close"].ewm(span=21, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()

    delta = out["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()

    rs = gain / loss.replace(0, pd.NA)
    out["rsi"] = 100 - (100 / (1 + rs))

    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - out["close"].shift()).abs(),
            (out["low"] - out["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    out["atr"] = true_range.rolling(14).mean()
    out["vol_ma"] = out["volume"].rolling(20).mean()
    out["hh10"] = out["high"].rolling(10).max().shift(1)
    out["ll10"] = out["low"].rolling(10).min().shift(1)
    out["body"] = (out["close"] - out["open"]).abs()

    out.dropna(inplace=True)
    out.reset_index(drop=True, inplace=True)

    return out

# ============================================================
# TREND / MARKET FILTERS
# ============================================================
def market_trend(df1: pd.DataFrame, df5: pd.DataFrame) -> str:
    r1 = df1.iloc[-1]
    r5 = df5.iloc[-1]

    if r5["ema9"] > r5["ema21"] and r1["ema9"] > r1["ema21"]:
        return "BULLISH"

    if r5["ema9"] < r5["ema21"] and r1["ema9"] < r1["ema21"]:
        return "BEARISH"

    return "CHOPPY"


def higher_timeframe_bias(df15: pd.DataFrame) -> str:
    r = df15.iloc[-1]

    if r["ema9"] > r["ema21"] > r["ema50"]:
        return "STRONG_BULL"

    if r["ema9"] < r["ema21"] < r["ema50"]:
        return "STRONG_BEAR"

    if r["ema9"] > r["ema21"]:
        return "BULL"

    if r["ema9"] < r["ema21"]:
        return "BEAR"

    return "NEUTRAL"


def has_enough_volatility(asset_key: str, price: float, atr_now: float) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    return (atr_now / max(price, 1.0)) >= cfg["MIN_VOLATILITY_PCT"]


def not_too_extended(asset_key: str, price: float, ema9_value: float) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    distance = abs(price - ema9_value) / max(price, 1.0)
    return distance <= cfg["MAX_EMA9_DISTANCE_PCT"]


def clean_candle(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]

    if float(r["atr"]) <= 0:
        return False

    if float(r["body"]) > float(r["atr"]) * cfg["MAX_BODY_ATR_MULT"]:
        return False

    return True


def long_not_chasing(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]

    if float(r["rsi"]) > cfg["LONG_RSI_MAX"]:
        return False

    if not not_too_extended(asset_key, float(r["close"]), float(r["ema9"])):
        return False

    return clean_candle(asset_key, df1)


def short_not_chasing(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]

    if float(r["rsi"]) < cfg["SHORT_RSI_MIN"]:
        return False

    if not not_too_extended(asset_key, float(r["close"]), float(r["ema9"])):
        return False

    return clean_candle(asset_key, df1)

# ============================================================
# ENTRY TYPES
# ============================================================
def breakout_long(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    return bool(
        r["close"] > r["hh10"] * cfg["BREAKOUT_BUFFER_LONG"]
        and r["close"] > p["high"]
        and r["close"] > r["ema9"]
        and r["volume"] >= r["vol_ma"] * cfg["VOL_CONFIRM_MULT"]
    )


def breakout_short(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    return bool(
        r["close"] < r["ll10"] * cfg["BREAKOUT_BUFFER_SHORT"]
        and r["close"] < p["low"]
        and r["close"] < r["ema9"]
        and r["volume"] >= r["vol_ma"] * cfg["VOL_CONFIRM_MULT"]
    )


def sniper_long(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    return bool(
        p["close"] < p["ema9"]
        and r["close"] > r["ema9"]
        and p["rsi"] < cfg["SNIPER_LONG_RSI_RECLAIM"]
        and r["rsi"] > 50
        and r["low"] > p2["low"]
    )


def sniper_short(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    return bool(
        p["close"] > p["ema9"]
        and r["close"] < r["ema9"]
        and p["rsi"] > cfg["SNIPER_SHORT_RSI_REJECT"]
        and r["rsi"] < 50
        and r["high"] < p2["high"]
    )


def pullback_long(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    return bool(
        p["close"] <= p["ema9"] * cfg["PULLBACK_LONG_EMA9_MAX"]
        and r["close"] > r["ema9"]
        and r["rsi"] > 50
    )


def pullback_short(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    return bool(
        p["close"] >= p["ema9"] * cfg["PULLBACK_SHORT_EMA9_MIN"]
        and r["close"] < r["ema9"]
        and r["rsi"] < 50
    )


def confirm_long(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    return bool(r["close"] > p["close"] and r["close"] > r["ema9"])


def confirm_short(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    return bool(r["close"] < p["close"] and r["close"] < r["ema9"])

# ============================================================
# SCORING
# ============================================================
def score_long(asset_key: str, df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame):
    r = df1.iloc[-1]
    score = 0
    reasons = []

    trend = market_trend(df1, df5)
    htf = higher_timeframe_bias(df15)

    if trend == "BULLISH":
        score += 25
        reasons.append("1m/5m bullish")

    if htf == "STRONG_BULL":
        score += 25
        reasons.append("15m strong bull")
    elif htf == "BULL":
        score += 15
        reasons.append("15m bull")
    elif htf in ["BEAR", "STRONG_BEAR"]:
        score -= 20
        reasons.append("15m against long")

    if r["ema9"] > r["ema21"]:
        score += 15
        reasons.append("EMA aligned")

    if 50 < r["rsi"] < 68:
        score += 15
        reasons.append("healthy RSI")
    elif 48 < r["rsi"] < 72:
        score += 8
        reasons.append("acceptable RSI")

    if r["volume"] >= r["vol_ma"]:
        score += 8
        reasons.append("volume ok")

    if breakout_long(asset_key, df1):
        score += 10
        reasons.append("breakout")

    if sniper_long(asset_key, df1):
        score += 10
        reasons.append("sniper")

    if pullback_long(asset_key, df1):
        score += 10
        reasons.append("pullback")

    return max(0, min(int(score), 100)), reasons


def score_short(asset_key: str, df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame):
    r = df1.iloc[-1]
    score = 0
    reasons = []

    trend = market_trend(df1, df5)
    htf = higher_timeframe_bias(df15)

    if trend == "BEARISH":
        score += 25
        reasons.append("1m/5m bearish")

    if htf == "STRONG_BEAR":
        score += 25
        reasons.append("15m strong bear")
    elif htf == "BEAR":
        score += 15
        reasons.append("15m bear")
    elif htf in ["BULL", "STRONG_BULL"]:
        score -= 20
        reasons.append("15m against short")

    if r["ema9"] < r["ema21"]:
        score += 15
        reasons.append("EMA aligned")

    if 32 < r["rsi"] < 50:
        score += 15
        reasons.append("healthy short RSI")
    elif 28 < r["rsi"] < 54:
        score += 8
        reasons.append("acceptable short RSI")

    if r["volume"] >= r["vol_ma"]:
        score += 8
        reasons.append("volume ok")

    if breakout_short(asset_key, df1):
        score += 10
        reasons.append("breakdown")

    if sniper_short(asset_key, df1):
        score += 10
        reasons.append("sniper")

    if pullback_short(asset_key, df1):
        score += 10
        reasons.append("pullback")

    return max(0, min(int(score), 100)), reasons


def confidence_grade(score: int) -> str:
    if score >= 95:
        return "S"
    if score >= 85:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 70:
        return "B+"
    if score >= 60:
        return "B"
    return "C"

# ============================================================
# SIGNAL ENGINE
# ============================================================
def maybe_alert_data_fail(asset_key: str, src1: str, src5: str, src15: str) -> None:
    state = STATE[asset_key]
    now = time.time()

    if src1 == "NONE" and src5 == "NONE" and src15 == "NONE":
        if now - state["LAST_DATA_FAIL_ALERT_TS"] >= DATA_FAIL_ALERT_COOLDOWN:
            send(
                f"â ï¸ {ASSETS[asset_key]['name']} DATA FEED FAIL\n"
                f"1m: {src1}\n"
                f"5m: {src5}\n"
                f"15m: {src15}"
            )
            state["LAST_DATA_FAIL_ALERT_TS"] = now


def get_signal(asset_key: str):
    df1_raw, src1 = get_klines(asset_key, "1m")
    df5_raw, src5 = get_klines(asset_key, "5m")
    df15_raw, src15 = get_klines(asset_key, "15m")

    maybe_alert_data_fail(asset_key, src1, src5, src15)

    df1 = add_indicators(df1_raw)
    df5 = add_indicators(df5_raw)
    df15 = add_indicators(df15_raw)

    sources = [s for s in [src1, src5, src15] if s != "NONE"]
    feed = "/".join(sorted(set(sources))) if sources else "NONE"

    if df1.empty or df5.empty or df15.empty:
        return None, feed

    price = float(df1.iloc[-1]["close"])
    atr_now = float(df1.iloc[-1]["atr"])

    if not has_enough_volatility(asset_key, price, atr_now):
        return None, feed

    ls, lr = score_long(asset_key, df1, df5, df15)
    ss, sr = score_short(asset_key, df1, df5, df15)

    return {
        "asset_key": asset_key,
        "price": price,
        "atr": atr_now,
        "df1": df1,
        "df5": df5,
        "df15": df15,
        "trend": market_trend(df1, df5),
        "htf": higher_timeframe_bias(df15),
        "long_score": ls,
        "short_score": ss,
        "long_reasons": lr,
        "short_reasons": sr,
        "long_breakout": breakout_long(asset_key, df1),
        "short_breakout": breakout_short(asset_key, df1),
        "long_sniper": sniper_long(asset_key, df1),
        "short_sniper": sniper_short(asset_key, df1),
        "long_pullback": pullback_long(asset_key, df1),
        "short_pullback": pullback_short(asset_key, df1),
        "confirm_long": confirm_long(df1),
        "confirm_short": confirm_short(df1),
        "feed": feed,
    }, feed

# ============================================================
# HEARTBEAT
# ============================================================
def heartbeat(asset_key: str, sig, feed: str) -> None:
    state = STATE[asset_key]
    now = time.time()
    name = ASSETS[asset_key]["name"]

    if now - state["LAST_HEARTBEAT_TS"] < HEARTBEAT_SECONDS:
        return

    if sig is None:
        send(
            f"ð {name} HEARTBEAT\n\n"
            f"Status: NO DATA / WAITING\n"
            f"In trade: {'YES' if state['IN_TRADE'] else 'NO'}\n"
            f"Feed: {feed}"
        )
    else:
        r = sig["df1"].iloc[-1]
        state["DATA_SOURCE"] = sig["feed"]
        state["LAST_KNOWN_PRICE"] = sig["price"]
        state["LAST_KNOWN_RSI"] = float(r["rsi"])
        state["LAST_KNOWN_TREND"] = sig["trend"]
        state["LAST_KNOWN_LONG_SCORE"] = sig["long_score"]
        state["LAST_KNOWN_SHORT_SCORE"] = sig["short_score"]
        state["LAST_HTF_BIAS"] = sig["htf"]

        send(
            f"ð {name} HEARTBEAT\n\n"
            f"Price: ${sig['price']:.2f}\n"
            f"RSI: {float(r['rsi']):.1f}\n"
            f"Trend: {sig['trend']}\n"
            f"HTF Bias: {sig['htf']}\n"
            f"Long: {sig['long_score']} | Short: {sig['short_score']}\n"
            f"In trade: {'YES' if state['IN_TRADE'] else 'NO'}\n"
            f"Feed: {sig['feed']}"
        )

    state["LAST_HEARTBEAT_TS"] = now

# ============================================================
# TRADE HELPERS
# ============================================================
def entry_size_label(score: int) -> str:
    return "FULL" if score >= FULL_SIZE_SCORE else "SNIPER"


def reset_trade(asset_key: str) -> None:
    state = STATE[asset_key]
    state["IN_TRADE"] = False
    state["TRADE_SIDE"] = None
    state["ENTRY_PRICE"] = 0.0
    state["AVG_ENTRY_PRICE"] = 0.0
    state["STOP_LOSS"] = 0.0
    state["TAKE_PROFIT"] = 0.0
    state["PARTIAL_SENT"] = False
    state["BREAK_EVEN_ACTIVE"] = False
    state["HIGHEST_PRICE"] = 0.0
    state["LOWEST_PRICE"] = 0.0
    state["LAST_TRADE_TIME"] = time.time()
    state["LAST_SCALE_TIME"] = 0.0
    state["SCALE_COUNT"] = 0
    state["ENTRY_TYPE"] = None
    state["CONFIDENCE_LABEL"] = None
    state["LAST_TRAIL_SENT_SL"] = 0.0


def start_trade(asset_key: str, side: str, trigger: str, score: int, reasons: list, price: float, atr_now: float) -> None:
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]
    cfg = ASSET_CONFIG[asset_key]

    state["ENTRY_PRICE"] = price
    state["AVG_ENTRY_PRICE"] = price
    state["HIGHEST_PRICE"] = price
    state["LOWEST_PRICE"] = price
    state["TRADE_SIDE"] = side
    state["IN_TRADE"] = True
    state["SCALE_COUNT"] = 1
    state["LAST_SCALE_TIME"] = time.time()
    state["ENTRY_TYPE"] = trigger
    state["CONFIDENCE_LABEL"] = confidence_grade(score)
    state["LAST_TRAIL_SENT_SL"] = 0.0
    state["PARTIAL_SENT"] = False
    state["BREAK_EVEN_ACTIVE"] = False

    if side == "LONG":
        state["STOP_LOSS"] = price - atr_now * cfg["ATR_SL_MULT"]
        state["TAKE_PROFIT"] = price + atr_now * cfg["ATR_TP_MULT"]
        emoji = "ð"
    else:
        state["STOP_LOSS"] = price + atr_now * cfg["ATR_SL_MULT"]
        state["TAKE_PROFIT"] = price - atr_now * cfg["ATR_TP_MULT"]
        emoji = "ð"

    send(
        f"{emoji} {name} {side} ENTRY\n\n"
        f"Trigger: {trigger}\n"
        f"Size: {entry_size_label(score)}\n"
        f"Confidence: {state['CONFIDENCE_LABEL']}\n"
        f"Scale: 1/{MAX_SCALE_INS}\n"
        f"Price: ${price:.2f}\n"
        f"Score: {score}\n"
        f"Reasons: {', '.join(reasons[:4])}\n\n"
        f"SL: ${state['STOP_LOSS']:.2f}\n"
        f"TP: ${state['TAKE_PROFIT']:.2f}"
    )

# ============================================================
# SCALE-IN / TRAIL / MANAGEMENT
# ============================================================
def maybe_scale_in(asset_key: str, sig: dict) -> None:
    state = STATE[asset_key]
    cfg = ASSET_CONFIG[asset_key]
    name = ASSETS[asset_key]["name"]

    if not state["IN_TRADE"]:
        return

    if state["SCALE_COUNT"] >= MAX_SCALE_INS:
        return

    if time.time() - state["LAST_SCALE_TIME"] < SCALE_IN_COOLDOWN_SECONDS:
        return

    price = sig["price"]
    atr_now = sig["atr"]

    if state["TRADE_SIDE"] == "LONG":
        ok = (
            price >= state["AVG_ENTRY_PRICE"] + atr_now * cfg["SCALE_IN_ATR_STEP"]
            and sig["confirm_long"]
            and sig["long_score"] >= LONG_ALERT_SCORE
            and long_not_chasing(asset_key, sig["df1"])
        )
    else:
        ok = (
            price <= state["AVG_ENTRY_PRICE"] - atr_now * cfg["SCALE_IN_ATR_STEP"]
            and sig["confirm_short"]
            and sig["short_score"] >= SHORT_ALERT_SCORE
            and short_not_chasing(asset_key, sig["df1"])
        )

    if not ok:
        return

    old_avg = state["AVG_ENTRY_PRICE"]
    state["AVG_ENTRY_PRICE"] = (old_avg * state["SCALE_COUNT"] + price) / (state["SCALE_COUNT"] + 1)
    state["SCALE_COUNT"] += 1
    state["LAST_SCALE_TIME"] = time.time()

    send(
        f"â {name} {state['TRADE_SIDE']} SCALE-IN\n\n"
        f"Entry type: {state['ENTRY_TYPE']}\n"
        f"New add price: ${price:.2f}\n"
        f"Old avg: ${old_avg:.2f}\n"
        f"New avg: ${state['AVG_ENTRY_PRICE']:.2f}\n"
        f"Scale: {state['SCALE_COUNT']}/{MAX_SCALE_INS}\n"
        f"Confidence: {state['CONFIDENCE_LABEL']}"
    )


def maybe_send_trailing_update(asset_key: str, new_sl: float, atr_now: float) -> None:
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]
    min_step = atr_now * ASSET_CONFIG[asset_key]["TRAIL_UPDATE_MIN_ATR"]

    if state["LAST_TRAIL_SENT_SL"] == 0.0 or abs(new_sl - state["LAST_TRAIL_SENT_SL"]) >= min_step:
        state["LAST_TRAIL_SENT_SL"] = new_sl
        send(f"ð {name} {state['TRADE_SIDE']} TRAILING STOP\nNew SL: ${new_sl:.2f}")


def manage_trade(asset_key: str, sig: dict) -> None:
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]
    cfg = ASSET_CONFIG[asset_key]

    price = sig["price"]
    atr_now = sig["atr"]

    maybe_scale_in(asset_key, sig)

    entry_ref = state["AVG_ENTRY_PRICE"] if state["AVG_ENTRY_PRICE"] > 0 else state["ENTRY_PRICE"]

    if state["TRADE_SIDE"] == "LONG":
        state["HIGHEST_PRICE"] = max(state["HIGHEST_PRICE"], price)

        if (not state["BREAK_EVEN_ACTIVE"]) and price >= entry_ref + atr_now * cfg["BREAK_EVEN_ATR_TRIGGER"]:
            state["STOP_LOSS"] = max(state["STOP_LOSS"], entry_ref)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"â¡ {name} LONG BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if (not state["PARTIAL_SENT"]) and price >= entry_ref + atr_now * cfg["PARTIAL_ATR_TRIGGER"]:
            state["PARTIAL_SENT"] = True
            send(f"ð° {name} LONG PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if state["BREAK_EVEN_ACTIVE"] and price > entry_ref + atr_now * cfg["TRAILING_ACTIVATION_ATR"]:
            new_sl = state["HIGHEST_PRICE"] - atr_now * cfg["ATR_TRAIL_MULT"]
            if new_sl > state["STOP_LOSS"]:
                state["STOP_LOSS"] = new_sl
                maybe_send_trailing_update(asset_key, new_sl, atr_now)

        if price <= state["STOP_LOSS"]:
            send(f"â {name} LONG STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price >= state["TAKE_PROFIT"]:
            send(f"ð¯ {name} LONG TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

    elif state["TRADE_SIDE"] == "SHORT":
        state["LOWEST_PRICE"] = min(state["LOWEST_PRICE"], price)

        if (not state["BREAK_EVEN_ACTIVE"]) and price <= entry_ref - atr_now * cfg["BREAK_EVEN_ATR_TRIGGER"]:
            state["STOP_LOSS"] = min(state["STOP_LOSS"], entry_ref)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"â¡ {name} SHORT BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if (not state["PARTIAL_SENT"]) and price <= entry_ref - atr_now * cfg["PARTIAL_ATR_TRIGGER"]:
            state["PARTIAL_SENT"] = True
            send(f"ð° {name} SHORT PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if state["BREAK_EVEN_ACTIVE"] and price < entry_ref - atr_now * cfg["TRAILING_ACTIVATION_ATR"]:
            new_sl = state["LOWEST_PRICE"] + atr_now * cfg["ATR_TRAIL_MULT"]
            if new_sl < state["STOP_LOSS"]:
                state["STOP_LOSS"] = new_sl
                maybe_send_trailing_update(asset_key, new_sl, atr_now)

        if price >= state["STOP_LOSS"]:
            send(f"â {name} SHORT STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price <= state["TAKE_PROFIT"]:
            send(f"ð¯ {name} SHORT TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

# ============================================================
# ENTRY DECISION
# ============================================================
def choose_triggers(sig: dict):
    long_trigger = None
    short_trigger = None

    if sig["long_breakout"]:
        long_trigger = "BREAKOUT"
    elif sig["long_sniper"]:
        long_trigger = "SNIPER"
    elif sig["long_pullback"]:
        long_trigger = "PULLBACK"

    if sig["short_breakout"]:
        short_trigger = "BREAKDOWN"
    elif sig["short_sniper"]:
        short_trigger = "SNIPER"
    elif sig["short_pullback"]:
        short_trigger = "PULLBACK"

    return long_trigger, short_trigger


def try_enter_trade(asset_key: str, sig: dict) -> None:
    state = STATE[asset_key]

    if state["IN_TRADE"]:
        return

    if time.time() - state["LAST_TRADE_TIME"] < COOLDOWN_SECONDS:
        return

    if sig["trend"] == "CHOPPY" and sig["htf"] not in ["STRONG_BULL", "STRONG_BEAR"]:
        return

    long_trigger, short_trigger = choose_triggers(sig)

    if (
        long_trigger
        and sig["long_score"] >= A_SETUP_SCORE
        and sig["htf"] in ["BULL", "STRONG_BULL"]
        and sig["confirm_long"]
        and long_not_chasing(asset_key, sig["df1"])
    ):
        start_trade(
            asset_key,
            "LONG",
            long_trigger,
            sig["long_score"],
            sig["long_reasons"],
            sig["price"],
            sig["atr"],
        )
        return

    if (
        short_trigger
        and sig["short_score"] >= A_SETUP_SCORE
        and sig["htf"] in ["BEAR", "STRONG_BEAR"]
        and sig["confirm_short"]
        and short_not_chasing(asset_key, sig["df1"])
    ):
        start_trade(
            asset_key,
            "SHORT",
            short_trigger,
            sig["short_score"],
            sig["short_reasons"],
            sig["price"],
            sig["atr"],
        )
        return

# ============================================================
# MAIN LOOP
# ============================================================
def run() -> None:
    time.sleep(8)
    send("â BOT STARTING...")
    time.sleep(2)
    send(f"ð¥ BTC + GOLD BOT LIVE ð¥\nTime: {time.strftime('%H:%M:%S')}")

    while True:
        try:
            for asset_key in ASSETS:
                sig, feed = get_signal(asset_key)

                heartbeat(asset_key, sig, feed)

                if DEBUG_MODE:
                    if sig is None:
                        print(asset_key, "NO DATA / WAITING", feed)
                    else:
                        print(
                            asset_key,
                            sig["trend"],
                            sig["htf"],
                            "L:", sig["long_score"],
                            "S:", sig["short_score"],
                            "FEED:", feed,
                        )

                if sig is None:
                    continue

                if STATE[asset_key]["IN_TRADE"]:
                    manage_trade(asset_key, sig)
                    continue

                try_enter_trade(asset_key, sig)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"ð¨ BOT ERROR:\n{e}")
            time.sleep(10)


if __name__ == "__main__":
    run()
