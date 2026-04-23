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

LONG_ALERT_SCORE = 68
SHORT_ALERT_SCORE = 68
A_SETUP_SCORE = 72
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

ASSET_CONFIG = {
    "BTC": {
        "ATR_SL_MULT": 2.0,
        "ATR_TP_MULT": 4.5,
        "ATR_TRAIL_MULT": 2.2,
        "BREAK_EVEN_ATR_TRIGGER": 1.4,
        "PARTIAL_ATR_TRIGGER": 2.0,
        "TRAILING_ACTIVATION_ATR": 1.7,
        "TRAIL_UPDATE_MIN_ATR": 0.25,
        "MIN_VOLATILITY_PCT": 0.0008,
        "MAX_EMA9_DISTANCE_PCT": 0.0065,
        "SCALE_IN_ATR_STEP": 0.85,
        "LONG_RSI_MAX": 68.0,
        "SHORT_RSI_MIN": 32.0,
        "MAX_BODY_ATR_MULT": 1.05,
        "BREAKOUT_BUFFER_LONG": 1.0010,
        "BREAKOUT_BUFFER_SHORT": 0.9990,
        "VOL_CONFIRM_MULT": 1.05,
    },
    "GOLD": {
        "ATR_SL_MULT": 1.35,
        "ATR_TP_MULT": 3.0,
        "ATR_TRAIL_MULT": 1.55,
        "BREAK_EVEN_ATR_TRIGGER": 1.0,
        "PARTIAL_ATR_TRIGGER": 1.5,
        "TRAILING_ACTIVATION_ATR": 1.25,
        "TRAIL_UPDATE_MIN_ATR": 0.15,
        "MIN_VOLATILITY_PCT": 0.00018,
        "MAX_EMA9_DISTANCE_PCT": 0.0045,
        "SCALE_IN_ATR_STEP": 0.45,
        "LONG_RSI_MAX": 66.0,
        "SHORT_RSI_MIN": 34.0,
        "MAX_BODY_ATR_MULT": 0.85,
        "BREAKOUT_BUFFER_LONG": 1.0005,
        "BREAKOUT_BUFFER_SHORT": 0.9995,
        "VOL_CONFIRM_MULT": 1.00,
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
        "LAST_DATA_FAIL_ALERT_TS": 0.0,
        "DATA_SOURCE": "UNKNOWN",
    }
    for key in ASSETS
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
            time.sleep(2)

# =========================
# DATA
# =========================
def td_interval(interval: str) -> str:
    return {"1m": "1min", "5m": "5min", "15m": "15min"}[interval]

def clean_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    needed = ["open", "high", "low", "close", "volume"]

    for col in needed:
        if col not in df.columns:
            if col == "volume":
                df[col] = 1.0
            else:
                return pd.DataFrame()

    df = df[needed].copy()

    for col in needed:
        df[col] = pd.to_numeric(df[col], errors="coerce")

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
                    "time", "open", "high", "low", "close", "volume",
                    "ct", "qav", "trades", "tbv", "tqv", "ignore",
                ],
            )

            return clean_df(df)

        except Exception:
            time.sleep(1)

    return pd.DataFrame()

def get_twelvedata(symbol: str, interval: str) -> pd.DataFrame:
    try:
        if not TWELVEDATA_API_KEY:
            return pd.DataFrame()

        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": symbol,
                "interval": td_interval(interval),
                "outputsize": 500,
                "apikey": TWELVEDATA_API_KEY,
                "format": "JSON",
            },
            timeout=15,
        )

        data = r.json()

        if "values" not in data:
            return pd.DataFrame()

        df = pd.DataFrame(data["values"]).iloc[::-1]
        return clean_df(df)

    except Exception:
        return pd.DataFrame()

