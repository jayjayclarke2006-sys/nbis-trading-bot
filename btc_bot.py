import os
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# ============================================================
# NBIS BTC + GOLD BOT - OLD 700 STYLE FIXED
# ============================================================
# FIXES IN THIS FILE:
# - BTC heartbeat will show price if ANY BTC data is available.
# - The bot no longer calls BTC "NO DATA" just because score/volatility is low.
# - Binance -> Coinbase -> Yahoo fallback.
# - Gold TwelveData -> Yahoo fallback.
# - Emojis are built with unicode escapes so they do not turn into ð / â.
# - Keeps old style breakout / pullback / sniper / scoring / scale / SL / TP.
# ============================================================

# ============================================================
# SAFE EMOJIS - DO NOT EDIT
# ============================================================
E_CHECK = "\u2705"
E_FIRE = "\U0001F525"
E_ROCKET = "\U0001F680"
E_HEART = "\U0001F493"
E_WARN = "\u26A0\uFE0F"
E_DOWN = "\U0001F4C9"
E_MONEY = "\U0001F4B0"
E_TARGET = "\U0001F3AF"
E_CROSS = "\u274C"
E_ZAP = "\u26A1"
E_UP = "\U0001F4C8"
E_PLUS = "\u2795"

# ============================================================
# ENV
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

# ============================================================
# CONFIG
# ============================================================
CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 300
COOLDOWN_SECONDS = 600
DATA_FAIL_ALERT_COOLDOWN = 1800
DEBUG_MODE = True

LONG_ALERT_SCORE = 72
SHORT_ALERT_SCORE = 72
A_SETUP_SCORE = 86
FULL_SIZE_SCORE = 92

MAX_SCALE_INS = 2
SCALE_IN_COOLDOWN_SECONDS = 120

ASSETS = {
    "BTC": {
        "name": "BTC",
        "yfinance_ticker": "BTC-USD",
        "binance_symbol": "BTCUSDT",
        "coinbase_symbol": "BTC-USD",
        "td_symbol": "BTC/USD",
    },
    "GOLD": {
        "name": "GOLD",
        "yfinance_ticker": "GC=F",
        "binance_symbol": None,
        "coinbase_symbol": None,
        "td_symbol": "XAU/USD",
    },
}

ASSET_CONFIG = {
    "BTC": {
        "ATR_SL_MULT": 3.0,
        "ATR_TP_MULT": 6.0,
        "ATR_TRAIL_MULT": 3.1,
        "BREAK_EVEN_ATR_TRIGGER": 2.2,
        "PARTIAL_ATR_TRIGGER": 2.8,
        "TRAILING_ACTIVATION_ATR": 3.0,
        "TRAIL_UPDATE_MIN_ATR": 0.35,
        "MIN_VOLATILITY_PCT": 0.00015,
        "MAX_EMA9_DISTANCE_PCT": 0.0045,
        "SCALE_IN_ATR_STEP": 1.2,
        "LONG_RSI_MAX": 66.0,
        "SHORT_RSI_MIN": 34.0,
        "MAX_BODY_ATR_MULT": 0.85,
        "BREAKOUT_BUFFER_LONG": 1.0010,
        "BREAKOUT_BUFFER_SHORT": 0.9990,
        "VOL_CONFIRM_MULT": 1.03,
        "PULLBACK_LONG_EMA9_MAX": 1.0010,
        "PULLBACK_SHORT_EMA9_MIN": 0.9990,
        "MAX_IMPULSE_ATR_MULT": 0.95,
        "MIN_RETEST_ATR": 0.35,
        "NO_SHORT_RSI_BELOW": 35.0,
        "NO_LONG_RSI_ABOVE": 65.0,
    },
    "GOLD": {
        "ATR_SL_MULT": 2.4,
        "ATR_TP_MULT": 4.8,
        "ATR_TRAIL_MULT": 2.4,
        "BREAK_EVEN_ATR_TRIGGER": 1.8,
        "PARTIAL_ATR_TRIGGER": 2.4,
        "TRAILING_ACTIVATION_ATR": 2.4,
        "TRAIL_UPDATE_MIN_ATR": 0.25,
        "MIN_VOLATILITY_PCT": 0.00008,
        "MAX_EMA9_DISTANCE_PCT": 0.0032,
        "SCALE_IN_ATR_STEP": 0.8,
        "LONG_RSI_MAX": 64.0,
        "SHORT_RSI_MIN": 36.0,
        "MAX_BODY_ATR_MULT": 0.80,
        "BREAKOUT_BUFFER_LONG": 1.0006,
        "BREAKOUT_BUFFER_SHORT": 0.9994,
        "VOL_CONFIRM_MULT": 1.02,
        "PULLBACK_LONG_EMA9_MAX": 1.0008,
        "PULLBACK_SHORT_EMA9_MIN": 0.9992,
        "MAX_IMPULSE_ATR_MULT": 0.85,
        "MIN_RETEST_ATR": 0.30,
        "NO_SHORT_RSI_BELOW": 37.0,
        "NO_LONG_RSI_ABOVE": 63.0,
    },
}

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
    }
    for key in ASSETS
}

