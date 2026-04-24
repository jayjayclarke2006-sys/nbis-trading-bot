import os
import time
import requests
import pandas as pd
import yfinance as yf

# ============================================================
# BTC + GOLD FULL STRATEGY BOT
# Built on stable anti-chase core:
# - Telegram live + 30 minute heartbeat
# - BTC Binance primary + Yahoo fallback
# - GOLD Yahoo primary
# - Multi-timeframe 1m / 5m / 15m
# - Breakout / sniper / pullback / hybrid scoring
# - Anti-top-buying filters
# - No same-level re-entry spam
# - SL / TP / break-even / partial / trailing
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
COOLDOWN_SECONDS = 900
DEBUG_MODE = True

A_SETUP_SCORE = 72
FULL_SIZE_SCORE = 85
SCALE_IN_SCORE = 75

MAX_SCALE_INS = 2
SCALE_IN_COOLDOWN_SECONDS = 300

# =========================
# ASSETS
# =========================
ASSETS = {
    "BTC": {
        "name": "BTC",
        "binance_symbol": "BTCUSDT",
        "yf_symbol": "BTC-USD",
    },
    "GOLD": {
        "name": "GOLD",
        "binance_symbol": None,
        "yf_symbol": "GC=F",
    },
}

# =========================
# STRATEGY SETTINGS
# =========================
ASSET_CONFIG = {
    "BTC": {
        "ATR_SL_MULT": 3.2,
        "ATR_TP_MULT": 7.0,
        "ATR_TRAIL_MULT": 3.0,
        "BREAK_EVEN_ATR_TRIGGER": 2.2,
        "PARTIAL_ATR_TRIGGER": 3.5,
        "TRAILING_ACTIVATION_ATR": 3.0,
        "TRAIL_UPDATE_MIN_ATR": 0.35,
        "SCALE_IN_ATR_STEP": 1.2,

        "MIN_VOLATILITY_PCT": 0.0008,
        "MAX_EMA9_DISTANCE_PCT": 0.0048,
        "MAX_BODY_ATR_MULT": 0.95,
        "MAX_ONE_CANDLE_ATR_MULT": 1.10,

        "LONG_RSI_MIN": 48.0,
        "LONG_RSI_MAX": 66.0,
        "SHORT_RSI_MIN": 34.0,
        "SHORT_RSI_MAX": 52.0,

        "BREAKOUT_BUFFER_LONG": 1.0008,
        "BREAKOUT_BUFFER_SHORT": 0.9992,
        "VOL_CONFIRM_MULT": 1.00,

        "PULLBACK_LONG_EMA9_MAX": 1.0015,
        "PULLBACK_SHORT_EMA9_MIN": 0.9985,

        "SAME_ZONE_REENTRY_PCT": 0.0025,
    },
    "GOLD": {
        "ATR_SL_MULT": 2.6,
        "ATR_TP_MULT": 6.0,
        "ATR_TRAIL_MULT": 2.8,
        "BREAK_EVEN_ATR_TRIGGER": 2.0,
        "PARTIAL_ATR_TRIGGER": 3.0,
        "TRAILING_ACTIVATION_ATR": 2.6,
        "TRAIL_UPDATE_MIN_ATR": 0.25,
        "SCALE_IN_ATR_STEP": 0.9,

        "MIN_VOLATILITY_PCT": 0.00012,
        "MAX_EMA9_DISTANCE_PCT": 0.0035,
        "MAX_BODY_ATR_MULT": 0.85,
        "MAX_ONE_CANDLE_ATR_MULT": 1.00,

        "LONG_RSI_MIN": 48.0,
        "LONG_RSI_MAX": 64.0,
        "SHORT_RSI_MIN": 36.0,
        "SHORT_RSI_MAX": 52.0,

        "BREAKOUT_BUFFER_LONG": 1.0004,
        "BREAKOUT_BUFFER_SHORT": 0.9996,
        "VOL_CONFIRM_MULT": 1.00,

        "PULLBACK_LONG_EMA9_MAX": 1.0010,
        "PULLBACK_SHORT_EMA9_MIN": 0.9990,

        "SAME_ZONE_REENTRY_PCT": 0.0018,
    },
}

# =========================
# STATE
# =========================
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
        "LAST_ENTRY_PRICE": 0.0,
        "LAST_ENTRY_SIDE": None,
        "LAST_KNOWN_PRICE": None,
    }
    for key in ASSETS
}

