import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange


# =========================
# SETTINGS
# =========================

BTC_SYMBOL = "BTC/USDT"
GOLD_SYMBOL = "GC=F"

TIMEFRAME = "1h"
BTC_LIMIT = 1500
GOLD_PERIOD = "730d"

FEE = 0.0006
INITIAL_BALANCE = 10_000
RISK_PER_TRADE = 0.01


# =========================
# DATA
# =========================

def fetch_btc():
    exchange = ccxt.binance()
    candles = exchange.fetch_ohlcv(BTC_SYMBOL, timeframe=TIMEFRAME, limit=BTC_LIMIT)

    df = pd.DataFrame(
        candles,
        columns=["timestamp", "Open", "High", "Low", "Close", "Volume"]
    )

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def fetch_gold():
    df = yf.download(
        GOLD_SYMBOL,
        period=GOLD_PERIOD,
        interval=TIMEFRAME,
        auto_adjust=True,
        progress=False
    ).reset_index()

    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "timestamp"})
    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "timestamp"})

    return df[["timestamp", "Open", "High", "Low", "Close", "Volume"]].dropna()


# =========================
# INDICATORS
# =========================

def add_indicators(df):
    df = df.copy()

    df["ema_20"] = EMAIndicator(df["Close"], 20).ema_indicator()
    df["ema_50"] = EMAIndicator(df["Close"], 50).ema_indicator()
    df["ema_200"] = EMAIndicator(df["Close"], 200).ema_indicator()

    df["rsi"] = RSIIndicator(df["Close"], 14).rsi()

    df["adx"] = ADXIndicator(
        df["High"],
        df["Low"],
        df["Close"],
        14
    ).adx()

    df["atr"] = AverageTrueRange(
        df["High"],
        df["Low"],
        df["Close"],
        14
    ).average_true_range()

    df["avg_volume"] = df["Volume"].rolling(30).mean()

    return df.dropna().reset_index(drop=True)


# =========================
# PATTERN LOGIC
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

    return lower > body * 2.5 and upper < body * 1.2 and row["Close"] > row["Open"]


def bearish_pin(row):
    body = abs(row["Close"] - row["Open"])
    rng = row["High"] - row["Low"]

    if rng == 0:
        return False

    upper = row["High"] - max(row["Open"], row["Close"])
    lower = min(row["Open"], row["Close"]) - row["Low"]

    return upper > body * 2.5 and lower < body * 1.2 and row["Close"] < row["Open"]


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

    lookback = params["sweep_lookback"]

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
            continue

        position_size = risk_amount / risk_per_unit

        exit_price = None
        result = None

        for j in range(i + 1, min(i + params["max_hold"], len(df))):
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

            if direction == "SHORT":
                if candle["High"] >= stop:
                    exit_price = stop
                    result = "LOSS"
                    break
                if candle["Low"] <= target:
                    exit_price = target
                    result = "WIN"
                    break

        if exit_price is None:
            exit_price = df.iloc[min(i + params["max_hold"], len(df) - 1)]["Close"]
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

    trades_df = pd.DataFrame(trades)
    equity = pd.Series(equity_curve)

    return trades_df, equity


# =========================
# METRICS
# =========================

def performance(trades, equity):
    if trades.empty or len(equity) < 2:
        return None

    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]

    win_rate = len(wins) / len(trades)
    profit_factor = wins["pnl"].sum() / abs(losses["pnl"].sum()) if len(losses) else np.inf
    expectancy = trades["pnl"].mean()

    drawdown = equity / equity.cummax() - 1
    max_dd = drawdown.min()

    total_return = equity.iloc[-1] / INITIAL_BALANCE - 1

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "max_drawdown": max_dd,
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

        if stats["trades"] < 15:
            continue

        if stats["profit_factor"] < 1.1:
            continue

        score = (
            stats["profit_factor"] * 0.40
            + stats["win_rate"] * 0.30
            + stats["total_return"] * 0.20
            + max(stats["max_drawdown"], -1) * 0.10
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
# WALK-FORWARD TEST
# =========================

def walk_forward(df, train_size=500, test_size=150):
    all_test_trades = []
    reports = []

    start = 0

    while start + train_size + test_size < len(df):
        train_df = df.iloc[start:start + train_size].reset_index(drop=True)
        test_df = df.iloc[start + train_size:start + train_size + test_size].reset_index(drop=True)

        best_params = optimize(train_df)

        if best_params is None:
            start += test_size
            continue

        clean_params = {
            k: best_params[k]
            for k in [
                "sweep_lookback",
                "min_adx",
                "rsi_bull",
                "rsi_bear",
                "volume_mult",
                "atr_stop",
                "rr",
                "displacement_atr",
                "max_hold"
            ]
        }

        test_trades, test_equity = backtest(test_df, clean_params)
        test_stats = performance(test_trades, test_equity)

        if test_stats:
            reports.append({
                "train_start": train_df.iloc[0]["timestamp"],
                "train_end": train_df.iloc[-1]["timestamp"],
                "test_start": test_df.iloc[0]["timestamp"],
                "test_end": test_df.iloc[-1]["timestamp"],
                **clean_params,
                **test_stats
            })

            test_trades["test_start"] = test_df.iloc[0]["timestamp"]
            all_test_trades.append(test_trades)

        start += test_size

    report_df = pd.DataFrame(reports)

    if all_test_trades:
        trades_df = pd.concat(all_test_trades, ignore_index=True)
    else:
        trades_df = pd.DataFrame()

    return report_df, trades_df


# =========================
# RUN
# =========================

def run_market(name, df):
    print(f"\n===== {name} =====")

    df = add_indicators(df)

    report, trades = walk_forward(
        df,
        train_size=500,
        test_size=150
    )

    if report.empty:
        print("No robust walk-forward results.")
        return

    print("\nWalk-forward report:")
    print(report.to_string(index=False))

    print("\nAverage out-of-sample results:")
    summary = {
        "windows": len(report),
        "avg_win_rate": report["win_rate"].mean(),
        "avg_profit_factor": report["profit_factor"].replace(np.inf, np.nan).mean(),
        "avg_return": report["total_return"].mean(),
        "avg_drawdown": report["max_drawdown"].mean(),
        "total_trades": report["trades"].sum()
    }

    for k, v in summary.items():
        print(f"{k}: {v}")

    report.to_csv(f"{name}_walk_forward_report.csv", index=False)
    trades.to_csv(f"{name}_walk_forward_trades.csv", index=False)

    print(f"\nSaved:")
    print(f"- {name}_walk_forward_report.csv")
    print(f"- {name}_walk_forward_trades.csv")


if __name__ == "__main__":
    btc = fetch_btc()
    gold = fetch_gold()

    run_market("BTC", btc)
    run_market("GOLD", gold)