def get_yfinance(symbol: str, interval: str) -> pd.DataFrame:
    try:
        period_map = {"1m": "7d", "5m": "30d", "15m": "60d"}

        df = yf.download(
            symbol,
            period=period_map.get(interval, "30d"),
            interval=interval,
            progress=False,
            auto_adjust=False,
        )

        if df is None or df.empty:
            return pd.DataFrame()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [str(c).lower() for c in df.columns]
        return clean_df(df)

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

        if "prices" not in data:
            return pd.DataFrame()

        prices = data["prices"]
        df = pd.DataFrame(prices, columns=["time", "close"])

        df["open"] = df["close"].shift(1).fillna(df["close"])
        df["high"] = df[["open", "close"]].max(axis=1)
        df["low"] = df[["open", "close"]].min(axis=1)
        df["volume"] = 1.0

        return clean_df(df)

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
def ema(df: pd.DataFrame, span: int) -> pd.Series:
    return df["close"].ewm(span=span, adjust=False).mean()

def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = delta.clip(upper=0).abs().rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 60:
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
    return out

# =========================
# FILTERS / TREND
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

def enough_volatility(asset_key: str, price: float, atr_now: float) -> bool:
    return (atr_now / max(price, 1.0)) >= ASSET_CONFIG[asset_key]["MIN_VOLATILITY_PCT"]