# =========================
# TELEGRAM
# =========================
def send(msg: str):
    print(msg)

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("TELEGRAM NOT SET")
        return

    for _ in range(5):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )
            if response.status_code == 200:
                return
            print("TELEGRAM FAIL:", response.status_code, response.text)
        except Exception as e:
            print("TELEGRAM ERROR:", e)
        time.sleep(2)

# =========================
# DATA
# =========================
def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]

    for c in ["open", "high", "low", "close"]:
        if c not in df.columns:
            return pd.DataFrame()

    if "volume" not in df.columns:
        df["volume"] = 1.0

    df = df[["open", "high", "low", "close", "volume"]].copy()

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df.dropna(inplace=True)
    return df.reset_index(drop=True)


def get_binance_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    for _ in range(3):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            if r.status_code != 200:
                time.sleep(1)
                continue

            data = r.json()

            if not isinstance(data, list) or len(data) < 60:
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

            if len(df) >= 60:
                return df

        except Exception as e:
            print("BINANCE ERROR:", e)
            time.sleep(1)

    return pd.DataFrame()


def get_yfinance_klines(symbol: str, interval: str) -> pd.DataFrame:
    try:
        period = {"1m": "7d", "5m": "30d", "15m": "60d"}[interval]

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

        if len(df) >= 60:
            return df

    except Exception as e:
        print("YFINANCE ERROR:", e)

    return pd.DataFrame()


def get_coingecko_btc() -> pd.DataFrame:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": "1"},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        data = r.json()
        prices = data.get("prices", [])

        if len(prices) < 60:
            return pd.DataFrame()

        df = pd.DataFrame(prices, columns=["time", "close"])
        df["open"] = df["close"].shift(1).fillna(df["close"])
        df["high"] = df[["open", "close"]].max(axis=1)
        df["low"] = df[["open", "close"]].min(axis=1)
        df["volume"] = 1.0

        return normalize_df(df)

    except Exception as e:
        print("COINGECKO ERROR:", e)

    return pd.DataFrame()


def get_klines(asset_key: str, interval: str):
    asset = ASSETS[asset_key]

    if asset_key == "BTC":
        df = get_binance_klines(asset["binance_symbol"], interval)
        if not df.empty:
            return df, "BINANCE"

        df = get_yfinance_klines(asset["yf_symbol"], interval)
        if not df.empty:
            return df, "YFINANCE"

        df = get_coingecko_btc()
        if not df.empty:
            return df, "COINGECKO"

        return pd.DataFrame(), "NONE"

    df = get_yfinance_klines(asset["yf_symbol"], interval)
    if not df.empty:
        return df, "YFINANCE"

    return pd.DataFrame(), "NONE"

# =========================
# INDICATORS
# =========================
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
    out["hh10"] = out["high"].rolling(10).max().shift(1)
    out["ll10"] = out["low"].rolling(10).min().shift(1)
    out["body"] = (out["close"] - out["open"]).abs()
    out["one_candle_move"] = (out["close"] - out["close"].shift(1)).abs()

    out.dropna(inplace=True)
    return out.reset_index(drop=True)

# =========================
# TREND
# =========================
def market_trend(df1: pd.DataFrame, df5: pd.DataFrame) -> str:
    r1 = df1.iloc[-1]
    r5 = df5.iloc[-1]

    if r5["ema9"] > r5["ema21"] and r1["ema9"] > r1["ema21"]:
        return "BULLISH"

    if r5["ema9"] < r5["ema21"] and r1["ema9"] < r1["ema21"]:
        return "BEARISH"

    return "CHOPPY"


def htf_bias(df15: pd.DataFrame) -> str:
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

# =========================
# ANTI-CHASE FILTERS
# =========================
def has_enough_volatility(asset_key: str, price: float, atr_now: float) -> bool:
    return (atr_now / max(price, 1.0)) >= ASSET_CONFIG[asset_key]["MIN_VOLATILITY_PCT"]


