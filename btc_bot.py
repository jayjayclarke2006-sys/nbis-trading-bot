import os
import time
import requests
import pandas as pd
import yfinance as yf

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

# =========================
# CONFIG
# =========================
CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 300
COOLDOWN_SECONDS = 600
DATA_FAIL_ALERT_COOLDOWN = 1800
DEBUG_MODE = True

MIN_ENTRY_SCORE = 72
FULL_SIZE_SCORE = 82

MAX_SCALE_INS = 2
SCALE_IN_COOLDOWN_SECONDS = 180

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

CFG = {
    "BTC": {
        "SL_ATR": 2.0,
        "TP_ATR": 4.5,
        "TRAIL_ATR": 2.2,
        "BE_ATR": 1.35,
        "PARTIAL_ATR": 2.0,
        "TRAIL_START_ATR": 1.75,
        "MIN_VOL_PCT": 0.0008,
        "MAX_EMA_DIST": 0.007,
        "MAX_BODY_ATR": 1.15,
        "LONG_RSI_MAX": 70,
        "SHORT_RSI_MIN": 30,
        "BREAK_LONG": 1.001,
        "BREAK_SHORT": 0.999,
        "PULLBACK_LONG": 1.002,
        "PULLBACK_SHORT": 0.998,
        "SCALE_ATR": 0.85,
    },
    "GOLD": {
        "SL_ATR": 1.35,
        "TP_ATR": 3.0,
        "TRAIL_ATR": 1.6,
        "BE_ATR": 1.0,
        "PARTIAL_ATR": 1.5,
        "TRAIL_START_ATR": 1.25,
        "MIN_VOL_PCT": 0.00015,
        "MAX_EMA_DIST": 0.0045,
        "MAX_BODY_ATR": 0.90,
        "LONG_RSI_MAX": 66,
        "SHORT_RSI_MIN": 34,
        "BREAK_LONG": 1.0005,
        "BREAK_SHORT": 0.9995,
        "PULLBACK_LONG": 1.0015,
        "PULLBACK_SHORT": 0.9985,
        "SCALE_ATR": 0.45,
    },
}

# =========================
# STATE
# =========================
STATE = {
    asset: {
        "IN_TRADE": False,
        "SIDE": None,
        "ENTRY": 0.0,
        "AVG_ENTRY": 0.0,
        "SL": 0.0,
        "TP": 0.0,
        "HIGH": 0.0,
        "LOW": 0.0,
        "PARTIAL_SENT": False,
        "BE_ACTIVE": False,
        "SCALE_COUNT": 0,
        "LAST_SCALE_TS": 0.0,
        "LAST_TRADE_TS": 0.0,
        "LAST_HEARTBEAT_TS": 0.0,
        "LAST_DATA_FAIL_TS": 0.0,
        "LAST_TRAIL_SL": 0.0,
        "ENTRY_TYPE": None,
        "CONFIDENCE": None,
        "LAST_PRICE": None,
        "LAST_FEED": "UNKNOWN",
    }
    for asset in ASSETS
}

# =========================
# TELEGRAM
# =========================
def send(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(msg)
        return

    for _ in range(3):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )
            return
        except Exception as e:
            print("Telegram error:", e)
            time.sleep(1)

# =========================
# DATA FEEDS
# =========================
def td_interval(interval: str) -> str:
    return {"1m": "1min", "5m": "5min", "15m": "15min"}[interval]

def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]

    needed = ["open", "high", "low", "close", "volume"]

    for c in ["open", "high", "low", "close"]:
        if c not in df.columns:
            return pd.DataFrame()

    if "volume" not in df.columns:
        df["volume"] = 1.0

    df = df[needed].copy()

    for c in needed:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df.dropna(inplace=True)
    return df.reset_index(drop=True)

def get_binance(symbol: str, interval: str) -> pd.DataFrame:
    for _ in range(3):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": 500},
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            if r.status_code != 200:
                time.sleep(1)
                continue

            data = r.json()

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

        except Exception:
            time.sleep(1)

    return pd.DataFrame()