def not_too_extended(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    distance = abs(float(r["close"]) - float(r["ema9"])) / max(float(r["close"]), 1.0)
    return distance <= cfg["MAX_EMA9_DISTANCE_PCT"]

def clean_candle(asset_key: str, df1: pd.DataFrame) -> bool:
    cfg = ASSET_CONFIG[asset_key]
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    if float(r["atr"]) <= 0:
        return False

    move = abs(float(r["close"]) - float(p["close"]))
    if move > float(r["atr"]) * cfg["MAX_IMPULSE_ATR_MULT"]:
        return False

    if float(r["body"]) > float(r["atr"]) * cfg["MAX_BODY_ATR_MULT"]:
        return False

    return True

def good_long_location(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    cfg = ASSET_CONFIG[asset_key]

    if float(r["rsi"]) > cfg["LONG_RSI_MAX"]:
        return False

    near_ema = float(r["close"]) <= float(r["ema9"]) * 1.0015
    fresh_breakout = float(r["close"]) > float(r["hh10"]) * cfg["BREAKOUT_BUFFER_LONG"] and float(r["close"]) > float(p["high"])

    return (near_ema or fresh_breakout) and not_too_extended(asset_key, df1)

def good_short_location(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    cfg = ASSET_CONFIG[asset_key]

    if float(r["rsi"]) < cfg["SHORT_RSI_MIN"]:
        return False

    near_ema = float(r["close"]) >= float(r["ema9"]) * 0.9985
    fresh_breakdown = float(r["close"]) < float(r["ll10"]) * cfg["BREAKOUT_BUFFER_SHORT"] and float(r["close"]) < float(p["low"])

    return (near_ema or fresh_breakdown) and not_too_extended(asset_key, df1)

# =========================
# ENTRY TYPES
# =========================
def breakout_long(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    cfg = ASSET_CONFIG[asset_key]

    return bool(
        r["close"] > r["hh10"] * cfg["BREAKOUT_BUFFER_LONG"]
        and r["close"] > p["high"]
        and r["volume"] >= r["vol_ma"] * cfg["VOL_CONFIRM_MULT"]
        and r["close"] > r["ema9"]
    )

def breakout_short(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    cfg = ASSET_CONFIG[asset_key]

    return bool(
        r["close"] < r["ll10"] * cfg["BREAKOUT_BUFFER_SHORT"]
        and r["close"] < p["low"]
        and r["volume"] >= r["vol_ma"] * cfg["VOL_CONFIRM_MULT"]
        and r["close"] < r["ema9"]
    )

def pullback_long(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    return bool(
        r["close"] > r["ema21"]
        and r["close"] <= r["ema9"] * ASSET_CONFIG[asset_key]["PULLBACK_LONG_EMA9_MAX"]
        and r["rsi"] > 50
    )

def pullback_short(asset_key: str, df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    return bool(
        r["close"] < r["ema21"]
        and r["close"] >= r["ema9"] * ASSET_CONFIG[asset_key]["PULLBACK_SHORT_EMA9_MIN"]
        and r["rsi"] < 50
    )

def sniper_long(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    return bool(
        p["close"] < p["ema9"]
        and r["close"] > r["ema9"]
        and p["rsi"] < 47
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
        and p["rsi"] > 53
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
def long_score(asset_key: str, df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame):
    r = df1.iloc[-1]
    score = 0
    reasons = []

    trend_now = market_trend(df1, df5)
    htf = htf_bias(df15)

    if trend_now == "BULLISH":
        score += 25
        reasons.append("1m/5m bullish")

    if htf == "STRONG_BULL":
        score += 25
        reasons.append("15m strong bull")
    elif htf == "BULL":
        score += 15
        reasons.append("15m bull")
    elif htf in ["BEAR", "STRONG_BEAR"]:
        score -= 25
        reasons.append("15m against long")

    if r["ema9"] > r["ema21"]:
        score += 15
        reasons.append("EMA9 above EMA21")

    if 50 < r["rsi"] < 68:
        score += 15
        reasons.append("healthy RSI")
    elif 48 < r["rsi"] < 72:
        score += 8
        reasons.append("acceptable RSI")

    if r["volume"] >= r["vol_ma"] * 1.1:
        score += 10
        reasons.append("volume confirm")

    if pullback_long(asset_key, df1):
        score += 10
        reasons.append("pullback location")

    if breakout_long(asset_key, df1):
        score += 10
        reasons.append("fresh breakout")

    if sniper_long(df1):
        score += 10
        reasons.append("sniper reclaim")

    return max(0, min(int(score), 100)), reasons

def short_score(asset_key: str, df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame):
    r = df1.iloc[-1]
    score = 0
    reasons = []

    trend_now = market_trend(df1, df5)
    htf = htf_bias(df15)

    if trend_now == "BEARISH":
        score += 25
        reasons.append("1m/5m bearish")

    if htf == "STRONG_BEAR":
        score += 25
        reasons.append("15m strong bear")
    elif htf == "BEAR":
        score += 15
        reasons.append("15m bear")
    elif htf in ["BULL", "STRONG_BULL"]:
        score -= 25
        reasons.append("15m against short")

    if r["ema9"] < r["ema21"]:
        score += 15
        reasons.append("EMA9 below EMA21")

    if 32 < r["rsi"] < 50:
        score += 15
        reasons.append("healthy short RSI")
    elif 28 < r["rsi"] < 54:
        score += 8
        reasons.append("acceptable short RSI")

    if r["volume"] >= r["vol_ma"] * 1.1:
        score += 10
        reasons.append("volume confirm")

    if pullback_short(asset_key, df1):
        score += 10
        reasons.append("pullback rejection")

    if breakout_short(asset_key, df1):
        score += 10
        reasons.append("fresh breakdown")

    if sniper_short(df1):
        score += 10
        reasons.append("sniper reject")

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
# HEARTBEAT / ALERTS
# =========================
def maybe_alert_data_fail(asset_key: str, s1: str, s5: str, s15: str):
    state = STATE[asset_key]
    now = time.time()

    if s1 == "NONE" and s5 == "NONE" and s15 == "NONE":
        if now - state["LAST_DATA_FAIL_ALERT_TS"] >= DATA_FAIL_ALERT_COOLDOWN:
            send(
                f"⚠️ {ASSETS[asset_key]['name']} DATA FEED FAIL\n"
                f"1m: {s1}\n"
                f"5m: {s5}\n"
                f"15m: {s15}"
            )
            state["LAST_DATA_FAIL_ALERT_TS"] = now

def heartbeat(asset_key: str, sig):
    now = time.time()
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]

    if now - state["LAST_HEARTBEAT_TS"] < HEARTBEAT_SECONDS:
        return

    if sig is None:
        send(
            f"💓 {name} HEARTBEAT\n\n"
            f"Status: NO DATA\n"
            f"In trade: {'YES' if state['IN_TRADE'] else 'NO'}\n"
            f"Feed: {state['DATA_SOURCE']}"
        )
        state["LAST_HEARTBEAT_TS"] = now
        return

    r = sig["df1"].iloc[-1]
    state["DATA_SOURCE"] = sig["data_source"]

    send(
        f"💓 {name} HEARTBEAT\n\n"
        f"Price: ${sig['price']:.2f}\n"
        f"RSI: {float(r['rsi']):.1f}\n"
        f"Trend: {sig['trend']}\n"
        f"HTF Bias: {sig['htf_bias']}\n"
        f"Long: {sig['long_score']} | Short: {sig['short_score']}\n"
        f"In trade: {'YES' if state['IN_TRADE'] else 'NO'}\n"
        f"Feed: {sig['data_source']}"
    )

    state["LAST_HEARTBEAT_TS"] = now

# =========================
# SIGNAL ENGINE
# =========================
def get_signal(asset_key: str):
    df1_raw, s1 = get_klines(asset_key, "1m")
    df5_raw, s5 = get_klines(asset_key, "5m")
    df15_raw, s15 = get_klines(asset_key, "15m")

    maybe_alert_data_fail(asset_key, s1, s5, s15)

    df1 = add_indicators(df1_raw)
    df5 = add_indicators(df5_raw)
    df15 = add_indicators(df15_raw)

    if df1.empty or df5.empty or df15.empty:
        return None

    price = float(df1.iloc[-1]["close"])
    atr_now = float(df1.iloc[-1]["atr"])

    if not enough_volatility(asset_key, price, atr_now):
        return None

    ls, lr = long_score(asset_key, df1, df5, df15)
    ss, sr = short_score(asset_key, df1, df5, df15)

    sources = [s for s in [s1, s5, s15] if s != "NONE"]
    data_source = "/".join(sorted(set(sources))) if sources else "NONE"

    return {
        "asset_key": asset_key,
        "price": price,
        "atr": atr_now,
        "df1": df1,
        "df5": df5,
        "df15": df15,
        "trend": market_trend(df1, df5),
        "htf_bias": htf_bias(df15),
        "long_score": ls,
        "short_score": ss,
        "long_reasons": lr,
        "short_reasons": sr,
        "long_breakout": breakout_long(asset_key, df1),
        "short_breakout": breakout_short(asset_key, df1),
        "long_pullback": pullback_long(asset_key, df1),
        "short_pullback": pullback_short(asset_key, df1),
        "long_sniper": sniper_long(df1),
        "short_sniper": sniper_short(df1),
        "confirm_long": confirm_long(df1),
        "confirm_short": confirm_short(df1),
        "data_source": data_source,
    }

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

    if side == "LONG":
        state["STOP_LOSS"] = price - atr_now * cfg["ATR_SL_MULT"]
        state["TAKE_PROFIT"] = price + atr_now * cfg["ATR_TP_MULT"]
        emoji = "🚀"
    else:
        state["STOP_LOSS"] = price + atr_now * cfg["ATR_SL_MULT"]
        state["TAKE_PROFIT"] = price - atr_now * cfg["ATR_TP_MULT"]
        emoji = "📉"

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

    if not state["IN_TRADE"]:
        return

    if state["SCALE_COUNT"] >= MAX_SCALE_INS:
        return

    if time.time() - state["LAST_SCALE_TIME"] < SCALE_IN_COOLDOWN_SECONDS:
        return

    price = sig["price"]
    atr_now = sig["atr"]

    if state["TRADE_SIDE"] == "LONG":
        if price >= state["AVG_ENTRY_PRICE"] + atr_now * cfg["SCALE_IN_ATR_STEP"] and sig["confirm_long"]:
            old_avg = state["AVG_ENTRY_PRICE"]
            state["AVG_ENTRY_PRICE"] = (old_avg * state["SCALE_COUNT"] + price) / (state["SCALE_COUNT"] + 1)
            state["SCALE_COUNT"] += 1
            state["LAST_SCALE_TIME"] = time.time()
            send(f"➕ {asset_key} LONG SCALE-IN\nNew avg: ${state['AVG_ENTRY_PRICE']:.2f}\nScale: {state['SCALE_COUNT']}/{MAX_SCALE_INS}")

    if state["TRADE_SIDE"] == "SHORT":
        if price <= state["AVG_ENTRY_PRICE"] - atr_now * cfg["SCALE_IN_ATR_STEP"] and sig["confirm_short"]:
            old_avg = state["AVG_ENTRY_PRICE"]
            state["AVG_ENTRY_PRICE"] = (old_avg * state["SCALE_COUNT"] + price) / (state["SCALE_COUNT"] + 1)
            state["SCALE_COUNT"] += 1
            state["LAST_SCALE_TIME"] = time.time()
            send(f"➕ {asset_key} SHORT SCALE-IN\nNew avg: ${state['AVG_ENTRY_PRICE']:.2f}\nScale: {state['SCALE_COUNT']}/{MAX_SCALE_INS}")

def maybe_send_trailing_update(asset_key: str, new_sl: float, atr_now: float):
    state = STATE[asset_key]
    min_step = atr_now * ASSET_CONFIG[asset_key]["TRAIL_UPDATE_MIN_ATR"]

    if state["LAST_TRAIL_SENT_SL"] == 0.0 or abs(new_sl - state["LAST_TRAIL_SENT_SL"]) >= min_step:
        state["LAST_TRAIL_SENT_SL"] = new_sl
        send(f"📈 {asset_key} {state['TRADE_SIDE']} TRAILING STOP\nNew SL: ${new_sl:.2f}")

def manage_trade(asset_key: str, sig: dict):
    state = STATE[asset_key]
    cfg = ASSET_CONFIG[asset_key]
    name = ASSETS[asset_key]["name"]

    price = sig["price"]
    atr_now = sig["atr"]
    entry = state["AVG_ENTRY_PRICE"] if state["AVG_ENTRY_PRICE"] > 0 else state["ENTRY_PRICE"]

    maybe_scale_in(asset_key, sig)

    if state["TRADE_SIDE"] == "LONG":
        state["HIGHEST_PRICE"] = max(state["HIGHEST_PRICE"], price)

        if not state["BREAK_EVEN_ACTIVE"] and price >= entry + atr_now * cfg["BREAK_EVEN_ATR_TRIGGER"]:
            state["STOP_LOSS"] = max(state["STOP_LOSS"], entry)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"⚡ {name} LONG BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if not state["PARTIAL_SENT"] and price >= entry + atr_now * cfg["PARTIAL_ATR_TRIGGER"]:
            state["PARTIAL_SENT"] = True
            send(f"💰 {name} LONG PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if state["BREAK_EVEN_ACTIVE"] and price > entry + atr_now * cfg["TRAILING_ACTIVATION_ATR"]:
            new_sl = state["HIGHEST_PRICE"] - atr_now * cfg["ATR_TRAIL_MULT"]
            if new_sl > state["STOP_LOSS"]:
                state["STOP_LOSS"] = new_sl
                maybe_send_trailing_update(asset_key, new_sl, atr_now)

        if price <= state["STOP_LOSS"]:
            send(f"❌ {name} LONG STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price >= state["TAKE_PROFIT"]:
            send(f"🎯 {name} LONG TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

    if state["TRADE_SIDE"] == "SHORT":
        state["LOWEST_PRICE"] = min(state["LOWEST_PRICE"], price)

        if not state["BREAK_EVEN_ACTIVE"] and price <= entry - atr_now * cfg["BREAK_EVEN_ATR_TRIGGER"]:
            state["STOP_LOSS"] = min(state["STOP_LOSS"], entry)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"⚡ {name} SHORT BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if not state["PARTIAL_SENT"] and price <= entry - atr_now * cfg["PARTIAL_ATR_TRIGGER"]:
            state["PARTIAL_SENT"] = True
            send(f"💰 {name} SHORT PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if state["BREAK_EVEN_ACTIVE"] and price < entry - atr_now * cfg["TRAILING_ACTIVATION_ATR"]:
            new_sl = state["LOWEST_PRICE"] + atr_now * cfg["ATR_TRAIL_MULT"]
            if new_sl < state["STOP_LOSS"]:
                state["STOP_LOSS"] = new_sl
                maybe_send_trailing_update(asset_key, new_sl, atr_now)

        if price >= state["STOP_LOSS"]:
            send(f"❌ {name} SHORT STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price <= state["TAKE_PROFIT"]:
            send(f"🎯 {name} SHORT TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

# =========================
# MAIN LOOP
# =========================
def run():
    time.sleep(5)
    send(f"🔥 BTC + GOLD BOT LIVE 🔥\nTime: {time.strftime('%H:%M:%S')}")

    while True:
        try:
            for asset_key in ASSETS:
                sig = get_signal(asset_key)
                heartbeat(asset_key, sig)

                if DEBUG_MODE:
                    if sig is None:
                        print(f"{asset_key} | NO SIGNAL DATA")
                    else:
                        print(
                            f"{asset_key} | Trend: {sig['trend']} | HTF: {sig['htf_bias']} | "
                            f"L:{sig['long_score']} S:{sig['short_score']} | Feed:{sig['data_source']}"
                        )

                if sig is None:
                    continue

                if STATE[asset_key]["IN_TRADE"]:
                    manage_trade(asset_key, sig)
                    continue

                if time.time() - STATE[asset_key]["LAST_TRADE_TIME"] < COOLDOWN_SECONDS:
                    continue

                if sig["trend"] == "CHOPPY":
                    continue

                if not clean_candle(asset_key, sig["df1"]):
                    continue

                if (
                    sig["long_score"] >= A_SETUP_SCORE
                    and sig["trend"] == "BULLISH"
                    and sig["htf_bias"] in ["BULL", "STRONG_BULL"]
                    and sig["confirm_long"]
                    and good_long_location(asset_key, sig["df1"])
                    and (sig["long_pullback"] or sig["long_breakout"] or sig["long_sniper"])
                ):
                    trigger = "A SETUP"
                    if sig["long_breakout"]:
                        trigger = "BREAKOUT"
                    elif sig["long_sniper"]:
                        trigger = "SNIPER"

                    start_trade(
                        asset_key,
                        "LONG",
                        trigger,
                        sig["long_score"],
                        sig["long_reasons"],
                        sig["price"],
                        sig["atr"],
                    )

                elif (
                    sig["short_score"] >= A_SETUP_SCORE
                    and sig["trend"] == "BEARISH"
                    and sig["htf_bias"] in ["BEAR", "STRONG_BEAR"]
                    and sig["confirm_short"]
                    and good_short_location(asset_key, sig["df1"])
                    and (sig["short_pullback"] or sig["short_breakout"] or sig["short_sniper"])
                ):
                    trigger = "A SETUP"
                    if sig["short_breakout"]:
                        trigger = "BREAKOUT"
                    elif sig["short_sniper"]:
                        trigger = "SNIPER"

                    start_trade(
                        asset_key,
                        "SHORT",
                        trigger,
                        sig["short_score"],
                        sig["short_reasons"],
                        sig["price"],
                        sig["atr"],
                    )

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"BOT ERROR: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run()
