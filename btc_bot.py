import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import requests
import time
import json
import os

from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange


# =========================
# TELEGRAM SETTINGS
# =========================

BOT_TOKEN = "PUT_YOUR_BOT_TOKEN_HERE"
CHAT_ID = "PUT_YOUR_CHAT_ID_HERE"

SEND_NO_SIGNAL_MESSAGE = False
PARAM_CACHE_FILE = "best_live_params.json"
SENT_SIGNAL_FILE = "sent_signals.json"


def send_telegram(message):
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE" or CHAT_ID == "PUT_YOUR_CHAT_ID_HERE":
        print("Telegram not configured.")
        print(message)
        return

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e)


# =========================
# SETTINGS
# =========================

TIMEFRAME = "1h"
HIGHER_TIMEFRAME = "4h"

BTC_LIMIT = 1500
BTC_HTF_LIMIT = 500

GOLD_PERIOD = "730d"
GOLD_HTF_PERIOD = "730d"

FEE = 0.0006
INITIAL_BALANCE = 10_000
RISK_PER_TRADE = 0.01

TRAIN_SIZE = 500
TEST_SIZE = 150
MONTE_CARLO_RUNS = 1000

LIVE_SCAN_SECONDS = 300

EXCHANGES = [
    ("coinbase", "BTC/USD"),
    ("kraken", "BTC/USD"),
    ("bybit", "BTC/USDT"),
    ("binanceus", "BTC/USDT"),
]


# =========================
# CACHE HELPERS
# =========================

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


# =========================
# DATA ENGINE
# =========================

def get_exchange(name):
    return getattr(ccxt, name)({
        "enableRateLimit": True,
        "timeout": 10000,
    })


def validate_data(df):
    if df is None or len(df) < 300:
        return False, "Not enough candles"

    if df.isna().sum().sum() > 0:
        return False, "NaN values detected"

    if "Volume" in df.columns and (df["Volume"] == 0).mean() > 0.20:
        return False, "Too many zero-volume candles"

    df = df.sort_values("timestamp").copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    diffs = df["timestamp"].diff().dropna()

    if len(diffs) == 0:
        return False, "Timestamp error"

    expected = diffs.mode()[0]
    gap_ratio = (diffs > expected * 1.5).mean()

    if gap_ratio > 0.10:
        return False, "Too many candle gaps"

    return True, "OK"


def fetch_btc(timeframe=TIMEFRAME, limit=BTC_LIMIT):
    for name, symbol in EXCHANGES:
        try:
            exchange = get_exchange(name)
            print(f"Trying BTC feed: {name} {timeframe}")

            candles = exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                limit=limit
            )

            df = pd.DataFrame(
                candles,
                columns=["timestamp", "Open", "High", "Low", "Close", "Volume"]
            )

            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

            valid, reason = validate_data(df)

            if not valid:
                print(f"{name} rejected: {reason}")
                continue

            print(f"Using BTC feed: {name} {timeframe}")
            return df

        except Exception as e:
            print(f"{name} failed: {e}")

    raise Exception("No valid BTC feed available")


def fetch_gold(interval=TIMEFRAME, period=GOLD_PERIOD):
    df = yf.download(
        "GC=F",
        period=period,
        interval=interval,
        auto_adjust=True,
        progress=False
    ).reset_index()

    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "timestamp"})
    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "timestamp"})

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df[["timestamp", "Open", "High", "Low", "Close", "Volume"]].dropna()

    valid, reason = validate_data(df)

    if not valid:
        raise Exception(f"Gold data invalid: {reason}")

    return df


# =========================
# INDICATORS
# =========================

def add_indicators(df):
    df = df.copy()

    df["ema_20"] = EMAIndicator(df["Close"], 20).ema_indicator()
    df["ema_50"] = EMAIndicator(df["Close"], 50).ema_indicator()
    df["ema_200"] = EMAIndicator(df["Close"], 200).ema_indicator()

    df["rsi"] = RSIIndicator(df["Close"], 14).rsi()
    df["adx"] = ADXIndicator(df["High"], df["Low"], df["Close"], 14).adx()

    df["atr"] = AverageTrueRange(
        df["High"],
        df["Low"],
        df["Close"],
        14
    ).average_true_range()

    df["avg_volume"] = df["Volume"].rolling(30).mean()

    return df.dropna().reset_index(drop=True)


