import os
import time
import requests
import pandas as pd
import yfinance as yf

# ============================================================
# BTC + GOLD SMC BOT
# Logic:
# - Trend continuation only after healthy pullbacks
# - Breakout + retest confirmation
# - Fair Value Gap bounce/rejection
# - Order Block bounce/rejection
# - Candle reversal confirmation
# - ATR-based realistic SL / TP
# - Anti-chase filters
# - Same-zone re-entry protection
# - Telegram live + 30 minute heartbeat
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 1800
COOLDOWN_SECONDS = 1800
MIN_SCORE = 78
FULL_SCORE = 90
DEBUG_MODE = True

ASSETS = {
    "BTC": {"name": "BTC", "binance": "BTCUSDT", "yf": "BTC-USD"},
    "GOLD": {"name": "GOLD", "binance": None, "yf": "GC=F"},
}

CFG = {
    "BTC": {
        "SL_ATR": 3.0, "TP1_ATR": 3.0, "TP2_ATR": 6.5,
        "BE_ATR": 2.2, "TRAIL_ATR": 2.8, "TRAIL_START_ATR": 3.5,
        "MIN_VOL": 0.0007, "MAX_CHASE": 0.0045,
        "MIN_PULL_ATR": 0.70, "MAX_PULL_ATR": 3.20,
        "MAX_CANDLE_ATR": 1.20,
        "RSI_LONG_MIN": 42, "RSI_LONG_MAX": 66,
        "RSI_SHORT_MIN": 34, "RSI_SHORT_MAX": 58,
        "BOS_LOOKBACK": 20, "FVG_LOOKBACK": 45, "OB_LOOKBACK": 45,
        "SAME_ZONE": 0.0030,
    },
    "GOLD": {
        "SL_ATR": 2.6, "TP1_ATR": 2.8, "TP2_ATR": 5.8,
        "BE_ATR": 2.0, "TRAIL_ATR": 2.5, "TRAIL_START_ATR": 3.2,
        "MIN_VOL": 0.00012, "MAX_CHASE": 0.0030,
        "MIN_PULL_ATR": 0.70, "MAX_PULL_ATR": 2.80,
        "MAX_CANDLE_ATR": 1.10,
        "RSI_LONG_MIN": 42, "RSI_LONG_MAX": 64,
        "RSI_SHORT_MIN": 36, "RSI_SHORT_MAX": 58,
        "BOS_LOOKBACK": 20, "FVG_LOOKBACK": 45, "OB_LOOKBACK": 45,
        "SAME_ZONE": 0.0020,
    },
}