def get_twelvedata(symbol: str, interval: str) -> pd.DataFrame:
    try:
        if not TWELVEDATA_API_KEY or not symbol:
            return pd.DataFrame()

        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": symbol,
                "interval": td_interval(interval),
                "apikey": TWELVEDATA_API_KEY,
                "outputsize": 500,
                "format": "JSON",
            },
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        data = r.json()

        if not isinstance(data, dict) or "values" not in data:
            return pd.DataFrame()

        df = pd.DataFrame(data["values"]).iloc[::-1].reset_index(drop=True)
        df = normalize_df(df)

        if len(df) >= 50:
            return df

        return pd.DataFrame()

    except Exception:
        return pd.DataFrame()

def get_yfinance(symbol: str, interval: str) -> pd.DataFrame:
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

        if len(df) >= 50:
            return df

        return pd.DataFrame()

    except Exception:
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

        if len(prices) < 50:
            return pd.DataFrame()

        df = pd.DataFrame(prices, columns=["time", "close"])
        df["open"] = df["close"].shift(1).fillna(df["close"])
        df["high"] = df[["open", "close"]].max(axis=1)
        df["low"] = df[["open", "close"]].min(axis=1)
        df["volume"] = 1.0

        return normalize_df(df)

    except Exception:
        return pd.DataFrame()

def get_klines(asset_key: str, interval: str):
    asset = ASSETS[asset_key]

    if asset_key == "BTC":
        df = get_binance(asset["binance_symbol"], interval)
        if not df.empty:
            return df, "BINANCE"

        df = get_twelvedata(asset["td_symbol"], interval)
        if not df.empty:
            return df, "TWELVEDATA"

        df = get_yfinance(asset["yf_symbol"], interval)
        if not df.empty:
            return df, "YFINANCE"

        df = get_coingecko_btc()
        if not df.empty:
            return df, "COINGECKO"

        return pd.DataFrame(), "NONE"

    if asset_key == "GOLD":
        df = get_twelvedata(asset["td_symbol"], interval)
        if not df.empty:
            return df, "TWELVEDATA"

        df = get_yfinance(asset["yf_symbol"], interval)
        if not df.empty:
            return df, "YFINANCE"

        return pd.DataFrame(), "NONE"

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

    out.dropna(inplace=True)
    return out

# =========================
# TREND / FILTERS
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

def volatility_ok(asset_key: str, price: float, atr: float) -> bool:
    return (atr / max(price, 1.0)) >= CFG[asset_key]["MIN_VOL_PCT"]

def not_extended(asset_key: str, price: float, ema9: float) -> bool:
    return abs(price - ema9) / max(price, 1.0) <= CFG[asset_key]["MAX_EMA_DIST"]