# =========================
# STRATEGY LOGIC
# =========================

def trend(row):
    if row["Close"] > row["ema_20"] > row["ema_50"] > row["ema_200"]:
        return "BULLISH"

    if row["Close"] < row["ema_20"] < row["ema_50"] < row["ema_200"]:
        return "BEARISH"

    return "NEUTRAL"


def bullish_pin(row):
    body = abs(row["Close"] - row["Open"])
    rng = row["High"] - row["Low"]

    if rng == 0:
        return False

    lower = min(row["Open"], row["Close"]) - row["Low"]
    upper = row["High"] - max(row["Open"], row["Close"])

    return (
        lower > body * 2.5
        and upper < body * 1.2
        and row["Close"] > row["Open"]
    )


def bearish_pin(row):
    body = abs(row["Close"] - row["Open"])
    rng = row["High"] - row["Low"]

    if rng == 0:
        return False

    upper = row["High"] - max(row["Open"], row["Close"])
    lower = min(row["Open"], row["Close"]) - row["Low"]

    return (
        upper > body * 2.5
        and lower < body * 1.2
        and row["Close"] < row["Open"]
    )


def bullish_engulf(prev, curr):
    return (
        prev["Close"] < prev["Open"]
        and curr["Close"] > curr["Open"]
        and curr["Close"] > prev["Open"]
        and curr["Open"] < prev["Close"]
    )


def bearish_engulf(prev, curr):
    return (
        prev["Close"] > prev["Open"]
        and curr["Close"] < curr["Open"]
        and curr["Open"] > prev["Close"]
        and curr["Close"] < prev["Open"]
    )


def liquidity_sweep_low(df, i, lookback):
    swing_low = df["Low"].iloc[i - lookback:i].min()
    return df.iloc[i]["Low"] < swing_low and df.iloc[i]["Close"] > swing_low


def liquidity_sweep_high(df, i, lookback):
    swing_high = df["High"].iloc[i - lookback:i].max()
    return df.iloc[i]["High"] > swing_high and df.iloc[i]["Close"] < swing_high


def displacement(row, atr_mult):
    body = abs(row["Close"] - row["Open"])
    rng = row["High"] - row["Low"]

    if rng == 0:
        return False

    return body / rng > 0.55 and rng > row["atr"] * atr_mult


# =========================
# BACKTEST
# =========================