STATE = {
    k: {
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
        "LAST_TRADE_TIME": 0.0,
        "LAST_HEARTBEAT": 0.0,
        "LAST_ENTRY_PRICE": 0.0,
        "LAST_ENTRY_SIDE": None,
        "LAST_TRAIL_SL": 0.0,
    }
    for k in ASSETS
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
# DATA
# ============================================================
def normalize(df):
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
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(inplace=True)
    return df.reset_index(drop=True)


def get_binance(symbol, interval, limit=500):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data = r.json()
        if not isinstance(data, list) or len(data) < 80:
            return pd.DataFrame()
        df = pd.DataFrame(
            data,
            columns=["time", "open", "high", "low", "close", "volume",
                     "ct", "qav", "trades", "tbv", "tqv", "ignore"]
        )
        return normalize(df)
    except Exception as e:
        print("BINANCE ERROR:", e)
        return pd.DataFrame()


def get_yf(symbol, interval):
    try:
        period = {"1m": "7d", "5m": "30d", "15m": "60d"}[interval]
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return normalize(df)
    except Exception as e:
        print("YFINANCE ERROR:", e)
        return pd.DataFrame()


def get_klines(asset, interval):
    a = ASSETS[asset]
    if asset == "BTC":
        df = get_binance(a["binance"], interval)
        if not df.empty:
            return df, "BINANCE"
    df = get_yf(a["yf"], interval)
    if not df.empty:
        return df, "YFINANCE"
    return pd.DataFrame(), "NONE"

# ============================================================
# INDICATORS
# ============================================================
def add_indicators(df):
    if df.empty or len(df) < 80:
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

    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - out["close"].shift()).abs(),
        (out["low"] - out["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.rolling(14).mean()

    out["body"] = (out["close"] - out["open"]).abs()
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["move"] = (out["close"] - out["close"].shift(1)).abs()
    out["vol_ma"] = out["volume"].rolling(20).mean()

    out.dropna(inplace=True)
    return out.reset_index(drop=True)

# ============================================================
# STRUCTURE / BIAS
# ============================================================
def bias(df5, df15):
    r5 = df5.iloc[-1]
    r15 = df15.iloc[-1]

    bull = r15["ema9"] > r15["ema21"] > r15["ema50"] and r5["ema9"] > r5["ema21"] and r5["close"] > r5["ema50"]
    bear = r15["ema9"] < r15["ema21"] < r15["ema50"] and r5["ema9"] < r5["ema21"] and r5["close"] < r5["ema50"]

    if bull:
        return "BULLISH"
    if bear:
        return "BEARISH"
    if r15["ema9"] > r15["ema21"]:
        return "BULLISH_WEAK"
    if r15["ema9"] < r15["ema21"]:
        return "BEARISH_WEAK"
    return "CHOPPY"


def swing_high(df, lookback):
    return float(df["high"].iloc[-lookback-1:-1].max())


def swing_low(df, lookback):
    return float(df["low"].iloc[-lookback-1:-1].min())


def bos_long(df, c):
    r = df.iloc[-1]
    return bool(r["close"] > swing_high(df, c["BOS_LOOKBACK"]) and r["close"] > r["ema9"])


def bos_short(df, c):
    r = df.iloc[-1]
    return bool(r["close"] < swing_low(df, c["BOS_LOOKBACK"]) and r["close"] < r["ema9"])


def breakout_retest_long(df, c):
    r = df.iloc[-1]
    p = df.iloc[-2]
    sh = swing_high(df.iloc[:-1], c["BOS_LOOKBACK"])
    return bool(p["close"] > sh and r["low"] <= sh and r["close"] > sh and r["close"] > r["open"])


def breakout_retest_short(df, c):
    r = df.iloc[-1]
    p = df.iloc[-2]
    sl = swing_low(df.iloc[:-1], c["BOS_LOOKBACK"])
    return bool(p["close"] < sl and r["high"] >= sl and r["close"] < sl and r["close"] < r["open"])

# ============================================================
# FVG
# ============================================================
def find_bull_fvg(df, lookback):
    start = max(2, len(df) - lookback)
    for i in range(len(df) - 2, start, -1):
        a = df.iloc[i - 2]
        c = df.iloc[i]
        if c["low"] > a["high"]:
            return {"low": float(a["high"]), "high": float(c["low"])}
    return None


def find_bear_fvg(df, lookback):
    start = max(2, len(df) - lookback)
    for i in range(len(df) - 2, start, -1):
        a = df.iloc[i - 2]
        c = df.iloc[i]
        if c["high"] < a["low"]:
            return {"low": float(c["high"]), "high": float(a["low"])}
    return None


def fvg_bounce_long(df, c):
    zone = find_bull_fvg(df, c["FVG_LOOKBACK"])
    if not zone:
        return False
    r = df.iloc[-1]
    touched = r["low"] <= zone["high"] and r["close"] >= zone["low"]
    rejected = r["close"] > r["open"] and r["lower_wick"] >= r["body"] * 0.4
    return bool(touched and rejected and r["close"] > r["ema9"])


def fvg_reject_short(df, c):
    zone = find_bear_fvg(df, c["FVG_LOOKBACK"])
    if not zone:
        return False
    r = df.iloc[-1]
    touched = r["high"] >= zone["low"] and r["close"] <= zone["high"]
    rejected = r["close"] < r["open"] and r["upper_wick"] >= r["body"] * 0.4
    return bool(touched and rejected and r["close"] < r["ema9"])

# ============================================================
# ORDER BLOCKS
# ============================================================
def find_bull_ob(df, c):
    start = max(3, len(df) - c["OB_LOOKBACK"])
    for i in range(len(df) - 3, start, -1):
        candle = df.iloc[i]
        n1 = df.iloc[i + 1]
        n2 = df.iloc[i + 2]
        if candle["close"] < candle["open"] and n1["close"] > n1["open"] and n2["close"] > candle["high"]:
            return {"low": float(candle["low"]), "high": float(candle["high"])}
    return None


def find_bear_ob(df, c):
    start = max(3, len(df) - c["OB_LOOKBACK"])
    for i in range(len(df) - 3, start, -1):
        candle = df.iloc[i]
        n1 = df.iloc[i + 1]
        n2 = df.iloc[i + 2]
        if candle["close"] > candle["open"] and n1["close"] < n1["open"] and n2["close"] < candle["low"]:
            return {"low": float(candle["low"]), "high": float(candle["high"])}
    return None


def ob_bounce_long(df, c):
    zone = find_bull_ob(df, c)
    if not zone:
        return False
    r = df.iloc[-1]
    touched = r["low"] <= zone["high"] and r["close"] >= zone["low"]
    rejected = r["close"] > r["open"] and r["lower_wick"] >= r["body"] * 0.5
    return bool(touched and rejected and r["close"] > r["ema9"])


def ob_reject_short(df, c):
    zone = find_bear_ob(df, c)
    if not zone:
        return False
    r = df.iloc[-1]
    touched = r["high"] >= zone["low"] and r["close"] <= zone["high"]
    rejected = r["close"] < r["open"] and r["upper_wick"] >= r["body"] * 0.5
    return bool(touched and rejected and r["close"] < r["ema9"])

# ============================================================
# PULLBACK + CANDLE LOGIC
# ============================================================
def bullish_reversal(df):
    r = df.iloc[-1]
    p = df.iloc[-2]
    engulf = r["close"] > r["open"] and r["close"] > p["open"] and r["open"] < p["close"]
    pin = r["lower_wick"] > r["body"] * 1.4 and r["close"] > r["open"]
    return bool((engulf or pin) and r["close"] > r["ema9"])


def bearish_reversal(df):
    r = df.iloc[-1]
    p = df.iloc[-2]
    engulf = r["close"] < r["open"] and r["close"] < p["open"] and r["open"] > p["close"]
    pin = r["upper_wick"] > r["body"] * 1.4 and r["close"] < r["open"]
    return bool((engulf or pin) and r["close"] < r["ema9"])


def healthy_pullback_long(asset, df1, df5):
    c = CFG[asset]
    r = df1.iloc[-1]
    depth = swing_high(df1, 5) - r["low"]
    if r["atr"] <= 0:
        return False
    depth_atr = depth / r["atr"]
    good_depth = c["MIN_PULL_ATR"] <= depth_atr <= c["MAX_PULL_ATR"]
    near_value = r["low"] <= r["ema21"] or r["low"] <= df5.iloc[-1]["ema9"]
    confirmation = r["close"] > r["ema9"] and r["close"] > r["open"]
    return bool(good_depth and near_value and confirmation)


def healthy_pullback_short(asset, df1, df5):
    c = CFG[asset]
    r = df1.iloc[-1]
    depth = r["high"] - swing_low(df1, 5)
    if r["atr"] <= 0:
        return False
    depth_atr = depth / r["atr"]
    good_depth = c["MIN_PULL_ATR"] <= depth_atr <= c["MAX_PULL_ATR"]
    near_value = r["high"] >= r["ema21"] or r["high"] >= df5.iloc[-1]["ema9"]
    confirmation = r["close"] < r["ema9"] and r["close"] < r["open"]
    return bool(good_depth and near_value and confirmation)


def not_chasing(asset, df, side):
    c = CFG[asset]
    r = df.iloc[-1]
    if r["atr"] <= 0:
        return False
    if abs(r["close"] - r["ema9"]) / max(r["close"], 1.0) > c["MAX_CHASE"]:
        return False
    if r["move"] > r["atr"] * c["MAX_CANDLE_ATR"]:
        return False
    if side == "LONG":
        return c["RSI_LONG_MIN"] <= r["rsi"] <= c["RSI_LONG_MAX"]
    return c["RSI_SHORT_MIN"] <= r["rsi"] <= c["RSI_SHORT_MAX"]


def same_zone_block(asset, side, price):
    s = STATE[asset]
    last = s["LAST_ENTRY_PRICE"]
    if last <= 0 or s["LAST_ENTRY_SIDE"] != side:
        return False
    return abs(price - last) / max(price, 1.0) < CFG[asset]["SAME_ZONE"]

# ============================================================
# SCORING
# ============================================================
def score_long(asset, df1, df5, df15):
    c = CFG[asset]
    r = df1.iloc[-1]
    b = bias(df5, df15)
    score = 0
    reasons = []

    if b == "BULLISH":
        score += 25; reasons.append("HTF bullish")
    elif b == "BULLISH_WEAK":
        score += 12; reasons.append("HTF weak bullish")
    elif b in ["BEARISH", "BEARISH_WEAK"]:
        score -= 25; reasons.append("HTF against")

    checks = [
        (bos_long(df1, c), 15, "BOS up"),
        (breakout_retest_long(df1, c), 25, "breakout retest"),
        (healthy_pullback_long(asset, df1, df5), 25, "healthy pullback"),
        (fvg_bounce_long(df1, c), 24, "FVG bounce"),
        (ob_bounce_long(df1, c), 24, "OB bounce"),
        (bullish_reversal(df1), 12, "bull candle"),
        (r["ema9"] > r["ema21"], 10, "EMA aligned"),
        (c["RSI_LONG_MIN"] <= r["rsi"] <= c["RSI_LONG_MAX"], 10, "RSI healthy"),
    ]

    for ok, pts, reason in checks:
        if ok:
            score += pts
            reasons.append(reason)

    if not not_chasing(asset, df1, "LONG"):
        score -= 35
        reasons.append("chase blocked")

    return max(0, min(int(score), 100)), reasons, b


def score_short(asset, df1, df5, df15):
    c = CFG[asset]
    r = df1.iloc[-1]
    b = bias(df5, df15)
    score = 0
    reasons = []

    if b == "BEARISH":
        score += 25; reasons.append("HTF bearish")
    elif b == "BEARISH_WEAK":
        score += 12; reasons.append("HTF weak bearish")
    elif b in ["BULLISH", "BULLISH_WEAK"]:
        score -= 25; reasons.append("HTF against")

    checks = [
        (bos_short(df1, c), 15, "BOS down"),
        (breakout_retest_short(df1, c), 25, "breakdown retest"),
        (healthy_pullback_short(asset, df1, df5), 25, "healthy pullback"),
        (fvg_reject_short(df1, c), 24, "FVG reject"),
        (ob_reject_short(df1, c), 24, "OB reject"),
        (bearish_reversal(df1), 12, "bear candle"),
        (r["ema9"] < r["ema21"], 10, "EMA aligned"),
        (c["RSI_SHORT_MIN"] <= r["rsi"] <= c["RSI_SHORT_MAX"], 10, "RSI healthy"),
    ]

    for ok, pts, reason in checks:
        if ok:
            score += pts
            reasons.append(reason)

    if not not_chasing(asset, df1, "SHORT"):
        score -= 35
        reasons.append("chase blocked")

    return max(0, min(int(score), 100)), reasons, b


def confidence(score):
    if score >= 95: return "S"
    if score >= 88: return "A+"
    if score >= 80: return "A"
    if score >= 75: return "B+"
    return "B"

# ============================================================
# SIGNAL
# ============================================================
def get_signal(asset):
    df1_raw, src1 = get_klines(asset, "1m")
    df5_raw, src5 = get_klines(asset, "5m")
    df15_raw, src15 = get_klines(asset, "15m")

    df1 = add_indicators(df1_raw)
    df5 = add_indicators(df5_raw)
    df15 = add_indicators(df15_raw)

    sources = [s for s in [src1, src5, src15] if s != "NONE"]
    feed = "/".join(sorted(set(sources))) if sources else "NONE"

    if df1.empty or df5.empty or df15.empty:
        return None, feed

    r = df1.iloc[-1]
    price = float(r["close"])
    atr = float(r["atr"])

    if atr <= 0 or (atr / max(price, 1.0)) < CFG[asset]["MIN_VOL"]:
        return None, feed

    ls, lr, b = score_long(asset, df1, df5, df15)
    ss, sr, _ = score_short(asset, df1, df5, df15)

    return {
        "asset": asset,
        "price": price,
        "atr": atr,
        "df1": df1,
        "df5": df5,
        "df15": df15,
        "bias": b,
        "long_score": ls,
        "short_score": ss,
        "long_reasons": lr,
        "short_reasons": sr,
        "feed": feed,
    }, feed

# ============================================================
# HEARTBEAT
# ============================================================
def heartbeat(asset, sig, feed):
    s = STATE[asset]
    if time.time() - s["LAST_HEARTBEAT"] < HEARTBEAT_SECONDS:
        return

    if sig is None:
        send(
            f"ð {asset} HEARTBEAT\n\n"
            f"Status: NO DATA / WAITING\n"
            f"In trade: {'YES' if s['IN_TRADE'] else 'NO'}\n"
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
            f"In trade: {'YES' if s['IN_TRADE'] else 'NO'}\n"
            f"Feed: {sig['feed']}"
        )

    s["LAST_HEARTBEAT"] = time.time()

# ============================================================
# TRADE
# ============================================================
def start_trade(asset, side, sig):
    s = STATE[asset]
    c = CFG[asset]
    price = sig["price"]
    atr = sig["atr"]

    s["IN_TRADE"] = True
    s["SIDE"] = side
    s["ENTRY"] = price
    s["HIGH"] = price
    s["LOW"] = price
    s["TP1_SENT"] = False
    s["BE_ACTIVE"] = False
    s["LAST_ENTRY_PRICE"] = price
    s["LAST_ENTRY_SIDE"] = side
    s["LAST_TRAIL_SL"] = 0.0

    if side == "LONG":
        score = sig["long_score"]
        reasons = sig["long_reasons"]
        s["SL"] = price - atr * c["SL_ATR"]
        s["TP1"] = price + atr * c["TP1_ATR"]
        s["TP2"] = price + atr * c["TP2_ATR"]
        icon = "ð"
    else:
        score = sig["short_score"]
        reasons = sig["short_reasons"]
        s["SL"] = price + atr * c["SL_ATR"]
        s["TP1"] = price - atr * c["TP1_ATR"]
        s["TP2"] = price - atr * c["TP2_ATR"]
        icon = "ð"

    send(
        f"{icon} {asset} {side} ENTRY\n\n"
        f"Style: SMC CONFIRMATION\n"
        f"Size: {'FULL' if score >= FULL_SCORE else 'SNIPER'}\n"
        f"Confidence: {confidence(score)}\n"
        f"Price: ${price:.2f}\n"
        f"Score: {score}\n"
        f"Reasons: {', '.join(reasons[:5])}\n\n"
        f"SL: ${s['SL']:.2f}\n"
        f"TP1: ${s['TP1']:.2f}\n"
        f"TP2: ${s['TP2']:.2f}"
    )


def reset_trade(asset):
    s = STATE[asset]
    s["IN_TRADE"] = False
    s["SIDE"] = None
    s["ENTRY"] = 0.0
    s["SL"] = 0.0
    s["TP1"] = 0.0
    s["TP2"] = 0.0
    s["TP1_SENT"] = False
    s["BE_ACTIVE"] = False
    s["HIGH"] = 0.0
    s["LOW"] = 0.0
    s["LAST_TRADE_TIME"] = time.time()
    s["LAST_TRAIL_SL"] = 0.0


def manage_trade(asset, sig):
    s = STATE[asset]
    c = CFG[asset]
    price = sig["price"]
    atr = sig["atr"]
    entry = s["ENTRY"]

    if s["SIDE"] == "LONG":
        s["HIGH"] = max(s["HIGH"], price)

        if not s["BE_ACTIVE"] and price >= entry + atr * c["BE_ATR"]:
            s["SL"] = max(s["SL"], entry)
            s["BE_ACTIVE"] = True
            send(f"â¡ {asset} LONG BREAK-EVEN\nNew SL: ${s['SL']:.2f}")

        if not s["TP1_SENT"] and price >= s["TP1"]:
            s["TP1_SENT"] = True
            send(f"ð° {asset} LONG TP1 / PARTIAL ZONE\nPrice: ${price:.2f}")

        if price >= entry + atr * c["TRAIL_START_ATR"]:
            new_sl = s["HIGH"] - atr * c["TRAIL_ATR"]
            if new_sl > s["SL"]:
                s["SL"] = new_sl
                send(f"ð {asset} LONG TRAILING STOP\nNew SL: ${new_sl:.2f}")

        if price <= s["SL"]:
            send(f"â {asset} LONG STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset)
            return

        if price >= s["TP2"]:
            send(f"ð¯ {asset} LONG TP2 HIT\nExit: ${price:.2f}")
            reset_trade(asset)
            return

    elif s["SIDE"] == "SHORT":
        s["LOW"] = min(s["LOW"], price)

        if not s["BE_ACTIVE"] and price <= entry - atr * c["BE_ATR"]:
            s["SL"] = min(s["SL"], entry)
            s["BE_ACTIVE"] = True
            send(f"â¡ {asset} SHORT BREAK-EVEN\nNew SL: ${s['SL']:.2f}")

        if not s["TP1_SENT"] and price <= s["TP1"]:
            s["TP1_SENT"] = True
            send(f"ð° {asset} SHORT TP1 / PARTIAL ZONE\nPrice: ${price:.2f}")

        if price <= entry - atr * c["TRAIL_START_ATR"]:
            new_sl = s["LOW"] + atr * c["TRAIL_ATR"]
            if new_sl < s["SL"]:
                s["SL"] = new_sl
                send(f"ð {asset} SHORT TRAILING STOP\nNew SL: ${new_sl:.2f}")

        if price >= s["SL"]:
            send(f"â {asset} SHORT STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset)
            return

        if price <= s["TP2"]:
            send(f"ð¯ {asset} SHORT TP2 HIT\nExit: ${price:.2f}")
            reset_trade(asset)
            return

# ============================================================
# ENTRY DECISION
# ============================================================
def try_enter(asset, sig):
    s = STATE[asset]
    if s["IN_TRADE"]:
        return

    if time.time() - s["LAST_TRADE_TIME"] < COOLDOWN_SECONDS:
        return

    price = sig["price"]

    long_ok = (
        sig["long_score"] >= MIN_SCORE
        and sig["bias"] in ["BULLISH", "BULLISH_WEAK"]
        and not_chasing(asset, sig["df1"], "LONG")
        and not same_zone_block(asset, "LONG", price)
    )

    short_ok = (
        sig["short_score"] >= MIN_SCORE
        and sig["bias"] in ["BEARISH", "BEARISH_WEAK"]
        and not_chasing(asset, sig["df1"], "SHORT")
        and not same_zone_block(asset, "SHORT", price)
    )

    if long_ok and sig["long_score"] >= sig["short_score"]:
        start_trade(asset, "LONG", sig)

    elif short_ok and sig["short_score"] > sig["long_score"]:
        start_trade(asset, "SHORT", sig)

# ============================================================
# MAIN
# ============================================================
def run():
    time.sleep(8)
    send("â BOT STARTING...")
    time.sleep(2)
    send(f"ð¥ BTC + GOLD SMC BOT LIVE ð¥\nTime: {time.strftime('%H:%M:%S')}")

    while True:
        try:
            for asset in ASSETS:
                sig, feed = get_signal(asset)
                heartbeat(asset, sig, feed)

                if DEBUG_MODE:
                    if sig is None:
                        print(asset, "NO DATA / WAITING", feed)
                    else:
                        print(asset, sig["bias"], "L:", sig["long_score"], "S:", sig["short_score"], "FEED:", feed)

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
