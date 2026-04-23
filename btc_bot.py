import os
import time
import requests
import pandas as pd
import yfinance as yf

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 900
LONG_SCORE_THRESHOLD = 70
SHORT_SCORE_THRESHOLD = 70
DEBUG = True

ASSETS = {
    "BTC": "BTC-USD",
    "GOLD": "GC=F",
}

STATE = {
    a: {
        "in_trade": False,
        "side": None,
        "entry": 0.0,
        "sl": 0.0,
        "tp": 0.0,
        "highest": 0.0,
        "lowest": 0.0,
        "last_heartbeat": 0.0,
        "break_even": False,
        "partial": False,
    }
    for a in ASSETS
}

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
        except Exception:
            time.sleep(2)

def get_data(symbol: str, interval: str) -> pd.DataFrame:
    try:
        period_map = {
            "1m": "7d",
            "5m": "7d",
            "15m": "30d",
        }
        df = yf.download(
            symbol,
            period=period_map.get(interval, "7d"),
            interval=interval,
            progress=False,
            auto_adjust=False,
        )
        if df is None or df.empty:
            return pd.DataFrame()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [str(c).lower() for c in df.columns]
        needed = ["open", "high", "low", "close", "volume"]
        if any(c not in df.columns for c in needed):
            return pd.DataFrame()

        df = df[needed].copy()
        for c in needed:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.dropna(inplace=True)
        return df
    except Exception:
        return pd.DataFrame()

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 30:
        return pd.DataFrame()

    out = df.copy()
    out["ema9"] = out["close"].ewm(span=9).mean()
    out["ema21"] = out["close"].ewm(span=21).mean()
    out["ema50"] = out["close"].ewm(span=50).mean()

    delta = out["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = delta.clip(upper=0).abs().rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    out["rsi"] = 100 - (100 / (1 + rs))

    out["atr"] = (out["high"] - out["low"]).rolling(14).mean()
    out["vol_ma"] = out["volume"].rolling(20).mean()
    out["hh10"] = out["high"].rolling(10).max().shift(1)
    out["ll10"] = out["low"].rolling(10).min().shift(1)
    out.dropna(inplace=True)
    return out

def trend(df1: pd.DataFrame, df5: pd.DataFrame) -> str:
    r1 = df1.iloc[-1]
    r5 = df5.iloc[-1]

    if r5["ema9"] > r5["ema21"] and r1["ema9"] > r1["ema21"]:
        return "BULL"
    if r5["ema9"] < r5["ema21"] and r1["ema9"] < r1["ema21"]:
        return "BEAR"
    return "CHOP"

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

def clean_entry(df: pd.DataFrame) -> bool:
    r = df.iloc[-1]
    p = df.iloc[-2]
    move = abs(float(r["close"]) - float(p["close"]))

    if float(r["atr"]) > 0 and move > float(r["atr"]) * 1.2:
        return False

    return True

def breakout_long(df: pd.DataFrame) -> bool:
    r = df.iloc[-1]
    return float(r["close"]) > float(df["hh10"].iloc[-1]) * 1.0005

def breakout_short(df: pd.DataFrame) -> bool:
    r = df.iloc[-1]
    return float(r["close"]) < float(df["ll10"].iloc[-1]) * 0.9995

def pullback_long(df: pd.DataFrame) -> bool:
    r = df.iloc[-1]
    return float(r["close"]) <= float(r["ema9"]) * 1.002

def pullback_short(df: pd.DataFrame) -> bool:
    r = df.iloc[-1]
    return float(r["close"]) >= float(r["ema9"]) * 0.998

def long_score(df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame) -> int:
    r = df1.iloc[-1]
    score = 0

    local_trend = trend(df1, df5)
    higher_bias = htf_bias(df15)

    if local_trend == "BULL":
        score += 25
    if higher_bias == "STRONG_BULL":
        score += 25
    elif higher_bias == "BULL":
        score += 15
    elif higher_bias in ["BEAR", "STRONG_BEAR"]:
        score -= 20

    if r["ema9"] > r["ema21"]:
        score += 15

    if 50 < r["rsi"] < 68:
        score += 15
    elif 48 < r["rsi"] < 72:
        score += 8

    if r["volume"] > r["vol_ma"] * 1.1:
        score += 10

    if r["close"] > r["ema9"]:
        score += 10

    return max(0, score)

def short_score(df1: pd.DataFrame, df5: pd.DataFrame, df15: pd.DataFrame) -> int:
    r = df1.iloc[-1]
    score = 0

    local_trend = trend(df1, df5)
    higher_bias = htf_bias(df15)

    if local_trend == "BEAR":
        score += 25
    if higher_bias == "STRONG_BEAR":
        score += 25
    elif higher_bias == "BEAR":
        score += 15
    elif higher_bias in ["BULL", "STRONG_BULL"]:
        score -= 20

    if r["ema9"] < r["ema21"]:
        score += 15

    if 32 < r["rsi"] < 50:
        score += 15
    elif 28 < r["rsi"] < 54:
        score += 8

    if r["volume"] > r["vol_ma"] * 1.1:
        score += 10

    if r["close"] < r["ema9"]:
        score += 10

    return max(0, score)

def heartbeat(asset: str, price, local_trend_text=None, htf_bias_text=None):
    now = time.time()
    s = STATE[asset]

    if now - s["last_heartbeat"] < HEARTBEAT_SECONDS:
        return

    if price is None:
        send(f"💓 {asset} HEARTBEAT\nNo data")
    else:
        msg = f"💓 {asset} HEARTBEAT\nPrice: {round(float(price), 2)}"
        if local_trend_text:
            msg += f"\nTrend: {local_trend_text}"
        if htf_bias_text:
            msg += f"\nHTF Bias: {htf_bias_text}"
        send(msg)

    s["last_heartbeat"] = now

def manage(asset: str, price: float, atr: float):
    s = STATE[asset]

    if not s["in_trade"]:
        return

    if s["side"] == "LONG":
        s["highest"] = max(s["highest"], price)

        if not s["break_even"] and price >= s["entry"] + atr:
            s["sl"] = s["entry"]
            s["break_even"] = True
            send(f"⚡ {asset} LONG BREAK-EVEN\nNew SL: {round(s['sl'], 2)}")

        if not s["partial"] and price >= s["entry"] + (atr * 1.5):
            s["partial"] = True
            send(f"💰 {asset} LONG PARTIAL PROFIT ZONE\nPrice: {round(price, 2)}")

        trail = s["highest"] - atr * 1.5
        if trail > s["sl"]:
            s["sl"] = trail
            send(f"📈 {asset} LONG TRAILING STOP\nNew SL: {round(s['sl'], 2)}")

        if price <= s["sl"]:
            send(f"❌ {asset} LONG EXIT\nPrice: {round(price, 2)}")
            s["in_trade"] = False
            s["side"] = None

        elif price >= s["tp"]:
            send(f"🎯 {asset} LONG TARGET HIT\nPrice: {round(price, 2)}")
            s["in_trade"] = False
            s["side"] = None

    else:
        s["lowest"] = min(s["lowest"], price)

        if not s["break_even"] and price <= s["entry"] - atr:
            s["sl"] = s["entry"]
            s["break_even"] = True
            send(f"⚡ {asset} SHORT BREAK-EVEN\nNew SL: {round(s['sl'], 2)}")

        if not s["partial"] and price <= s["entry"] - (atr * 1.5):
            s["partial"] = True
            send(f"💰 {asset} SHORT PARTIAL PROFIT ZONE\nPrice: {round(price, 2)}")

        trail = s["lowest"] + atr * 1.5
        if trail < s["sl"]:
            s["sl"] = trail
            send(f"📉 {asset} SHORT TRAILING STOP\nNew SL: {round(s['sl'], 2)}")

        if price >= s["sl"]:
            send(f"❌ {asset} SHORT EXIT\nPrice: {round(price, 2)}")
            s["in_trade"] = False
            s["side"] = None

        elif price <= s["tp"]:
            send(f"🎯 {asset} SHORT TARGET HIT\nPrice: {round(price, 2)}")
            s["in_trade"] = False
            s["side"] = None

def run():
    time.sleep(5)
    send("🔥 BTC + GOLD BOT LIVE 🔥")

    while True:
        try:
            for asset, symbol in ASSETS.items():
                df1 = add_indicators(get_data(symbol, "1m"))
                df5 = add_indicators(get_data(symbol, "5m"))
                df15 = add_indicators(get_data(symbol, "15m"))

                if df1.empty or df5.empty or df15.empty:
                    heartbeat(asset, None)
                    continue

                price = float(df1.iloc[-1]["close"])
                atr = float(df1.iloc[-1]["atr"])
                local_trend_text = trend(df1, df5)
                htf_bias_text = htf_bias(df15)

                heartbeat(asset, price, local_trend_text, htf_bias_text)
                manage(asset, price, atr)

                if STATE[asset]["in_trade"]:
                    continue

                if not clean_entry(df1):
                    continue

                long_s = long_score(df1, df5, df15)
                short_s = short_score(df1, df5, df15)

                if DEBUG:
                    print(
                        asset,
                        "Trend:", local_trend_text,
                        "HTF:", htf_bias_text,
                        "L:", long_s,
                        "S:", short_s,
                    )

                if (
                    long_s >= LONG_SCORE_THRESHOLD
                    and (pullback_long(df1) or breakout_long(df1))
                    and local_trend_text == "BULL"
                    and htf_bias_text in ["BULL", "STRONG_BULL"]
                ):
                    STATE[asset].update({
                        "in_trade": True,
                        "side": "LONG",
                        "entry": price,
                        "sl": price - atr * 1.5,
                        "tp": price + atr * 3.0,
                        "highest": price,
                        "lowest": price,
                        "break_even": False,
                        "partial": False,
                    })
                    send(
                        f"🚀 {asset} LONG ENTRY\n"
                        f"Price: {round(price, 2)}\n"
                        f"Score: {long_s}\n"
                        f"Trend: {local_trend_text}\n"
                        f"HTF Bias: {htf_bias_text}\n"
                        f"SL: {round(price - atr * 1.5, 2)}\n"
                        f"TP: {round(price + atr * 3.0, 2)}"
                    )

                elif (
                    short_s >= SHORT_SCORE_THRESHOLD
                    and (pullback_short(df1) or breakout_short(df1))
                    and local_trend_text == "BEAR"
                    and htf_bias_text in ["BEAR", "STRONG_BEAR"]
                ):
                    STATE[asset].update({
                        "in_trade": True,
                        "side": "SHORT",
                        "entry": price,
                        "sl": price + atr * 1.5,
                        "tp": price - atr * 3.0,
                        "highest": price,
                        "lowest": price,
                        "break_even": False,
                        "partial": False,
                    })
                    send(
                        f"📉 {asset} SHORT ENTRY\n"
                        f"Price: {round(price, 2)}\n"
                        f"Score: {short_s}\n"
                        f"Trend: {local_trend_text}\n"
                        f"HTF Bias: {htf_bias_text}\n"
                        f"SL: {round(price + atr * 1.5, 2)}\n"
                        f"TP: {round(price - atr * 3.0, 2)}"
                    )

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