def backtest(df, params):
    balance = INITIAL_BALANCE
    equity_curve = []
    trades = []

    lookback = int(params["sweep_lookback"])

    for i in range(max(220, lookback + 2), len(df) - 2):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]

        market_trend = trend(curr)

        bull_pattern = bullish_pin(curr) or bullish_engulf(prev, curr)
        bear_pattern = bearish_pin(curr) or bearish_engulf(prev, curr)

        bull_signal = (
            market_trend == "BULLISH"
            and liquidity_sweep_low(df, i, lookback)
            and displacement(curr, params["displacement_atr"])
            and bull_pattern
            and curr["rsi"] >= params["rsi_bull"]
            and curr["adx"] >= params["min_adx"]
            and curr["Volume"] >= curr["avg_volume"] * params["volume_mult"]
        )

        bear_signal = (
            market_trend == "BEARISH"
            and liquidity_sweep_high(df, i, lookback)
            and displacement(curr, params["displacement_atr"])
            and bear_pattern
            and curr["rsi"] <= params["rsi_bear"]
            and curr["adx"] >= params["min_adx"]
            and curr["Volume"] >= curr["avg_volume"] * params["volume_mult"]
        )

        if not bull_signal and not bear_signal:
            equity_curve.append(balance)
            continue

        entry = df.iloc[i + 1]["Open"]
        atr = curr["atr"]
        risk_amount = balance * RISK_PER_TRADE

        if bull_signal:
            stop = entry - atr * params["atr_stop"]
            target = entry + ((entry - stop) * params["rr"])
            risk_per_unit = entry - stop
            direction = "LONG"
        else:
            stop = entry + atr * params["atr_stop"]
            target = entry - ((stop - entry) * params["rr"])
            risk_per_unit = stop - entry
            direction = "SHORT"

        if risk_per_unit <= 0:
            equity_curve.append(balance)
            continue

        position_size = risk_amount / risk_per_unit

        exit_price = None
        result = None

        for j in range(i + 1, min(i + int(params["max_hold"]), len(df))):
            candle = df.iloc[j]

            if direction == "LONG":
                if candle["Low"] <= stop:
                    exit_price = stop
                    result = "LOSS"
                    break
                if candle["High"] >= target:
                    exit_price = target
                    result = "WIN"
                    break

            else:
                if candle["High"] >= stop:
                    exit_price = stop
                    result = "LOSS"
                    break
                if candle["Low"] <= target:
                    exit_price = target
                    result = "WIN"
                    break

        if exit_price is None:
            exit_index = min(i + int(params["max_hold"]), len(df) - 1)
            exit_price = df.iloc[exit_index]["Close"]
            result = "TIME_EXIT"

        if direction == "LONG":
            pnl = (exit_price - entry) * position_size
        else:
            pnl = (entry - exit_price) * position_size

        fees = abs(entry * position_size) * FEE + abs(exit_price * position_size) * FEE
        pnl -= fees

        balance += pnl

        trades.append({
            "time": curr["timestamp"],
            "direction": direction,
            "entry": entry,
            "exit": exit_price,
            "pnl": pnl,
            "result": result,
            "balance": balance
        })

        equity_curve.append(balance)

    return pd.DataFrame(trades), pd.Series(equity_curve)


# =========================
# METRICS
# =========================

def performance(trades, equity):
    if trades.empty or len(equity) < 2:
        return None

    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]

    win_rate = len(wins) / len(trades)
    profit_factor = (
        wins["pnl"].sum() / abs(losses["pnl"].sum())
        if len(losses) else np.inf
    )

    expectancy = trades["pnl"].mean()
    total_return = equity.iloc[-1] / INITIAL_BALANCE - 1

    drawdown = equity / equity.cummax() - 1
    max_drawdown = drawdown.min()

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "max_drawdown": max_drawdown,
        "total_return": total_return,
        "final_balance": equity.iloc[-1]
    }


# =========================
# PARAMETER GRID
# =========================

def parameter_grid():
    for sweep_lookback in [10, 20, 30]:
        for min_adx in [15, 18, 22, 25]:
            for rsi_bull in [50, 52, 55]:
                for rsi_bear in [50, 48, 45]:
                    for volume_mult in [1.0, 1.1, 1.2, 1.4]:
                        for atr_stop in [1.0, 1.2, 1.5, 2.0]:
                            for rr in [1.2, 1.5, 2.0, 2.5]:
                                for displacement_atr in [0.8, 1.0, 1.2]:
                                    yield {
                                        "sweep_lookback": sweep_lookback,
                                        "min_adx": min_adx,
                                        "rsi_bull": rsi_bull,
                                        "rsi_bear": rsi_bear,
                                        "volume_mult": volume_mult,
                                        "atr_stop": atr_stop,
                                        "rr": rr,
                                        "displacement_atr": displacement_atr,
                                        "max_hold": 48
                                    }


# =========================
# OPTIMIZATION
# =========================

def optimize(train_df):
    results = []

    for params in parameter_grid():
        trades, equity = backtest(train_df, params)
        stats = performance(trades, equity)

        if not stats:
            continue

        if stats["trades"] < 10:
            continue

        if stats["profit_factor"] < 1.05:
            continue

        score = (
            stats["profit_factor"] * 0.35
            + stats["win_rate"] * 0.25
            + stats["total_return"] * 0.25
            + max(stats["max_drawdown"], -1) * 0.15
        )

        results.append({
            **params,
            **stats,
            "score": score
        })

    if not results:
        return None

    ranked = pd.DataFrame(results).sort_values("score", ascending=False)
    return ranked.iloc[0].to_dict()