def not_too_extended(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    distance = abs(float(r["close"]) - float(r["ema9"])) / max(float(r["close"]), 1.0)
    return distance <= cfg["MAX_EMA9_DISTANCE_PCT"]


def no_big_candle(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]

    if float(r["atr"]) <= 0:
        return False

    if float(r["body"]) > float(r["atr"]) * cfg["MAX_BODY_ATR_MULT"]:
        return False

    if float(r["one_candle_move"]) > float(r["atr"]) * cfg["MAX_ONE_CANDLE_ATR_MULT"]:
        return False

    return True


def not_same_reentry_zone(asset_key: str, side: str, price: float) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    state = STATE[asset_key]
    last_price = float(state["LAST_ENTRY_PRICE"] or 0.0)
    last_side = state["LAST_ENTRY_SIDE"]

    if last_price <= 0 or last_side != side:
        return True

    distance = abs(price - last_price) / max(price, 1.0)
    return distance >= cfg["SAME_ZONE_REENTRY_PCT"]


def long_not_chasing(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]

    if not (cfg["LONG_RSI_MIN"] <= float(r["rsi"]) <= cfg["LONG_RSI_MAX"]):
        return False

    if not not_too_extended(asset_key, df1):
        return False

    if not no_big_candle(asset_key, df1):
        return False

    return True


def short_not_chasing(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]

    if not (cfg["SHORT_RSI_MIN"] <= float(r["rsi"]) <= cfg["SHORT_RSI_MAX"]):
        return False

    if not not_too_extended(asset_key, df1):
        return False

    if not no_big_candle(asset_key, df1):
        return False

    return True

# =========================
# ENTRY TYPES
# =========================
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


def sniper_long(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    return bool(
        p["close"] < p["ema9"]
        and r["close"] > r["ema9"]
        and p["rsi"] < 48
        and r["rsi"] > 50
        and r["low"] > p2["low"]
    )


def sniper_short(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    return bool(
        p["close"] > p["ema9"]
        and r["close"] < r["ema9"]
        and p["rsi"] > 52
        and r["rsi"] < 50
        and r["high"] < p2["high"]
    )


def confirm_long(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    return bool(r["close"] > p["close"] and r["close"] > r["ema9"])


def confirm_short(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    return bool(r["close"] < p["close"] and r["close"] < r["ema9"])

# =========================
# SCORING
# =========================
def score_long(asset_key: str, df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame):
    r = df1.iloc[-1]
    score = 0
    reasons = []

    trend = market_trend(df1, df5)
    htf = htf_bias(df15)

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

    if ASSET_CONFIG[asset_key]["LONG_RSI_MIN"] <= r["rsi"] <= ASSET_CONFIG[asset_key]["LONG_RSI_MAX"]:
        score += 15
        reasons.append("healthy RSI")

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

    if not no_big_candle(asset_key, df1):
        score -= 20
        reasons.append("impulse blocked")

    return max(0, min(int(score), 100)), reasons


def score_short(asset_key: str, df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame):
    r = df1.iloc[-1]
    score = 0
    reasons = []

    trend = market_trend(df1, df5)
    htf = htf_bias(df15)

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

    if ASSET_CONFIG[asset_key]["SHORT_RSI_MIN"] <= r["rsi"] <= ASSET_CONFIG[asset_key]["SHORT_RSI_MAX"]:
        score += 15
        reasons.append("healthy short RSI")

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

    if not no_big_candle(asset_key, df1):
        score -= 20
        reasons.append("impulse blocked")

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

# =========================
# SIGNAL ENGINE
# =========================
def get_signal(asset_key: str):
    df1_raw, src1 = get_klines(asset_key, "1m")
    df5_raw, src5 = get_klines(asset_key, "5m")
    df15_raw, src15 = get_klines(asset_key, "15m")

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
        "htf": htf_bias(df15),
        "long_score": ls,
        "short_score": ss,
        "long_reasons": lr,
        "short_reasons": sr,
        "long_breakout": breakout_long(asset_key, df1),
        "short_breakout": breakout_short(asset_key, df1),
        "long_pullback": pullback_long(asset_key, df1),
        "short_pullback": pullback_short(asset_key, df1),
        "long_sniper": sniper_long(asset_key, df1),
        "short_sniper": sniper_short(asset_key, df1),
        "confirm_long": confirm_long(df1),
        "confirm_short": confirm_short(df1),
        "feed": feed,
    }, feed

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset_key: str, sig, feed: str):
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

# =========================
# TRADE MANAGEMENT
# =========================
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


def start_trade(asset_key: str, side: str, trigger: str, score: int, reasons: list, price: float, atr_now: float):
    state = STATE[asset_key]
    cfg = ASSET_CONFIG[asset_key]
    name = ASSETS[asset_key]["name"]

    state["IN_TRADE"] = True
    state["TRADE_SIDE"] = side
    state["ENTRY_PRICE"] = price
    state["AVG_ENTRY_PRICE"] = price
    state["HIGHEST_PRICE"] = price
    state["LOWEST_PRICE"] = price
    state["SCALE_COUNT"] = 1
    state["LAST_SCALE_TIME"] = time.time()
    state["ENTRY_TYPE"] = trigger
    state["CONFIDENCE_LABEL"] = confidence_grade(score)
    state["PARTIAL_SENT"] = False
    state["BREAK_EVEN_ACTIVE"] = False
    state["LAST_TRAIL_SENT_SL"] = 0.0
    state["LAST_ENTRY_PRICE"] = price
    state["LAST_ENTRY_SIDE"] = side

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


def maybe_scale_in(asset_key: str, sig: dict):
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
            and sig["long_score"] >= SCALE_IN_SCORE
            and long_not_chasing(asset_key, sig["df1"])
            and not_same_reentry_zone(asset_key, "LONG", price)
        )
    else:
        ok = (
            price <= state["AVG_ENTRY_PRICE"] - atr_now * cfg["SCALE_IN_ATR_STEP"]
            and sig["confirm_short"]
            and sig["short_score"] >= SCALE_IN_SCORE
            and short_not_chasing(asset_key, sig["df1"])
            and not_same_reentry_zone(asset_key, "SHORT", price)
        )

    if not ok:
        return

    old_avg = state["AVG_ENTRY_PRICE"]
    state["AVG_ENTRY_PRICE"] = (old_avg * state["SCALE_COUNT"] + price) / (state["SCALE_COUNT"] + 1)
    state["SCALE_COUNT"] += 1
    state["LAST_SCALE_TIME"] = time.time()

    send(
        f"â {name} {state['TRADE_SIDE']} SCALE-IN\n\n"
        f"New add price: ${price:.2f}\n"
        f"Old avg: ${old_avg:.2f}\n"
        f"New avg: ${state['AVG_ENTRY_PRICE']:.2f}\n"
        f"Scale: {state['SCALE_COUNT']}/{MAX_SCALE_INS}"
    )


def maybe_send_trailing_update(asset_key: str, new_sl: float, atr_now: float):
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]
    min_step = atr_now * ASSET_CONFIG[asset_key]["TRAIL_UPDATE_MIN_ATR"]

    if state["LAST_TRAIL_SENT_SL"] == 0.0 or abs(new_sl - state["LAST_TRAIL_SENT_SL"]) >= min_step:
        state["LAST_TRAIL_SENT_SL"] = new_sl
        send(f"ð {name} {state['TRADE_SIDE']} TRAILING STOP\nNew SL: ${new_sl:.2f}")


def manage_trade(asset_key: str, sig: dict):
    state = STATE[asset_key]
    cfg = ASSET_CONFIG[asset_key]
    name = ASSETS[asset_key]["name"]

    price = sig["price"]
    atr_now = sig["atr"]

    maybe_scale_in(asset_key, sig)

    entry_ref = state["AVG_ENTRY_PRICE"] if state["AVG_ENTRY_PRICE"] > 0 else state["ENTRY_PRICE"]

    if state["TRADE_SIDE"] == "LONG":
        state["HIGHEST_PRICE"] = max(state["HIGHEST_PRICE"], price)

        if not state["BREAK_EVEN_ACTIVE"] and price >= entry_ref + atr_now * cfg["BREAK_EVEN_ATR_TRIGGER"]:
            state["STOP_LOSS"] = max(state["STOP_LOSS"], entry_ref)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"â¡ {name} LONG BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if not state["PARTIAL_SENT"] and price >= entry_ref + atr_now * cfg["PARTIAL_ATR_TRIGGER"]:
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

        if not state["BREAK_EVEN_ACTIVE"] and price <= entry_ref - atr_now * cfg["BREAK_EVEN_ATR_TRIGGER"]:
            state["STOP_LOSS"] = min(state["STOP_LOSS"], entry_ref)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"â¡ {name} SHORT BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if not state["PARTIAL_SENT"] and price <= entry_ref - atr_now * cfg["PARTIAL_ATR_TRIGGER"]:
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

# =========================
# ENTRY DECISION
# =========================
def choose_triggers(sig: dict):
    long_trigger = None
    short_trigger = None

    # Pullback first to avoid buying extended breakouts late
    if sig["long_pullback"]:
        long_trigger = "PULLBACK"
    elif sig["long_sniper"]:
        long_trigger = "SNIPER"
    elif sig["long_breakout"]:
        long_trigger = "BREAKOUT"

    if sig["short_pullback"]:
        short_trigger = "PULLBACK"
    elif sig["short_sniper"]:
        short_trigger = "SNIPER"
    elif sig["short_breakout"]:
        short_trigger = "BREAKDOWN"

    return long_trigger, short_trigger


def try_enter_trade(asset_key: str, sig: dict):
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
        and not_same_reentry_zone(asset_key, "LONG", sig["price"])
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
        and not_same_reentry_zone(asset_key, "SHORT", sig["price"])
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

# =========================
# MAIN
# =========================
def run():
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