# ============================================================
# TELEGRAM
# ============================================================
def send(msg: str):
    print(msg)
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("TELEGRAM NOT SET")
        return

    for _ in range(3):
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
# DATA FEEDS
# ============================================================
def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.columns = [str(c).lower() for c in df.columns]

    if "volume" not in df.columns:
        df["volume"] = 1.0

    needed = ["open", "high", "low", "close", "volume"]
    for c in needed:
        if c not in df.columns:
            return pd.DataFrame()

    df = df[needed].copy()

    for c in needed:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df.dropna(inplace=True)
    return df.reset_index(drop=True)


def td_interval(interval: str) -> str:
    return {"1m": "1min", "5m": "5min", "15m": "15min"}[interval]


def get_binance_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )

        if r.status_code != 200:
            print("BINANCE STATUS:", r.status_code, r.text[:250])
            return pd.DataFrame()

        data = r.json()

        if not isinstance(data, list) or len(data) < 25:
            print("BINANCE BAD DATA:", data if isinstance(data, dict) else "bad length")
            return pd.DataFrame()

        df = pd.DataFrame(
            data,
            columns=[
                "time", "open", "high", "low", "close", "volume",
                "ct", "qav", "trades", "tbv", "tqv", "ignore"
            ],
        )

        return normalize_ohlcv(df)

    except Exception as e:
        print("BINANCE ERROR:", e)
        return pd.DataFrame()


def get_coinbase_klines(symbol: str, interval: str) -> pd.DataFrame:
    try:
        granularity = {"1m": 60, "5m": 300, "15m": 900}[interval]
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={"granularity": granularity},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        if r.status_code != 200:
            print("COINBASE STATUS:", r.status_code, r.text[:250])
            return pd.DataFrame()

        data = r.json()

        if not isinstance(data, list) or len(data) < 25:
            print("COINBASE BAD DATA:", data if isinstance(data, dict) else "bad length")
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=["time", "low", "high", "open", "close", "volume"])
        df = df.sort_values("time").reset_index(drop=True)
        return normalize_ohlcv(df)

    except Exception as e:
        print("COINBASE ERROR:", e)
        return pd.DataFrame()


def get_twelvedata_klines(td_symbol: str, interval: str, outputsize: int = 500) -> pd.DataFrame:
    try:
        if not TWELVEDATA_API_KEY or not td_symbol:
            return pd.DataFrame()

        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": td_symbol,
                "interval": td_interval(interval),
                "outputsize": outputsize,
                "apikey": TWELVEDATA_API_KEY,
                "format": "JSON",
            },
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        if r.status_code != 200:
            print("TWELVEDATA STATUS:", r.status_code, r.text[:250])
            return pd.DataFrame()

        data = r.json()

        if not isinstance(data, dict) or "values" not in data:
            print("TWELVEDATA BAD DATA:", data)
            return pd.DataFrame()

        values = data["values"]
        if not isinstance(values, list) or len(values) < 25:
            return pd.DataFrame()

        df = pd.DataFrame(values)
        df = df.iloc[::-1].reset_index(drop=True)
        return normalize_ohlcv(df)

    except Exception as e:
        print("TWELVEDATA ERROR:", e)
        return pd.DataFrame()