# =========================
# TRUE WALK-FORWARD
# =========================

def walk_forward(df, train_size=TRAIN_SIZE, test_size=TEST_SIZE):
    reports = []
    all_trades = []

    start = 0
    window = 1

    while start + train_size + test_size < len(df):
        train_df = df.iloc[start:start + train_size].reset_index(drop=True)
        test_df = df.iloc[start + train_size:start + train_size + test_size].reset_index(drop=True)

        print(f"Optimizing window {window}...")

        best = optimize(train_df)

        if best is None:
            print(f"Window {window}: no valid parameters")
            start += test_size
            window += 1
            continue

        params = {
            "sweep_lookback": best["sweep_lookback"],
            "min_adx": best["min_adx"],
            "rsi_bull": best["rsi_bull"],
            "rsi_bear": best["rsi_bear"],
            "volume_mult": best["volume_mult"],
            "atr_stop": best["atr_stop"],
            "rr": best["rr"],
            "displacement_atr": best["displacement_atr"],
            "max_hold": best["max_hold"]
        }

        test_trades, test_equity = backtest(test_df, params)
        test_stats = performance(test_trades, test_equity)

        if test_stats:
            report = {
                "window": window,
                "train_start": train_df.iloc[0]["timestamp"],
                "train_end": train_df.iloc[-1]["timestamp"],
                "test_start": test_df.iloc[0]["timestamp"],
                "test_end": test_df.iloc[-1]["timestamp"],
                **params,
                **test_stats
            }

            reports.append(report)

            test_trades["window"] = window
            test_trades["test_start"] = test_df.iloc[0]["timestamp"]
            all_trades.append(test_trades)

            print(
                f"Window {window}: "
                f"trades={test_stats['trades']} "
                f"win_rate={test_stats['win_rate']:.2f} "
                f"PF={test_stats['profit_factor']:.2f} "
                f"return={test_stats['total_return']:.2%}"
            )
        else:
            print(f"Window {window}: no test trades")

        start += test_size
        window += 1

    report_df = pd.DataFrame(reports)
    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()

    return report_df, trades_df


# =========================
# MONTE CARLO
# =========================

def monte_carlo(trades, runs=MONTE_CARLO_RUNS):
    if trades.empty:
        return None, None

    pnl = trades["pnl"].values
    results = []

    for _ in range(runs):
        sampled = np.random.choice(pnl, size=len(pnl), replace=True)
        equity = INITIAL_BALANCE + np.cumsum(sampled)

        final_balance = equity[-1]
        total_return = final_balance / INITIAL_BALANCE - 1

        peak = np.maximum.accumulate(equity)
        drawdown = equity / peak - 1
        max_drawdown = drawdown.min()

        results.append({
            "final_balance": final_balance,
            "total_return": total_return,
            "max_drawdown": max_drawdown
        })

    mc = pd.DataFrame(results)

    summary = {
        "runs": runs,
        "median_final_balance": mc["final_balance"].median(),
        "worst_5pct_balance": mc["final_balance"].quantile(0.05),
        "best_5pct_balance": mc["final_balance"].quantile(0.95),
        "median_return": mc["total_return"].median(),
        "worst_5pct_return": mc["total_return"].quantile(0.05),
        "median_drawdown": mc["max_drawdown"].median(),
        "worst_5pct_drawdown": mc["max_drawdown"].quantile(0.05),
        "risk_of_loss": (mc["final_balance"] < INITIAL_BALANCE).mean()
    }

    return mc, summary


def plot_monte_carlo(mc, name):
    plt.figure()
    mc["final_balance"].hist(bins=50)
    plt.title(f"{name} Monte Carlo Final Balance Distribution")
    plt.xlabel("Final Balance")
    plt.ylabel("Frequency")
    plt.savefig(f"{name}_monte_carlo.png")
    plt.close()