def clean_candle(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = CFG[asset_key]
    r = df1.iloc[-1]

    if r["atr"] <= 0:
        return False

    if r["body"] > r["atr"] * cfg["MAX_BODY_ATR"]:
        return False

    return True

def long_not_chasing(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]

    if r["rsi"] > CFG[asset_key]["LONG_RSI_MAX"]:
        return False

    if not not_extended(asset_key, float(r["close"]), float(r["ema9"])):
        return False

    return clean_candle(asset_key, df1)

def short_not_chasing(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]

    if r["rsi"] < CFG[asset_key]["SHORT_RSI_MIN"]:
        return False

    if not not_extended(asset_key, float(r["close"]), float(r["ema9"])):
        return False

    return clean_candle(asset_key, df1)

# =========================
# ENTRY TYPES
# =========================
def breakout_long(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = CFG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    return bool(
        r["close"] > r["hh10"] * cfg["BREAK_LONG"]
        and r["close"] > p["high"]
        and r["close"] > r["ema9"]
        and r["volume"] >= r["vol_ma"]
    )

def breakout_short(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = CFG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    return bool(
        r["close"] < r["ll10"] * cfg["BREAK_SHORT"]
        and r["close"] < p["low"]
        and r["close"] < r["ema9"]
        and r["volume"] >= r["vol_ma"]
    )

def sniper_long(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    return bool(
        p["close"] < p["ema9"]
        and r["close"] > r["ema9"]
        and p["rsi"] < 46
        and r["rsi"] > 50
        and r["low"] > p2["low"]
    )

def sniper_short(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    return bool(
        p["close"] > p["ema9"]
        and r["close"] < r["ema9"]
        and p["rsi"] > 54
        and r["rsi"] < 50
        and r["high"] < p2["high"]
    )

def pullback_long(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    return bool(
        p["close"] <= p["ema9"] * CFG[asset_key]["PULLBACK_LONG"]
        and r["close"] > r["ema9"]
        and r["rsi"] > 50
    )

def pullback_short(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    return bool(
        p["close"] >= p["ema9"] * CFG[asset_key]["PULLBACK_SHORT"]
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

# =========================
# SCORING
# =========================
def score_long(asset_key: str, df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame):
    r = df1.iloc[-1]
    score = 0
    reasons = []

    trend = market_trend(df1, df5)
    bias = htf_bias(df15)

    if trend == "BULLISH":
        score += 25
        reasons.append("1m/5m bullish")

    if bias == "STRONG_BULL":
        score += 25
        reasons.append("15m strong bull")
    elif bias == "BULL":
        score += 15
        reasons.append("15m bull")
    elif bias in ["BEAR", "STRONG_BEAR"]:
        score -= 25
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

    if sniper_long(df1):
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
    bias = htf_bias(df15)

    if trend == "BEARISH":
        score += 25
        reasons.append("1m/5m bearish")

    if bias == "STRONG_BEAR":
        score += 25
        reasons.append("15m strong bear")
    elif bias == "BEAR":
        score += 15
        reasons.append("15m bear")
    elif bias in ["BULL", "STRONG_BULL"]:
        score -= 25
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

    if sniper_short(df1):
        score += 10
        reasons.append("sniper")

    if pullback_short(asset_key, df1):
        score += 10
        reasons.append("pullback")

    return max(0, min(int(score), 100)), reasons

def confidence(score: int) -> str:
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

    sources = [x for x in [src1, src5, src15] if x != "NONE"]
    feed = "/".join(sorted(set(sources))) if sources else "NONE"

    if df1.empty or df5.empty or df15.empty:
        return None, feed

    price = float(df1.iloc[-1]["close"])
    atr = float(df1.iloc[-1]["atr"])

    if not volatility_ok(asset_key, price, atr):
        return None, feed

    long_score, long_reasons = score_long(asset_key, df1, df5, df15)
    short_score, short_reasons = score_short(asset_key, df1, df5, df15)

    return {
        "asset": asset_key,
        "price": price,
        "atr": atr,
        "df1": df1,
        "df5": df5,
        "df15": df15,
        "trend": market_trend(df1, df5),
        "htf": htf_bias(df15),
        "long_score": long_score,
        "short_score": short_score,
        "long_reasons": long_reasons,
        "short_reasons": short_reasons,
        "long_breakout": breakout_long(asset_key, df1),
        "short_breakout": breakout_short(asset_key, df1),
        "long_sniper": sniper_long(df1),
        "short_sniper": sniper_short(df1),
        "long_pullback": pullback_long(asset_key, df1),
        "short_pullback": pullback_short(asset_key, df1),
        "confirm_long": confirm_long(df1),
        "confirm_short": confirm_short(df1),
        "feed": feed,
    }, feed

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset_key: str, sig, feed: str):
    s = STATE[asset_key]
    now = time.time()
    name = ASSETS[asset_key]["name"]

    if now - s["LAST_HEARTBEAT_TS"] < HEARTBEAT_SECONDS:
        return

    if sig is None:
        send(
            f"💓 {name} HEARTBEAT\n\n"
            f"Status: NO DATA / WAITING\n"
            f"In trade: {'YES' if s['IN_TRADE'] else 'NO'}\n"
            f"Feed: {feed}"
        )
    else:
        r = sig["df1"].iloc[-1]
        s["LAST_PRICE"] = sig["price"]
        s["LAST_FEED"] = sig["feed"]

        send(
            f"💓 {name} HEARTBEAT\n\n"
            f"Price: ${sig['price']:.2f}\n"
            f"RSI: {float(r['rsi']):.1f}\n"
            f"Trend: {sig['trend']}\n"
            f"HTF Bias: {sig['htf']}\n"
            f"Long: {sig['long_score']} | Short: {sig['short_score']}\n"
            f"In trade: {'YES' if s['IN_TRADE'] else 'NO'}\n"
            f"Feed: {sig['feed']}"
        )

    s["LAST_HEARTBEAT_TS"] = now

# =========================
# TRADE MANAGEMENT
# =========================
def size_label(score: int) -> str:
    return "FULL" if score >= FULL_SIZE_SCORE else "SNIPER"

def reset_trade(asset_key: str):
    s = STATE[asset_key]
    s["IN_TRADE"] = False
    s["SIDE"] = None
    s["ENTRY"] = 0.0
    s["AVG_ENTRY"] = 0.0
    s["SL"] = 0.0
    s["TP"] = 0.0
    s["HIGH"] = 0.0
    s["LOW"] = 0.0
    s["PARTIAL_SENT"] = False
    s["BE_ACTIVE"] = False
    s["SCALE_COUNT"] = 0
    s["LAST_SCALE_TS"] = 0.0
    s["LAST_TRADE_TS"] = time.time()
    s["LAST_TRAIL_SL"] = 0.0
    s["ENTRY_TYPE"] = None
    s["CONFIDENCE"] = None

def start_trade(asset_key: str, side: str, trigger: str, score: int, reasons: list, price: float, atr: float):
    cfg = CFG[asset_key]
    s = STATE[asset_key]
    name = ASSETS[asset_key]["name"]

    s["IN_TRADE"] = True
    s["SIDE"] = side
    s["ENTRY"] = price
    s["AVG_ENTRY"] = price
    s["HIGH"] = price
    s["LOW"] = price
    s["SCALE_COUNT"] = 1
    s["LAST_SCALE_TS"] = time.time()
    s["PARTIAL_SENT"] = False
    s["BE_ACTIVE"] = False
    s["ENTRY_TYPE"] = trigger
    s["CONFIDENCE"] = confidence(score)
    s["LAST_TRAIL_SL"] = 0.0

    if side == "LONG":
        s["SL"] = price - atr * cfg["SL_ATR"]
        s["TP"] = price + atr * cfg["TP_ATR"]
        emoji = "🚀"
    else:
        s["SL"] = price + atr * cfg["SL_ATR"]
        s["TP"] = price - atr * cfg["TP_ATR"]
        emoji = "📉"

    send(
        f"{emoji} {name} {side} ENTRY\n\n"
        f"Trigger: {trigger}\n"
        f"Size: {size_label(score)}\n"
        f"Confidence: {s['CONFIDENCE']}\n"
        f"Scale: 1/{MAX_SCALE_INS}\n"
        f"Price: ${price:.2f}\n"
        f"Score: {score}\n"
        f"Reasons: {', '.join(reasons[:4])}\n\n"
        f"SL: ${s['SL']:.2f}\n"
        f"TP: ${s['TP']:.2f}"
    )

def maybe_scale(asset_key: str, sig: dict):
    s = STATE[asset_key]
    cfg = CFG[asset_key]

    if not s["IN_TRADE"]:
        return

    if s["SCALE_COUNT"] >= MAX_SCALE_INS:
        return

    if time.time() - s["LAST_SCALE_TS"] < SCALE_IN_COOLDOWN_SECONDS:
        return

    price = sig["price"]
    atr = sig["atr"]

    if s["SIDE"] == "LONG":
        ok = (
            price >= s["AVG_ENTRY"] + atr * cfg["SCALE_ATR"]
            and sig["confirm_long"]
            and sig["long_score"] >= MIN_ENTRY_SCORE
            and long_not_chasing(asset_key, sig["df1"])
        )
    else:
        ok = (
            price <= s["AVG_ENTRY"] - atr * cfg["SCALE_ATR"]
            and sig["confirm_short"]
            and sig["short_score"] >= MIN_ENTRY_SCORE
            and short_not_chasing(asset_key, sig["df1"])
        )

    if not ok:
        return

    old_avg = s["AVG_ENTRY"]
    s["AVG_ENTRY"] = (old_avg * s["SCALE_COUNT"] + price) / (s["SCALE_COUNT"] + 1)
    s["SCALE_COUNT"] += 1
    s["LAST_SCALE_TS"] = time.time()

    send(
        f"➕ {asset_key} {s['SIDE']} SCALE-IN\n\n"
        f"New add price: ${price:.2f}\n"
        f"Old avg: ${old_avg:.2f}\n"
        f"New avg: ${s['AVG_ENTRY']:.2f}\n"
        f"Scale: {s['SCALE_COUNT']}/{MAX_SCALE_INS}"
    )

def trail_alert(asset_key: str, new_sl: float, atr: float):
    s = STATE[asset_key]
    min_step = atr * 0.2

    if s["LAST_TRAIL_SL"] == 0.0 or abs(new_sl - s["LAST_TRAIL_SL"]) >= min_step:
        s["LAST_TRAIL_SL"] = new_sl
        send(f"📈 {asset_key} {s['SIDE']} TRAILING STOP\nNew SL: ${new_sl:.2f}")

def manage_trade(asset_key: str, sig: dict):
    s = STATE[asset_key]
    cfg = CFG[asset_key]
    name = ASSETS[asset_key]["name"]

    price = sig["price"]
    atr = sig["atr"]
    entry = s["AVG_ENTRY"] if s["AVG_ENTRY"] > 0 else s["ENTRY"]

    maybe_scale(asset_key, sig)

    if s["SIDE"] == "LONG":
        s["HIGH"] = max(s["HIGH"], price)

        if not s["BE_ACTIVE"] and price >= entry + atr * cfg["BE_ATR"]:
            s["SL"] = max(s["SL"], entry)
            s["BE_ACTIVE"] = True
            send(f"⚡ {name} LONG BREAK-EVEN\nNew SL: ${s['SL']:.2f}")

        if not s["PARTIAL_SENT"] and price >= entry + atr * cfg["PARTIAL_ATR"]:
            s["PARTIAL_SENT"] = True
            send(f"💰 {name} LONG PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if s["BE_ACTIVE"] and price >= entry + atr * cfg["TRAIL_START_ATR"]:
            new_sl = s["HIGH"] - atr * cfg["TRAIL_ATR"]
            if new_sl > s["SL"]:
                s["SL"] = new_sl
                trail_alert(asset_key, new_sl, atr)

        if price <= s["SL"]:
            send(f"❌ {name} LONG STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)

        elif price >= s["TP"]:
            send(f"🎯 {name} LONG TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)

    elif s["SIDE"] == "SHORT":
        s["LOW"] = min(s["LOW"], price)

        if not s["BE_ACTIVE"] and price <= entry - atr * cfg["BE_ATR"]:
            s["SL"] = min(s["SL"], entry)
            s["BE_ACTIVE"] = True
            send(f"⚡ {name} SHORT BREAK-EVEN\nNew SL: ${s['SL']:.2f}")

        if not s["PARTIAL_SENT"] and price <= entry - atr * cfg["PARTIAL_ATR"]:
            s["PARTIAL_SENT"] = True
            send(f"💰 {name} SHORT PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if s["BE_ACTIVE"] and price <= entry - atr * cfg["TRAIL_START_ATR"]:
            new_sl = s["LOW"] + atr * cfg["TRAIL_ATR"]
            if new_sl < s["SL"]:
                s["SL"] = new_sl
                trail_alert(asset_key, new_sl, atr)

        if price >= s["SL"]:
            send(f"❌ {name} SHORT STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)

        elif price <= s["TP"]:
            send(f"🎯 {name} SHORT TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)

# =========================
# MAIN LOOP
# =========================
def run():
    time.sleep(3)
    send(f"🔥 BTC + GOLD BOT LIVE 🔥\nTime: {time.strftime('%H:%M:%S')}")

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

                if time.time() - STATE[asset_key]["LAST_TRADE_TS"] < COOLDOWN_SECONDS:
                    continue

                if sig["trend"] == "CHOPPY":
                    continue

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

                if (
                    long_trigger
                    and sig["long_score"] >= MIN_ENTRY_SCORE
                    and sig["trend"] == "BULLISH"
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

                elif (
                    short_trigger
                    and sig["short_score"] >= MIN_ENTRY_SCORE
                    and sig["trend"] == "BEARISH"
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

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"BOT ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