def get_yfinance_klines(ticker: str, interval: str) -> pd.DataFrame:
    try:
        period_map = {"1m": "7d", "5m": "30d", "15m": "60d"}
        df = yf.download(
            ticker,
            period=period_map.get(interval, "30d"),
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        return normalize_ohlcv(df)
    except Exception as e:
        print("YFINANCE ERROR:", e)
        return pd.DataFrame()


def get_klines(asset_key: str, interval: str):
    asset = ASSETS[asset_key]

    if asset_key == "BTC":
        # Old profitable style: Binance first.
        df = get_binance_klines(asset["binance_symbol"], interval)
        if not df.empty:
            return df, "BINANCE"

        # Backup 1.
        df = get_coinbase_klines(asset["coinbase_symbol"], interval)
        if not df.empty:
            return df, "COINBASE"

        # Backup 2.
        df = get_yfinance_klines(asset["yfinance_ticker"], interval)
        if not df.empty:
            return df, "YFINANCE"

        # Backup 3 if user has key.
        df = get_twelvedata_klines(asset["td_symbol"], interval)
        if not df.empty:
            return df, "TWELVEDATA"

        return pd.DataFrame(), "NONE"

    # Gold old style: TwelveData first, Yahoo fallback.
    df = get_twelvedata_klines(asset["td_symbol"], interval)
    if not df.empty:
        return df, "TWELVEDATA"

    df = get_yfinance_klines(asset["yfinance_ticker"], interval)
    if not df.empty:
        return df, "YFINANCE"

    return pd.DataFrame(), "NONE"

# ============================================================
# INDICATORS
# ============================================================
def ema(df: pd.DataFrame, span: int) -> pd.Series:
    return df["close"].ewm(span=span, adjust=False).mean()


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = delta.clip(upper=0).abs().rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 25:
        return pd.DataFrame()

    out = df.copy()
    out["ema9"] = ema(out, 9)
    out["ema21"] = ema(out, 21)
    out["ema50"] = ema(out, 50)
    out["rsi"] = rsi(out, 14)
    out["atr"] = atr(out, 14)
    out["vol_ma"] = out["volume"].rolling(20).mean()
    out["hh10"] = out["high"].rolling(10).max().shift(1)
    out["ll10"] = out["low"].rolling(10).min().shift(1)
    out["body"] = (out["close"] - out["open"]).abs()
    out.dropna(inplace=True)
    return out.reset_index(drop=True)

# ============================================================
# TREND / FILTERS
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
    return (atr_now / max(price, 1.0)) >= ASSET_CONFIG[asset_key]["MIN_VOLATILITY_PCT"]


def not_too_extended(asset_key: str, price: float, ema9_value: float) -> bool:
    distance = abs(price - ema9_value) / max(price, 1.0)
    return distance <= ASSET_CONFIG[asset_key]["MAX_EMA9_DISTANCE_PCT"]


def clean_entry(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    move_size = abs(float(r["close"]) - float(p["close"]))

    if float(r["atr"]) > 0 and move_size > float(r["atr"]) * cfg["MAX_IMPULSE_ATR_MULT"]:
        return False

    if float(r["atr"]) > 0 and float(r["body"]) > float(r["atr"]) * cfg["MAX_BODY_ATR_MULT"]:
        return False

    return True



def pro_entry_guard(asset_key: str, sig: dict, side: str) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    df1 = sig["df1"]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    if side == "LONG":
        if sig["trend"] != "BULLISH" or sig["htf_bias"] not in ["BULL", "STRONG_BULL"]:
            return False
        if float(r["rsi"]) >= cfg["NO_LONG_RSI_ABOVE"]:
            return False

    if side == "SHORT":
        if sig["trend"] != "BEARISH" or sig["htf_bias"] not in ["BEAR", "STRONG_BEAR"]:
            return False
        if float(r["rsi"]) <= cfg["NO_SHORT_RSI_BELOW"]:
            return False

    if float(r["atr"]) > 0:
        if float(r["body"]) > float(r["atr"]) * cfg["MAX_BODY_ATR_MULT"]:
            return False
        if abs(float(r["close"]) - float(p["close"])) > float(r["atr"]) * cfg["MAX_IMPULSE_ATR_MULT"]:
            return False

    if not not_too_extended(asset_key, float(r["close"]), float(r["ema9"])):
        return False

    return True

def long_not_chasing(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]

    if float(r["rsi"]) > cfg["LONG_RSI_MAX"]:
        return False

    return not_too_extended(asset_key, float(r["close"]), float(r["ema9"]))


def short_not_chasing(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]

    if float(r["rsi"]) < cfg["SHORT_RSI_MIN"]:
        return False

    return not_too_extended(asset_key, float(r["close"]), float(r["ema9"]))

# ============================================================
# ENTRY TYPES
# ============================================================
def breakout_long(df1: pd.DataFrame, asset_key: str) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    cfg = ASSET_CONFIG[asset_key]

    return bool(
        r["close"] > r["hh10"] * cfg["BREAKOUT_BUFFER_LONG"]
        and r["close"] > p["close"]
        and r["volume"] >= r["vol_ma"] * cfg["VOL_CONFIRM_MULT"]
        and r["close"] > r["ema9"]
    )


def breakout_short(df1: pd.DataFrame, asset_key: str) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    cfg = ASSET_CONFIG[asset_key]

    return bool(
        r["close"] < r["ll10"] * cfg["BREAKOUT_BUFFER_SHORT"]
        and r["close"] < p["close"]
        and r["volume"] >= r["vol_ma"] * cfg["VOL_CONFIRM_MULT"]
        and r["close"] < r["ema9"]
    )


def pullback_long(df1: pd.DataFrame, asset_key: str) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    if float(r["atr"]) <= 0:
        return False

    touched_value = float(r["low"]) <= float(r["ema9"]) or float(r["low"]) <= float(r["ema21"])
    reclaimed = float(r["close"]) > float(r["ema9"]) and float(r["close"]) > float(r["open"])
    not_impulse = float(r["body"]) <= float(r["atr"]) * cfg["MAX_BODY_ATR_MULT"]
    actually_pulled_back = abs(float(p["close"]) - float(r["low"])) >= float(r["atr"]) * cfg["MIN_RETEST_ATR"]

    return bool(touched_value and reclaimed and not_impulse and actually_pulled_back)


def pullback_short(df1: pd.DataFrame, asset_key: str) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    if float(r["atr"]) <= 0:
        return False

    touched_value = float(r["high"]) >= float(r["ema9"]) or float(r["high"]) >= float(r["ema21"])
    rejected = float(r["close"]) < float(r["ema9"]) and float(r["close"]) < float(r["open"])
    not_impulse = float(r["body"]) <= float(r["atr"]) * cfg["MAX_BODY_ATR_MULT"]
    actually_pulled_back = abs(float(r["high"]) - float(p["close"])) >= float(r["atr"]) * cfg["MIN_RETEST_ATR"]

    return bool(touched_value and rejected and not_impulse and actually_pulled_back)


def sniper_long(df1: pd.DataFrame) -> bool:
    # PRO MODE: sniper entries disabled to stop late/choppy entries.
    return False


def sniper_short(df1: pd.DataFrame) -> bool:
    # PRO MODE: sniper entries disabled to stop late/choppy entries.
    return False


def confirm_long(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    return bool(r["close"] > p["high"] and r["close"] > r["ema9"])


def confirm_short(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    return bool(r["close"] < p["low"] and r["close"] < r["ema9"])

# ============================================================
# SCORING
# ============================================================
def long_score(df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame) -> int:
    r = df1.iloc[-1]
    score = 0

    local_trend = market_trend(df1, df5)
    htf = higher_timeframe_bias(df15)

    if local_trend == "BULLISH":
        score += 25

    if htf == "STRONG_BULL":
        score += 25
    elif htf == "BULL":
        score += 15
    elif htf in ["BEAR", "STRONG_BEAR"]:
        score -= 20

    if r["ema9"] > r["ema21"]:
        score += 15

    if 50 < r["rsi"] < 68:
        score += 15
    elif 48 < r["rsi"] < 72:
        score += 8

    if r["volume"] >= r["vol_ma"]:
        score += 8

    if r["close"] > r["ema9"]:
        score += 10

    if breakout_long(df1, "BTC" if r["close"] > 10000 else "GOLD"):
        score += 8

    return max(0, min(int(score), 100))


def short_score(df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame) -> int:
    r = df1.iloc[-1]
    score = 0

    local_trend = market_trend(df1, df5)
    htf = higher_timeframe_bias(df15)

    if local_trend == "BEARISH":
        score += 25

    if htf == "STRONG_BEAR":
        score += 25
    elif htf == "BEAR":
        score += 15
    elif htf in ["BULL", "STRONG_BULL"]:
        score -= 20

    if r["ema9"] < r["ema21"]:
        score += 15

    if 32 < r["rsi"] < 50:
        score += 15
    elif 28 < r["rsi"] < 54:
        score += 8

    if r["volume"] >= r["vol_ma"]:
        score += 8

    if r["close"] < r["ema9"]:
        score += 10

    if breakout_short(df1, "BTC" if r["close"] > 10000 else "GOLD"):
        score += 8

    return max(0, min(int(score), 100))


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
# HEARTBEAT / ALERTS
# ============================================================
def maybe_alert_data_fail(asset_key: str, src1: str, src5: str, src15: str):
    state = STATE[asset_key]
    now = time.time()

    if src1 == "NONE" and src5 == "NONE" and src15 == "NONE":
        if now - state["LAST_DATA_FAIL_ALERT_TS"] >= DATA_FAIL_ALERT_COOLDOWN:
            send(
                f"{E_WARN} {ASSETS[asset_key]['name']} DATA FEED FAIL\n"
                f"1m: {src1}\n"
                f"5m: {src5}\n"
                f"15m: {src15}"
            )
            state["LAST_DATA_FAIL_ALERT_TS"] = now


def heartbeat(asset_key: str, sig):
    now = time.time()
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]

    if now - state["LAST_HEARTBEAT_TS"] < HEARTBEAT_SECONDS:
        return

    if sig is None:
        msg = (
            f"{E_HEART} {name} HEARTBEAT\n\n"
            f"Status: NO DATA\n"
            f"In trade: {'YES' if state['IN_TRADE'] else 'NO'}\n"
            f"Feed: {state['DATA_SOURCE']}"
        )
        send(msg)
        state["LAST_HEARTBEAT_TS"] = now
        return

    send(
        f"{E_HEART} {name} HEARTBEAT\n\n"
        f"Price: ${sig['price']:.2f}\n"
        f"RSI: {float(sig['df1'].iloc[-1]['rsi']):.1f}\n"
        f"Trend: {sig['trend']}\n"
        f"HTF Bias: {sig['htf_bias']}\n"
        f"Long: {sig['long_score']} | Short: {sig['short_score']}\n"
        f"In trade: {'YES' if state['IN_TRADE'] else 'NO'}\n"
        f"Feed: {sig['data_source']}"
    )

    state["LAST_HEARTBEAT_TS"] = now

# ============================================================
# SIGNAL ENGINE
# ============================================================
def get_signal(asset_key: str):
    df1_raw, src1 = get_klines(asset_key, "1m")
    df5_raw, src5 = get_klines(asset_key, "5m")
    df15_raw, src15 = get_klines(asset_key, "15m")

    maybe_alert_data_fail(asset_key, src1, src5, src15)

    df1 = add_indicators(df1_raw)
    df5 = add_indicators(df5_raw)
    df15 = add_indicators(df15_raw)

    sources = [s for s in [src1, src5, src15] if s != "NONE"]
    data_source = "/".join(sorted(set(sources))) if sources else "NONE"
    STATE[asset_key]["DATA_SOURCE"] = data_source

    if DEBUG_MODE:
        print(asset_key, "RAW:", len(df1_raw), len(df5_raw), len(df15_raw), "IND:", len(df1), len(df5), len(df15), "SRC:", data_source)

    if df1.empty or df5.empty or df15.empty:
        # If 1m has price but higher timeframes fail, do not lie as unknown.
        # Heartbeat still needs full signal, so return None but keep feed.
        return None

    price = float(df1.iloc[-1]["close"])
    atr_now = float(df1.iloc[-1]["atr"])

    if atr_now <= 0:
        return None

    # Old fix: do NOT return None just because volatility is quiet.
    # Low volatility simply means lower chance of entry, not no data.

    ls = long_score(df1, df5, df15)
    ss = short_score(df1, df5, df15)

    return {
        "asset_key": asset_key,
        "price": price,
        "atr": atr_now,
        "df1": df1,
        "df5": df5,
        "df15": df15,
        "trend": market_trend(df1, df5),
        "htf_bias": higher_timeframe_bias(df15),
        "long_score": ls,
        "short_score": ss,
        "long_breakout": breakout_long(df1, asset_key),
        "short_breakout": breakout_short(df1, asset_key),
        "long_pullback": pullback_long(df1, asset_key),
        "short_pullback": pullback_short(df1, asset_key),
        "long_sniper": sniper_long(df1),
        "short_sniper": sniper_short(df1),
        "confirm_long": confirm_long(df1),
        "confirm_short": confirm_short(df1),
        "data_source": data_source,
    }

# ============================================================
# TRADE HELPERS
# ============================================================
def entry_size_label(score: int) -> str:
    return "FULL" if score >= FULL_SIZE_SCORE else "SNIPER"


def reset_trade(asset_key: str):
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


def start_trade(asset_key: str, side: str, trigger: str, score: int, price: float, atr_now: float):
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
        state["STOP_LOSS"] = price - (atr_now * cfg["ATR_SL_MULT"])
        state["TAKE_PROFIT"] = price + (atr_now * cfg["ATR_TP_MULT"])
        icon = E_ROCKET
    else:
        state["STOP_LOSS"] = price + (atr_now * cfg["ATR_SL_MULT"])
        state["TAKE_PROFIT"] = price - (atr_now * cfg["ATR_TP_MULT"])
        icon = E_DOWN

    send(
        f"{icon} {name} {side} ENTRY\n\n"
        f"Trigger: {trigger}\n"
        f"Size: {entry_size_label(score)}\n"
        f"Confidence: {state['CONFIDENCE_LABEL']}\n"
        f"Scale: 1/{MAX_SCALE_INS}\n"
        f"Price: ${price:.2f}\n"
        f"Score: {score}\n\n"
        f"SL: ${state['STOP_LOSS']:.2f}\n"
        f"TP: ${state['TAKE_PROFIT']:.2f}"
    )


def can_scale_in(asset_key: str) -> bool:
    state = STATE[asset_key]
    if not state["IN_TRADE"]:
        return False
    if state["SCALE_COUNT"] >= MAX_SCALE_INS:
        return False
    if time.time() - state["LAST_SCALE_TIME"] < SCALE_IN_COOLDOWN_SECONDS:
        return False
    return True


def maybe_scale_in(asset_key: str, sig: dict):
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]
    cfg = ASSET_CONFIG[asset_key]

    if not can_scale_in(asset_key):
        return

    price = sig["price"]
    atr_now = sig["atr"]

    if state["TRADE_SIDE"] == "LONG":
        favorable_move = price >= state["AVG_ENTRY_PRICE"] + (atr_now * cfg["SCALE_IN_ATR_STEP"])
        if favorable_move and sig["confirm_long"] and sig["long_score"] >= LONG_ALERT_SCORE and long_not_chasing(asset_key, sig["df1"]) and pro_entry_guard(asset_key, sig, "LONG"):
            old_avg = state["AVG_ENTRY_PRICE"]
            state["AVG_ENTRY_PRICE"] = (state["AVG_ENTRY_PRICE"] * state["SCALE_COUNT"] + price) / (state["SCALE_COUNT"] + 1)
            state["SCALE_COUNT"] += 1
            state["LAST_SCALE_TIME"] = time.time()
            state["HIGHEST_PRICE"] = max(state["HIGHEST_PRICE"], price)
            send(
                f"{E_PLUS} {name} LONG SCALE-IN\n\n"
                f"Entry type: {state['ENTRY_TYPE']}\n"
                f"New add price: ${price:.2f}\n"
                f"Old avg: ${old_avg:.2f}\n"
                f"New avg: ${state['AVG_ENTRY_PRICE']:.2f}\n"
                f"Scale: {state['SCALE_COUNT']}/{MAX_SCALE_INS}\n"
                f"Confidence: {state['CONFIDENCE_LABEL']}"
            )

    elif state["TRADE_SIDE"] == "SHORT":
        favorable_move = price <= state["AVG_ENTRY_PRICE"] - (atr_now * cfg["SCALE_IN_ATR_STEP"])
        if favorable_move and sig["confirm_short"] and sig["short_score"] >= SHORT_ALERT_SCORE and short_not_chasing(asset_key, sig["df1"]) and pro_entry_guard(asset_key, sig, "SHORT"):
            old_avg = state["AVG_ENTRY_PRICE"]
            state["AVG_ENTRY_PRICE"] = (state["AVG_ENTRY_PRICE"] * state["SCALE_COUNT"] + price) / (state["SCALE_COUNT"] + 1)
            state["SCALE_COUNT"] += 1
            state["LAST_SCALE_TIME"] = time.time()
            state["LOWEST_PRICE"] = min(state["LOWEST_PRICE"], price)
            send(
                f"{E_PLUS} {name} SHORT SCALE-IN\n\n"
                f"Entry type: {state['ENTRY_TYPE']}\n"
                f"New add price: ${price:.2f}\n"
                f"Old avg: ${old_avg:.2f}\n"
                f"New avg: ${state['AVG_ENTRY_PRICE']:.2f}\n"
                f"Scale: {state['SCALE_COUNT']}/{MAX_SCALE_INS}\n"
                f"Confidence: {state['CONFIDENCE_LABEL']}"
            )


def maybe_send_trailing_update(asset_key: str, new_sl: float, atr_now: float):
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]
    min_step = atr_now * ASSET_CONFIG[asset_key]["TRAIL_UPDATE_MIN_ATR"]

    if state["LAST_TRAIL_SENT_SL"] == 0.0 or abs(new_sl - state["LAST_TRAIL_SENT_SL"]) >= min_step:
        state["LAST_TRAIL_SENT_SL"] = new_sl
        side = "LONG" if state["TRADE_SIDE"] == "LONG" else "SHORT"
        icon = E_UP if side == "LONG" else E_DOWN
        send(f"{icon} {name} {side} TRAILING STOP\nNew SL: ${new_sl:.2f}")

# ============================================================
# TRADE MANAGEMENT
# ============================================================
def manage_trade(asset_key: str, sig: dict):
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]
    cfg = ASSET_CONFIG[asset_key]

    price = sig["price"]
    atr_now = sig["atr"]

    maybe_scale_in(asset_key, sig)

    entry_ref = state["AVG_ENTRY_PRICE"] if state["AVG_ENTRY_PRICE"] > 0 else state["ENTRY_PRICE"]

    if state["TRADE_SIDE"] == "LONG":
        state["HIGHEST_PRICE"] = max(state["HIGHEST_PRICE"], price)

        if (not state["BREAK_EVEN_ACTIVE"]) and price >= entry_ref + (atr_now * cfg["BREAK_EVEN_ATR_TRIGGER"]):
            state["STOP_LOSS"] = max(state["STOP_LOSS"], entry_ref)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"{E_ZAP} {name} LONG BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if (not state["PARTIAL_SENT"]) and price >= entry_ref + (atr_now * cfg["PARTIAL_ATR_TRIGGER"]):
            state["PARTIAL_SENT"] = True
            send(f"{E_MONEY} {name} LONG PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if state["BREAK_EVEN_ACTIVE"] and price > entry_ref + (atr_now * cfg["TRAILING_ACTIVATION_ATR"]):
            new_sl = state["HIGHEST_PRICE"] - (atr_now * cfg["ATR_TRAIL_MULT"])
            if new_sl > state["STOP_LOSS"]:
                state["STOP_LOSS"] = new_sl
                maybe_send_trailing_update(asset_key, new_sl, atr_now)

        if price <= state["STOP_LOSS"]:
            send(f"{E_CROSS} {name} LONG STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price >= state["TAKE_PROFIT"]:
            send(f"{E_TARGET} {name} LONG TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

    elif state["TRADE_SIDE"] == "SHORT":
        state["LOWEST_PRICE"] = min(state["LOWEST_PRICE"], price)

        if (not state["BREAK_EVEN_ACTIVE"]) and price <= entry_ref - (atr_now * cfg["BREAK_EVEN_ATR_TRIGGER"]):
            state["STOP_LOSS"] = min(state["STOP_LOSS"], entry_ref)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"{E_ZAP} {name} SHORT BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if (not state["PARTIAL_SENT"]) and price <= entry_ref - (atr_now * cfg["PARTIAL_ATR_TRIGGER"]):
            state["PARTIAL_SENT"] = True
            send(f"{E_MONEY} {name} SHORT PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if state["BREAK_EVEN_ACTIVE"] and price < entry_ref - (atr_now * cfg["TRAILING_ACTIVATION_ATR"]):
            new_sl = state["LOWEST_PRICE"] + (atr_now * cfg["ATR_TRAIL_MULT"])
            if new_sl < state["STOP_LOSS"]:
                state["STOP_LOSS"] = new_sl
                maybe_send_trailing_update(asset_key, new_sl, atr_now)

        if price >= state["STOP_LOSS"]:
            send(f"{E_CROSS} {name} SHORT STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price <= state["TAKE_PROFIT"]:
            send(f"{E_TARGET} {name} SHORT TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

# ============================================================
# MAIN LOOP
# ============================================================
def run():
    time.sleep(5)
    send(f"{E_FIRE} BTC + GOLD PRO BOT LIVE {E_FIRE}\nTime: {time.strftime('%H:%M:%S')}\nMode: PRO FILTERS ON")

    while True:
        try:
            for asset_key in ASSETS:
                sig = get_signal(asset_key)
                heartbeat(asset_key, sig)

                if DEBUG_MODE:
                    if sig is None:
                        print(f"{asset_key} | NO SIGNAL DATA | Feed:{STATE[asset_key]['DATA_SOURCE']}")
                    else:
                        print(
                            f"{asset_key} | Trend: {sig['trend']} | "
                            f"HTF: {sig['htf_bias']} | "
                            f"L:{sig['long_score']} S:{sig['short_score']} | "
                            f"Feed:{sig['data_source']}"
                        )

                if sig is None:
                    continue

                STATE[asset_key]["DATA_SOURCE"] = sig["data_source"]

                if not STATE[asset_key]["IN_TRADE"]:
                    if time.time() - STATE[asset_key]["LAST_TRADE_TIME"] < COOLDOWN_SECONDS:
                        continue

                    if sig["trend"] == "CHOPPY":
                        continue

                    if not clean_entry(asset_key, sig["df1"]):
                        continue

                    if (
                        sig["long_score"] >= A_SETUP_SCORE
                        and sig["confirm_long"]
                        and long_not_chasing(asset_key, sig["df1"])
                        and pro_entry_guard(asset_key, sig, "LONG")
                        and (sig["long_pullback"] or sig["long_breakout"])
                    ):
                        trigger = "A SETUP"
                        if sig["long_breakout"]:
                            trigger = "BREAKOUT"
                        start_trade(
                            asset_key=asset_key,
                            side="LONG",
                            trigger=trigger,
                            score=sig["long_score"],
                            price=sig["price"],
                            atr_now=sig["atr"],
                        )

                    elif (
                        sig["short_score"] >= A_SETUP_SCORE
                        and sig["confirm_short"]
                        and short_not_chasing(asset_key, sig["df1"])
                        and pro_entry_guard(asset_key, sig, "SHORT")
                        and (sig["short_pullback"] or sig["short_breakout"])
                    ):
                        trigger = "A SETUP"
                        if sig["short_breakout"]:
                            trigger = "BREAKOUT"
                        start_trade(
                            asset_key=asset_key,
                            side="SHORT",
                            trigger=trigger,
                            score=sig["short_score"],
                            price=sig["price"],
                            atr_now=sig["atr"],
                        )

                else:
                    manage_trade(asset_key, sig)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"🚨 BOT ERROR:\n{e}")
            time.sleep(15)


if __name__ == "__main__":
    run()