# =========================
# LIVE TRADE SIGNAL ENGINE
# =========================

def get_last_best_params(report):
    if report.empty:
        return None

    row = report.sort_values("window").iloc[-1]

    return {
        "sweep_lookback": int(row["sweep_lookback"]),
        "min_adx": float(row["min_adx"]),
        "rsi_bull": float(row["rsi_bull"]),
        "rsi_bear": float(row["rsi_bear"]),
        "volume_mult": float(row["volume_mult"]),
        "atr_stop": float(row["atr_stop"]),
        "rr": float(row["rr"]),
        "displacement_atr": float(row["displacement_atr"]),
        "max_hold": int(row["max_hold"])
    }


def higher_timeframe_confirm(htf_df, direction):
    htf_df = add_indicators(htf_df)
    htf_row = htf_df.iloc[-2]
    htf_trend = trend(htf_row)

    if direction == "LONG":
        return htf_trend == "BULLISH", htf_trend

    if direction == "SHORT":
        return htf_trend == "BEARISH", htf_trend

    return False, htf_trend


def build_live_signal(name, df, htf_df, params):
    df = add_indicators(df)

    lookback = int(params["sweep_lookback"])
    i = len(df) - 2

    if i < max(220, lookback + 2):
        return None

    prev = df.iloc[i - 1]
    curr = df.iloc[i]

    market_trend = trend(curr)

    bull_pattern = bullish_pin(curr) or bullish_engulf(prev, curr)
    bear_pattern = bearish_pin(curr) or bearish_engulf(prev, curr)

    bull_signal = (
        market_trend == "BULLISH"
        and liquidity_sweep_low(df, i, lookback)
        and displacement(curr, params["displacement_atr"])
        and bull_pattern
        and curr["rsi"] >= params["rsi_bull"]
        and curr["adx"] >= params["min_adx"]
        and curr["Volume"] >= curr["avg_volume"] * params["volume_mult"]
    )

    bear_signal = (
        market_trend == "BEARISH"
        and liquidity_sweep_high(df, i, lookback)
        and displacement(curr, params["displacement_atr"])
        and bear_pattern
        and curr["rsi"] <= params["rsi_bear"]
        and curr["adx"] >= params["min_adx"]
        and curr["Volume"] >= curr["avg_volume"] * params["volume_mult"]
    )

    if not bull_signal and not bear_signal:
        return None

    direction = "LONG" if bull_signal else "SHORT"

    htf_ok, htf_trend = higher_timeframe_confirm(htf_df, direction)
    if not htf_ok:
        print(f"{name}: signal rejected by HTF trend. HTF={htf_trend}")
        return None

    entry = df.iloc[i + 1]["Open"]
    atr = curr["atr"]

    if direction == "LONG":
        stop = entry - atr * params["atr_stop"]
        target = entry + ((entry - stop) * params["rr"])
    else:
        stop = entry + atr * params["atr_stop"]
        target = entry - ((stop - entry) * params["rr"])

    risk = abs(entry - stop)
    reward = abs(target - entry)

    confidence = 0
    confidence += 25 if market_trend in ["BULLISH", "BEARISH"] else 0
    confidence += 20 if bull_pattern or bear_pattern else 0
    confidence += 20 if curr["adx"] >= params["min_adx"] else 0
    confidence += 15 if curr["Volume"] >= curr["avg_volume"] * params["volume_mult"] else 0
    confidence += 20 if htf_ok else 0

    return {
        "market": name,
        "time": str(curr["timestamp"]),
        "direction": direction,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
        "risk": float(risk),
        "reward": float(reward),
        "rr": float(reward / risk) if risk > 0 else 0,
        "ltf_trend": market_trend,
        "htf_trend": htf_trend,
        "rsi": float(curr["rsi"]),
        "adx": float(curr["adx"]),
        "confidence": int(confidence),
        "params": params
    }


def format_signal(signal):
    return f"""
🚨 *LIVE TRADE SIGNAL*

Market: *{signal['market']}*
Direction: *{signal['direction']}*

Entry: `{signal['entry']:.2f}`
Stop Loss: `{signal['stop']:.2f}`
Take Profit: `{signal['target']:.2f}`

R:R: `{signal['rr']:.2f}`
Confidence: *{signal['confidence']}/100*

LTF Trend: `{signal['ltf_trend']}`
HTF Trend: `{signal['htf_trend']}`

RSI: `{signal['rsi']:.2f}`
ADX: `{signal['adx']:.2f}`

Time: `{signal['time']}`
"""


def maybe_send_signal(signal):
    sent = load_json(SENT_SIGNAL_FILE, {})

    key = f"{signal['market']}_{signal['direction']}_{signal['time']}"

    if sent.get(key):
        print(f"Duplicate signal blocked: {key}")
        return

    send_telegram(format_signal(signal))
    sent[key] = True
    save_json(SENT_SIGNAL_FILE, sent)


# =========================
# RUN MARKET
# =========================

def run_market(name, df, htf_df):
    print(f"\n==============================")
    print(f"RUNNING {name}")
    print(f"==============================")

    df_ind = add_indicators(df)

    report, trades = walk_forward(df_ind)

    if report.empty or trades.empty:
        print(f"{name}: no robust walk-forward results")
        return None

    best_params = get_last_best_params(report)

    cache = load_json(PARAM_CACHE_FILE, {})
    cache[name] = best_params
    save_json(PARAM_CACHE_FILE, cache)

    report.to_csv(f"{name}_walk_forward_report.csv", index=False)
    trades.to_csv(f"{name}_walk_forward_trades.csv", index=False)

    print("\nWalk-forward summary:")
    print(f"Windows: {len(report)}")
    print(f"Total trades: {len(trades)}")
    print(f"Average win rate: {report['win_rate'].mean():.2%}")
    print(f"Average profit factor: {report['profit_factor'].replace(np.inf, np.nan).mean():.2f}")
    print(f"Average return per window: {report['total_return'].mean():.2%}")
    print(f"Average max drawdown: {report['max_drawdown'].mean():.2%}")

    mc, mc_summary = monte_carlo(trades)

    if mc is not None:
        mc.to_csv(f"{name}_monte_carlo_results.csv", index=False)
        plot_monte_carlo(mc, name)

        print("\nMonte Carlo summary:")
        for k, v in mc_summary.items():
            if "return" in k or "drawdown" in k or "risk" in k:
                print(f"{k}: {v:.2%}")
            else:
                print(f"{k}: {v}")

        pd.DataFrame([mc_summary]).to_csv(
            f"{name}_monte_carlo_summary.csv",
            index=False
        )

    signal = build_live_signal(name, df, htf_df, best_params)

    if signal:
        maybe_send_signal(signal)
        print(format_signal(signal))
    else:
        print(f"{name}: no live trade signal.")

        if SEND_NO_SIGNAL_MESSAGE:
            send_telegram(f"{name}: no live trade signal.")

    print("\nSaved files:")
    print(f"- {name}_walk_forward_report.csv")
    print(f"- {name}_walk_forward_trades.csv")
    print(f"- {name}_monte_carlo_results.csv")
    print(f"- {name}_monte_carlo_summary.csv")
    print(f"- {name}_monte_carlo.png")

    return best_params


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    while True:
        try:
            btc = fetch_btc(TIMEFRAME, BTC_LIMIT)
            btc_htf = fetch_btc(HIGHER_TIMEFRAME, BTC_HTF_LIMIT)

            gold = fetch_gold(TIMEFRAME, GOLD_PERIOD)
            gold_htf = fetch_gold(HIGHER_TIMEFRAME, GOLD_HTF_PERIOD)

            run_market("BTC", btc, btc_htf)
            run_market("GOLD", gold, gold_htf)

        except Exception as e:
            error_msg = f"Bot error: `{e}`"
            print(error_msg)
            send_telegram(error_msg)

        time.sleep(LIVE_SCAN_SECONDS)
