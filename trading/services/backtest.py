from __future__ import annotations

import math
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from trading.constants import (
    STRATEGY_VERSION,
    BACKTEST_ENGINE_VERSION,
    RESEARCH_ENGINE_VERSION,
    RESEARCH_DEFAULT_DAYS,
    RESEARCH_SPLIT_DATE,
    RESEARCH_INTERVALS,
    RESEARCH_WALK_FORWARD_DAYS,
)
from trading.models import AppConfig, BacktestRun
from .binance_client import BinanceClient
from .indicators import candles_to_df, enrich, bars_per_24h
from .scoring import score_latest, is_actionable_setup, regime_score_from_values

SUPPORTED_MATRIX_INTERVALS = ("15m", "30m", "1h", "4h")
INTERVAL_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}
RESAMPLE_RULES = {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h"}
CACHE_TTL_SECONDS = 30 * 60
RESEARCH_CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_DIR = Path(os.getenv("BINANCE_BACKTEST_CACHE_DIR", "/tmp/binance-insight-backtest-cache"))

SCORE_BANDS = (
    ("72–74", 72.0, 75.0),
    ("75–79", 75.0, 80.0),
    ("80–84", 80.0, 85.0),
    ("85–89", 85.0, 90.0),
    ("90+", 90.0, 101.0),
)


def _max_drawdown(equity_curve):
    peak = -1e99
    maxdd = 0.0
    for x in equity_curve:
        peak = max(peak, x)
        if peak > 0:
            maxdd = max(maxdd, (peak - x) / peak * 100)
    return maxdd


def _attach_btc_regime(asset_df: pd.DataFrame, btc_df: pd.DataFrame) -> pd.DataFrame:
    btc = btc_df[["open_time", "close", "ema20", "ema50", "ema200", "rsi14"]].copy()
    btc = btc.rename(
        columns={
            "close": "btc_close",
            "ema20": "btc_ema20",
            "ema50": "btc_ema50",
            "ema200": "btc_ema200",
            "rsi14": "btc_rsi14",
        }
    )
    return pd.merge_asof(
        asset_df.sort_values("open_time"),
        btc.sort_values("open_time"),
        on="open_time",
        direction="backward",
    )


def _cache_path(symbol: str, days: int) -> Path:
    safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum())
    return CACHE_DIR / f"{safe_symbol}_15m_{int(days)}d.pkl"


def _cached_raw_15m(client: BinanceClient, symbol: str, days: int) -> pd.DataFrame:
    """Short-lived 15m cache used by the existing 180-day matrix."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, days)
    try:
        if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS:
            cached = pd.read_pickle(path)
            if isinstance(cached, pd.DataFrame) and len(cached) >= 220:
                return cached
    except Exception:
        pass

    rows = client.historical_klines(symbol, "15m", days=days, max_candles=20_000)
    raw = candles_to_df(rows)
    try:
        raw.to_pickle(path)
    except Exception:
        pass
    return raw


def _resample_candles(raw: pd.DataFrame, interval: str) -> pd.DataFrame:
    if interval not in RESAMPLE_RULES:
        raise ValueError(f"Unsupported matrix timeframe: {interval}")
    if interval == "15m":
        return raw.copy()

    src = raw.sort_values("open_time").set_index("open_time")
    rule = RESAMPLE_RULES[interval]
    out = src.resample(rule, origin="epoch", label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "quote_volume": "sum",
            "close_time": "max",
        }
    )
    out = out.dropna(subset=["open", "high", "low", "close"]).reset_index()
    return out


def _run_backtest_frames(
    symbol: str,
    interval: str,
    asset_df: pd.DataFrame,
    btc_df: pd.DataFrame,
    *,
    days: int,
    initial: float,
    cfg: AppConfig,
) -> BacktestRun:
    df = _attach_btc_regime(asset_df, btc_df)
    if len(df) < 220:
        raise RuntimeError("Not enough historical candles for a meaningful backtest")

    balance = float(initial)
    equity_curve = [balance]
    trades = []
    pos = None
    fee_rate = cfg.fee_bps / 10000
    slip = cfg.slippage_bps / 10000
    interval_minutes = INTERVAL_MINUTES.get(interval, 15)
    max_hold_bars = max(1, int(round((24 * 60) / interval_minutes)))
    inferred_bars_per_day = bars_per_24h(asset_df)

    for i in range(210, len(df) - 1):
        r = df.iloc[i]
        nxt = df.iloc[i + 1]
        if any(pd.isna(r[c]) for c in ["btc_close", "btc_ema20", "btc_ema50", "btc_ema200", "btc_rsi14"]):
            continue

        regime = regime_score_from_values(
            r.btc_close,
            r.btc_ema20,
            r.btc_ema50,
            r.btc_ema200,
            r.btc_rsi14,
        )
        qv = (
            float(r.quote_volume_24h)
            if not pd.isna(r.quote_volume_24h)
            else max(float(r.quote_volume) * inferred_bars_per_day, 1_000_000)
        )
        spread_bps = 5.0
        scored = score_latest(
            r,
            qv,
            spread_bps,
            regime,
            cfg.stop_atr_multiple,
            cfg.target_r_multiple,
        )

        if pos is None and is_actionable_setup(r, scored, regime, spread_bps, cfg.signal_threshold):
            entry = float(nxt.open) * (1 + slip)
            if scored.stop_price < entry:
                risk_per_unit = max(entry - scored.stop_price, entry * 0.002)
                risk_amount = balance * cfg.risk_per_trade_pct / 100
                qty_by_risk = risk_amount / risk_per_unit
                qty_by_asset = (balance * cfg.max_asset_exposure_pct / 100) / entry
                qty = max(0.0, min(qty_by_risk, qty_by_asset))
                notional = qty * entry
                if notional >= 5:
                    entry_fee = notional * fee_rate
                    balance -= entry_fee
                    actual_stop = float(scored.stop_price)
                    actual_target = entry + cfg.target_r_multiple * (entry - actual_stop)
                    pos = {
                        "entry": entry,
                        "qty": qty,
                        "stop": actual_stop,
                        "target": actual_target,
                        "entry_fee": entry_fee,
                        "open_i": i,
                    }

        if pos is not None:
            exit_px = None
            reason = None
            if float(nxt.low) <= pos["stop"]:
                exit_px = pos["stop"] * (1 - slip)
                reason = "stop"
            elif float(nxt.high) >= pos["target"]:
                exit_px = pos["target"] * (1 - slip)
                reason = "target"
            elif i - pos["open_i"] >= max_hold_bars:
                exit_px = float(nxt.close) * (1 - slip)
                reason = "time"

            if exit_px is not None:
                exit_notional = pos["qty"] * exit_px
                exit_fee = exit_notional * fee_rate
                gross = (exit_px - pos["entry"]) * pos["qty"]
                balance += gross - exit_fee
                pnl = gross - pos["entry_fee"] - exit_fee
                planned_stop_fill = pos["stop"] * (1 - slip)
                planned_stop_fee = planned_stop_fill * pos["qty"] * fee_rate
                initial_risk_dollars = (
                    (pos["entry"] - planned_stop_fill) * pos["qty"]
                    + pos["entry_fee"]
                    + planned_stop_fee
                )
                trades.append(
                    {
                        "pnl": pnl,
                        "ret": pnl / (pos["entry"] * pos["qty"]) * 100,
                        "r": pnl / initial_risk_dollars if initial_risk_dollars > 0 else 0.0,
                        "reason": reason,
                    }
                )
                pos = None

        if pos is None:
            marked_equity = balance
        else:
            gross_unrealized = (float(nxt.close) - pos["entry"]) * pos["qty"]
            estimated_exit_fee = pos["qty"] * float(nxt.close) * fee_rate
            marked_equity = balance + gross_unrealized - estimated_exit_fee
        equity_curve.append(marked_equity)

    if pos is not None:
        final_close = float(df.iloc[-1].close) * (1 - slip)
        exit_fee = pos["qty"] * final_close * fee_rate
        gross = (final_close - pos["entry"]) * pos["qty"]
        balance += gross - exit_fee
        pnl = gross - pos["entry_fee"] - exit_fee
        planned_stop_fill = pos["stop"] * (1 - slip)
        planned_stop_fee = planned_stop_fill * pos["qty"] * fee_rate
        initial_risk_dollars = (
            (pos["entry"] - planned_stop_fill) * pos["qty"]
            + pos["entry_fee"]
            + planned_stop_fee
        )
        trades.append(
            {
                "pnl": pnl,
                "ret": pnl / (pos["entry"] * pos["qty"]) * 100,
                "r": pnl / initial_risk_dollars if initial_risk_dollars > 0 else 0.0,
                "reason": "end",
            }
        )
        equity_curve.append(balance)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (999 if gross_profit else 0)
    rets = [t["ret"] for t in trades]
    expectancy = sum(rets) / len(rets) if rets else 0
    r_values = [t["r"] for t in trades]
    expectancy_r = sum(r_values) / len(r_values) if r_values else 0
    sharpe = (
        np.mean(rets) / np.std(rets) * math.sqrt(len(rets))
        if len(rets) > 1 and np.std(rets) > 0
        else 0
    )

    run = BacktestRun.objects.create(
        symbol=symbol,
        timeframe=interval,
        start_date=df.open_time.iloc[0].to_pydatetime(),
        end_date=df.open_time.iloc[-1].to_pydatetime(),
        trades=len(trades),
        win_rate_pct=(len(wins) / len(trades) * 100 if trades else 0),
        net_return_pct=(balance / initial - 1) * 100,
        profit_factor=profit_factor,
        max_drawdown_pct=_max_drawdown(equity_curve),
        expectancy_pct=expectancy,
        sharpe=sharpe,
        results={
            "strategy_version": STRATEGY_VERSION,
            "backtest_engine_version": BACKTEST_ENGINE_VERSION,
            "trades": trades[-100:],
            "initial": initial,
            "final": balance,
            "btc_regime_aligned": True,
            "fees_bps": cfg.fee_bps,
            "slippage_bps": cfg.slippage_bps,
            "lookback_days": days,
            "max_hold_hours": 24,
            "max_hold_bars": max_hold_bars,
            "expectancy_r": expectancy_r,
            "source_interval": "15m",
            "resampled": interval != "15m",
        },
    )
    return run


def run_backtest(symbol="BTCUSDT", interval=None, days=180, initial=10000.0):
    """Run one Strategy v2.0 backtest using a consistent 15m source dataset."""
    cfg = AppConfig.current()
    interval = interval or cfg.scan_interval
    if interval not in SUPPORTED_MATRIX_INTERVALS:
        raise ValueError(f"Unsupported backtest timeframe: {interval}")

    client = BinanceClient(mode="paper")
    asset_raw = _cached_raw_15m(client, symbol, days)
    btc_raw = asset_raw.copy() if symbol == "BTCUSDT" else _cached_raw_15m(client, "BTCUSDT", days)

    asset_df = enrich(_resample_candles(asset_raw, interval))
    btc_df = enrich(_resample_candles(btc_raw, interval))
    return _run_backtest_frames(
        symbol,
        interval,
        asset_df,
        btc_df,
        days=days,
        initial=initial,
        cfg=cfg,
    )


def run_symbol_matrix(symbol: str, intervals=SUPPORTED_MATRIX_INTERVALS, days=180, initial=10000.0):
    """Run the existing 180-day quick matrix for one symbol."""
    cfg = AppConfig.current()
    client = BinanceClient(mode="paper")
    asset_raw = _cached_raw_15m(client, symbol, days)
    btc_raw = asset_raw.copy() if symbol == "BTCUSDT" else _cached_raw_15m(client, "BTCUSDT", days)

    runs = []
    errors = []
    for interval in intervals:
        if interval not in SUPPORTED_MATRIX_INTERVALS:
            errors.append({"interval": interval, "error": "unsupported timeframe"})
            continue
        try:
            asset_df = enrich(_resample_candles(asset_raw, interval))
            btc_df = enrich(_resample_candles(btc_raw, interval))
            run = _run_backtest_frames(
                symbol,
                interval,
                asset_df,
                btc_df,
                days=days,
                initial=initial,
                cfg=cfg,
            )
            runs.append(run)
        except Exception as exc:
            errors.append({"interval": interval, "error": str(exc)})
    return runs, errors


# ---------------------------------------------------------------------------
# Research Engine v2.2
# ---------------------------------------------------------------------------


def _research_cache_path(symbol: str, days: int) -> Path:
    safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum())
    return CACHE_DIR / f"research_{safe_symbol}_1h_{int(days)}d.pkl"


def _drop_incomplete_1h(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return raw
    now = pd.Timestamp.now(tz="UTC")
    out = raw.copy()
    if "close_time" in out.columns:
        out = out[out["close_time"] <= now]
    return out.reset_index(drop=True)


def _cached_raw_research_1h(client: BinanceClient, symbol: str, days: int) -> pd.DataFrame:
    """Fetch/cache multi-year 1h candles for Research Engine v2.2.

    Using 1h as the source makes a three-year research run practical while
    still allowing 4h candles to be built consistently from complete bars.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _research_cache_path(symbol, days)
    try:
        if path.exists() and (time.time() - path.stat().st_mtime) < RESEARCH_CACHE_TTL_SECONDS:
            cached = pd.read_pickle(path)
            if isinstance(cached, pd.DataFrame) and len(cached) >= 220:
                return cached
    except Exception:
        pass

    expected = int(days * 24) + 1_000
    max_candles = min(max(expected, 10_000), 50_000)
    rows = client.historical_klines(symbol, "1h", days=days, max_candles=max_candles)
    raw = _drop_incomplete_1h(candles_to_df(rows))
    try:
        raw.to_pickle(path)
    except Exception:
        pass
    return raw


def _research_resample(raw_1h: pd.DataFrame, interval: str) -> pd.DataFrame:
    if interval not in RESEARCH_INTERVALS:
        raise ValueError(f"Research Engine v{RESEARCH_ENGINE_VERSION} supports only 1h and 4h")
    if interval == "1h":
        return raw_1h.copy()

    src = raw_1h.sort_values("open_time").set_index("open_time")
    out = src.resample("4h", origin="epoch", label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        quote_volume=("quote_volume", "sum"),
        close_time=("close_time", "max"),
        source_bars=("close", "count"),
    )
    # Do not let a partially formed 4h bucket enter research results.
    out = out[(out["source_bars"] == 4) & out["close"].notna()]
    return out.drop(columns=["source_bars"]).reset_index()


def _trade_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "net_return_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "expectancy_pct": 0.0,
            "expectancy_r": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "sharpe": 0.0,
            "stop_exits": 0,
            "target_exits": 0,
            "time_exits": 0,
            "end_exits": 0,
        }

    wins = [t for t in trades if float(t["pnl"]) > 0]
    losses = [t for t in trades if float(t["pnl"]) < 0]
    gross_profit = sum(float(t["pnl"]) for t in wins)
    gross_loss = abs(sum(float(t["pnl"]) for t in losses))
    pf = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit else 0.0)
    rets = [float(t["ret"]) for t in trades]
    rs = [float(t["r"]) for t in trades]
    win_rs = [float(t["r"]) for t in wins]
    loss_rs = [float(t["r"]) for t in losses]

    start_equity = float(trades[0].get("equity_before", 0.0) or 0.0)
    end_equity = float(trades[-1].get("equity_after", start_equity) or start_equity)
    net_return = ((end_equity / start_equity) - 1) * 100 if start_equity > 0 else 0.0
    curve = [start_equity] + [float(t.get("equity_after", start_equity)) for t in trades]
    sharpe = (
        float(np.mean(rets) / np.std(rets) * math.sqrt(len(rets)))
        if len(rets) > 1 and np.std(rets) > 0
        else 0.0
    )
    reasons = {"stop": 0, "target": 0, "time": 0, "end": 0}
    for trade in trades:
        reason = str(trade.get("reason") or "")
        if reason in reasons:
            reasons[reason] += 1

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100,
        "net_return_pct": net_return,
        "profit_factor": pf,
        "max_drawdown_pct": _max_drawdown(curve),
        "expectancy_pct": float(np.mean(rets)) if rets else 0.0,
        "expectancy_r": float(np.mean(rs)) if rs else 0.0,
        "avg_win_r": float(np.mean(win_rs)) if win_rs else 0.0,
        "avg_loss_r": float(np.mean(loss_rs)) if loss_rs else 0.0,
        "sharpe": sharpe,
        "stop_exits": reasons["stop"],
        "target_exits": reasons["target"],
        "time_exits": reasons["time"],
        "end_exits": reasons["end"],
    }


def _subset_by_open_time(trades: list[dict], start=None, end=None) -> list[dict]:
    out = []
    start_ts = pd.Timestamp(start) if start is not None else None
    end_ts = pd.Timestamp(end) if end is not None else None
    if start_ts is not None and start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    if end_ts is not None and end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")

    for trade in trades:
        ts = pd.Timestamp(trade["opened_at"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if start_ts is not None and ts < start_ts:
            continue
        if end_ts is not None and ts >= end_ts:
            continue
        out.append(trade)
    return out


def _equity_period_metrics(equity_points: list[dict], start=None, end=None) -> dict:
    """Calculate return and drawdown from mark-to-market equity points.

    For a bounded period, the baseline is the latest equity observation before
    the period starts (or the first observation if no prior point exists).
    """
    if not equity_points:
        return {"net_return_pct": 0.0, "max_drawdown_pct": 0.0}

    parsed = []
    for point in equity_points:
        ts = pd.Timestamp(point["time"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        parsed.append((ts, float(point["equity"])))
    parsed.sort(key=lambda x: x[0])

    start_ts = pd.Timestamp(start) if start is not None else None
    end_ts = pd.Timestamp(end) if end is not None else None
    if start_ts is not None and start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    if end_ts is not None and end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")

    if start_ts is None:
        baseline = parsed[0]
    else:
        before = [point for point in parsed if point[0] < start_ts]
        baseline = before[-1] if before else parsed[0]

    selected = [
        point for point in parsed
        if (start_ts is None or point[0] >= start_ts)
        and (end_ts is None or point[0] < end_ts)
    ]
    curve = [baseline[1]] + [point[1] for point in selected]
    if not selected:
        curve = [baseline[1]]

    start_equity = curve[0]
    end_equity = curve[-1]
    return {
        "net_return_pct": ((end_equity / start_equity) - 1) * 100 if start_equity > 0 else 0.0,
        "max_drawdown_pct": _max_drawdown(curve),
    }


def _score_band_diagnostics(trades: list[dict], split_date: str) -> list[dict]:
    rows = []
    oos_all = _subset_by_open_time(trades, start=split_date)
    for label, lo, hi in SCORE_BANDS:
        full = [t for t in trades if lo <= float(t.get("score", 0.0)) < hi]
        oos = [t for t in oos_all if lo <= float(t.get("score", 0.0)) < hi]
        full_m = _trade_metrics(full)
        oos_m = _trade_metrics(oos)
        rows.append(
            {
                "label": label,
                "min_score": lo,
                "max_score": hi,
                "full": full_m,
                "oos": oos_m,
            }
        )
    return rows


def _walk_forward_windows(trades: list[dict], equity_points: list[dict], split_date: str, final_date, window_days: int) -> dict:
    start = pd.Timestamp(split_date)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    final = pd.Timestamp(final_date)
    if final.tzinfo is None:
        final = final.tz_localize("UTC")

    windows = []
    cursor = start
    delta = pd.Timedelta(days=window_days)
    while cursor < final:
        end = min(cursor + delta, final + pd.Timedelta(seconds=1))
        subset = _subset_by_open_time(trades, start=cursor, end=end)
        metrics = _trade_metrics(subset)
        equity_metrics = _equity_period_metrics(equity_points, start=cursor, end=end)
        metrics["net_return_pct"] = equity_metrics["net_return_pct"]
        metrics["max_drawdown_pct"] = equity_metrics["max_drawdown_pct"]
        evaluable = metrics["trades"] >= 3
        positive = bool(
            evaluable
            and metrics["net_return_pct"] > 0
            and metrics["expectancy_r"] > 0
            and metrics["profit_factor"] > 1.0
        )
        windows.append(
            {
                "start": cursor.date().isoformat(),
                "end": (end - pd.Timedelta(seconds=1)).date().isoformat(),
                "evaluable": evaluable,
                "positive": positive,
                "metrics": metrics,
            }
        )
        cursor = end

    evaluable_windows = [w for w in windows if w["evaluable"]]
    positive_windows = [w for w in evaluable_windows if w["positive"]]
    pfs = [w["metrics"]["profit_factor"] for w in evaluable_windows if w["metrics"]["profit_factor"] < 900]
    exp_rs = [w["metrics"]["expectancy_r"] for w in evaluable_windows]
    returns = [w["metrics"]["net_return_pct"] for w in evaluable_windows]

    return {
        "window_days": window_days,
        "windows": windows,
        "evaluable_windows": len(evaluable_windows),
        "positive_windows": len(positive_windows),
        "positive_window_pct": (
            len(positive_windows) / len(evaluable_windows) * 100 if evaluable_windows else 0.0
        ),
        "median_profit_factor": float(np.median(pfs)) if pfs else 0.0,
        "median_expectancy_r": float(np.median(exp_rs)) if exp_rs else 0.0,
        "median_return_pct": float(np.median(returns)) if returns else 0.0,
    }


def _research_candidate(oos: dict, walk: dict) -> dict:
    reasons = []
    if oos["trades"] < 20:
        reasons.append("fewer than 20 out-of-sample trades")
    if oos["net_return_pct"] <= 0:
        reasons.append("out-of-sample return is not positive")
    if oos["profit_factor"] < 1.25:
        reasons.append("out-of-sample profit factor is below 1.25")
    if oos["expectancy_r"] <= 0:
        reasons.append("out-of-sample expectancy R is not positive")
    if walk["evaluable_windows"] < 2:
        reasons.append("fewer than two evaluable walk-forward windows")
    elif walk["positive_window_pct"] < 60:
        reasons.append("fewer than 60% of evaluable walk-forward windows are positive")

    return {
        "passes": not reasons,
        "reasons": reasons,
        "criteria": {
            "min_oos_trades": 20,
            "min_oos_profit_factor": 1.25,
            "positive_oos_return": True,
            "positive_oos_expectancy_r": True,
            "min_evaluable_windows": 2,
            "min_positive_window_pct": 60,
        },
    }


def _simulate_research_frames(
    symbol: str,
    interval: str,
    asset_df: pd.DataFrame,
    btc_df: pd.DataFrame,
    *,
    cfg: AppConfig,
    initial: float,
) -> tuple[pd.DataFrame, list[dict], float]:
    df = _attach_btc_regime(asset_df, btc_df)
    if len(df) < 220:
        raise RuntimeError("Not enough completed historical candles for multi-year research")

    balance = float(initial)
    trades = []
    equity_points = [{"time": pd.Timestamp(df.open_time.iloc[210]).isoformat(), "equity": float(initial)}]
    pos = None
    fee_rate = cfg.fee_bps / 10000
    slip = cfg.slippage_bps / 10000
    interval_minutes = INTERVAL_MINUTES[interval]
    max_hold_bars = max(1, int(round((24 * 60) / interval_minutes)))
    inferred_bars_per_day = bars_per_24h(asset_df)

    for i in range(210, len(df) - 1):
        r = df.iloc[i]
        nxt = df.iloc[i + 1]
        if any(pd.isna(r[c]) for c in ["btc_close", "btc_ema20", "btc_ema50", "btc_ema200", "btc_rsi14"]):
            continue

        regime = regime_score_from_values(
            r.btc_close,
            r.btc_ema20,
            r.btc_ema50,
            r.btc_ema200,
            r.btc_rsi14,
        )
        qv = (
            float(r.quote_volume_24h)
            if not pd.isna(r.quote_volume_24h)
            else max(float(r.quote_volume) * inferred_bars_per_day, 1_000_000)
        )
        spread_bps = 5.0
        scored = score_latest(
            r,
            qv,
            spread_bps,
            regime,
            cfg.stop_atr_multiple,
            cfg.target_r_multiple,
        )

        if pos is None and is_actionable_setup(r, scored, regime, spread_bps, cfg.signal_threshold):
            entry = float(nxt.open) * (1 + slip)
            if scored.stop_price < entry:
                risk_per_unit = max(entry - scored.stop_price, entry * 0.002)
                risk_amount = balance * cfg.risk_per_trade_pct / 100
                qty_by_risk = risk_amount / risk_per_unit
                qty_by_asset = (balance * cfg.max_asset_exposure_pct / 100) / entry
                qty = max(0.0, min(qty_by_risk, qty_by_asset))
                notional = qty * entry
                if notional >= 5:
                    equity_before = balance
                    entry_fee = notional * fee_rate
                    balance -= entry_fee
                    actual_stop = float(scored.stop_price)
                    actual_target = entry + cfg.target_r_multiple * (entry - actual_stop)
                    pos = {
                        "entry": entry,
                        "qty": qty,
                        "stop": actual_stop,
                        "target": actual_target,
                        "entry_fee": entry_fee,
                        "open_i": i,
                        "signal_time": pd.Timestamp(r.open_time).isoformat(),
                        "opened_at": pd.Timestamp(nxt.open_time).isoformat(),
                        "score": float(scored.score),
                        "regime": float(regime),
                        "factors": {k: float(v) for k, v in scored.factors.items()},
                        "equity_before": equity_before,
                    }

        if pos is not None:
            exit_px = None
            reason = None
            if float(nxt.low) <= pos["stop"]:
                exit_px = pos["stop"] * (1 - slip)
                reason = "stop"
            elif float(nxt.high) >= pos["target"]:
                exit_px = pos["target"] * (1 - slip)
                reason = "target"
            elif i - pos["open_i"] >= max_hold_bars:
                exit_px = float(nxt.close) * (1 - slip)
                reason = "time"

            if exit_px is not None:
                exit_notional = pos["qty"] * exit_px
                exit_fee = exit_notional * fee_rate
                gross = (exit_px - pos["entry"]) * pos["qty"]
                balance += gross - exit_fee
                pnl = gross - pos["entry_fee"] - exit_fee
                planned_stop_fill = pos["stop"] * (1 - slip)
                planned_stop_fee = planned_stop_fill * pos["qty"] * fee_rate
                initial_risk_dollars = (
                    (pos["entry"] - planned_stop_fill) * pos["qty"]
                    + pos["entry_fee"]
                    + planned_stop_fee
                )
                trades.append(
                    {
                        "opened_at": pos["opened_at"],
                        "closed_at": pd.Timestamp(nxt.close_time).isoformat(),
                        "signal_time": pos["signal_time"],
                        "entry": pos["entry"],
                        "exit": float(exit_px),
                        "stop": pos["stop"],
                        "target": pos["target"],
                        "qty": pos["qty"],
                        "score": pos["score"],
                        "regime": pos["regime"],
                        "factors": pos["factors"],
                        "pnl": float(pnl),
                        "ret": float(pnl / (pos["entry"] * pos["qty"]) * 100),
                        "r": float(pnl / initial_risk_dollars if initial_risk_dollars > 0 else 0.0),
                        "reason": reason,
                        "equity_before": float(pos["equity_before"]),
                        "equity_after": float(balance),
                    }
                )
                pos = None

        if pos is None:
            marked_equity = balance
        else:
            gross_unrealized = (float(nxt.close) - pos["entry"]) * pos["qty"]
            estimated_exit_fee = pos["qty"] * float(nxt.close) * fee_rate
            marked_equity = balance + gross_unrealized - estimated_exit_fee
        equity_points.append({"time": pd.Timestamp(nxt.close_time).isoformat(), "equity": float(marked_equity)})

    if pos is not None:
        final_row = df.iloc[-1]
        final_close = float(final_row.close) * (1 - slip)
        exit_fee = pos["qty"] * final_close * fee_rate
        gross = (final_close - pos["entry"]) * pos["qty"]
        balance += gross - exit_fee
        pnl = gross - pos["entry_fee"] - exit_fee
        planned_stop_fill = pos["stop"] * (1 - slip)
        planned_stop_fee = planned_stop_fill * pos["qty"] * fee_rate
        initial_risk_dollars = (
            (pos["entry"] - planned_stop_fill) * pos["qty"]
            + pos["entry_fee"]
            + planned_stop_fee
        )
        trades.append(
            {
                "opened_at": pos["opened_at"],
                "closed_at": pd.Timestamp(final_row.close_time).isoformat(),
                "signal_time": pos["signal_time"],
                "entry": pos["entry"],
                "exit": final_close,
                "stop": pos["stop"],
                "target": pos["target"],
                "qty": pos["qty"],
                "score": pos["score"],
                "regime": pos["regime"],
                "factors": pos["factors"],
                "pnl": float(pnl),
                "ret": float(pnl / (pos["entry"] * pos["qty"]) * 100),
                "r": float(pnl / initial_risk_dollars if initial_risk_dollars > 0 else 0.0),
                "reason": "end",
                "equity_before": float(pos["equity_before"]),
                "equity_after": float(balance),
            }
        )
        equity_points.append({"time": pd.Timestamp(final_row.close_time).isoformat(), "equity": float(balance)})

    return df, trades, balance, equity_points


def _create_research_run(
    symbol: str,
    interval: str,
    asset_df: pd.DataFrame,
    btc_df: pd.DataFrame,
    *,
    days: int,
    split_date: str,
    initial: float,
    cfg: AppConfig,
) -> BacktestRun:
    df, trades, final_balance, equity_points = _simulate_research_frames(
        symbol,
        interval,
        asset_df,
        btc_df,
        cfg=cfg,
        initial=initial,
    )

    full = _trade_metrics(trades)
    train = _trade_metrics(_subset_by_open_time(trades, end=split_date))
    oos = _trade_metrics(_subset_by_open_time(trades, start=split_date))

    full_equity = _equity_period_metrics(equity_points)
    train_equity = _equity_period_metrics(equity_points, end=split_date)
    oos_equity = _equity_period_metrics(equity_points, start=split_date)
    full.update(full_equity)
    train.update(train_equity)
    oos.update(oos_equity)

    bands = _score_band_diagnostics(trades, split_date)
    final_date = pd.Timestamp(df.open_time.iloc[-1])
    walk = _walk_forward_windows(
        trades,
        equity_points,
        split_date,
        final_date,
        RESEARCH_WALK_FORWARD_DAYS,
    )
    candidate = _research_candidate(oos, walk)

    simulation_start = pd.Timestamp(df.open_time.iloc[min(210, len(df) - 1)])
    simulation_end = pd.Timestamp(df.open_time.iloc[-1])

    return BacktestRun.objects.create(
        symbol=symbol,
        timeframe=interval,
        start_date=simulation_start.to_pydatetime(),
        end_date=simulation_end.to_pydatetime(),
        trades=full["trades"],
        win_rate_pct=full["win_rate_pct"],
        net_return_pct=(final_balance / initial - 1) * 100,
        profit_factor=full["profit_factor"],
        max_drawdown_pct=full["max_drawdown_pct"],
        expectancy_pct=full["expectancy_pct"],
        sharpe=full["sharpe"],
        results={
            "strategy_version": STRATEGY_VERSION,
            "backtest_engine_version": BACKTEST_ENGINE_VERSION,
            "research_engine_version": RESEARCH_ENGINE_VERSION,
            "research_mode": True,
            "source_interval": "1h",
            "resampled": interval == "4h",
            "lookback_days": int(days),
            "split_date": split_date,
            "walk_forward_days": RESEARCH_WALK_FORWARD_DAYS,
            "fees_bps": cfg.fee_bps,
            "slippage_bps": cfg.slippage_bps,
            "signal_threshold": cfg.signal_threshold,
            "max_hold_hours": 24,
            "initial": initial,
            "final": final_balance,
            "full_metrics": full,
            "train_metrics": train,
            "oos_metrics": oos,
            "score_bands": bands,
            "walk_forward": walk,
            "candidate": candidate,
            "trades": trades[-100:],
        },
    )


def run_research_backtest(
    symbol="BTCUSDT",
    interval="1h",
    days=RESEARCH_DEFAULT_DAYS,
    split_date=RESEARCH_SPLIT_DATE,
    initial=10000.0,
):
    """Run one multi-year Research Engine v2.2 study.

    Strategy parameters are frozen. The 2026 segment is kept separate from the
    pre-2026 training/history segment, and the OOS segment is broken into
    sequential 90-day windows to test temporal stability.
    """
    if interval not in RESEARCH_INTERVALS:
        raise ValueError("Research backtests support only 1h and 4h")
    if int(days) not in (730, 1095):
        raise ValueError("Research lookback must be 2 years or 3 years")

    cfg = AppConfig.current()
    client = BinanceClient(mode="paper")
    asset_raw = _cached_raw_research_1h(client, symbol, int(days))
    btc_raw = asset_raw.copy() if symbol == "BTCUSDT" else _cached_raw_research_1h(client, "BTCUSDT", int(days))

    asset_df = enrich(_research_resample(asset_raw, interval))
    btc_df = enrich(_research_resample(btc_raw, interval))
    return _create_research_run(
        symbol,
        interval,
        asset_df,
        btc_df,
        days=int(days),
        split_date=split_date,
        initial=initial,
        cfg=cfg,
    )


def run_research_symbol(
    symbol: str,
    intervals=RESEARCH_INTERVALS,
    days=RESEARCH_DEFAULT_DAYS,
    split_date=RESEARCH_SPLIT_DATE,
    initial=10000.0,
):
    """Run 1h and 4h multi-year research for one symbol from one 1h download."""
    cfg = AppConfig.current()
    client = BinanceClient(mode="paper")
    days = int(days)
    asset_raw = _cached_raw_research_1h(client, symbol, days)
    btc_raw = asset_raw.copy() if symbol == "BTCUSDT" else _cached_raw_research_1h(client, "BTCUSDT", days)

    runs = []
    errors = []
    for interval in intervals:
        if interval not in RESEARCH_INTERVALS:
            errors.append({"interval": interval, "error": "unsupported research timeframe"})
            continue
        try:
            asset_df = enrich(_research_resample(asset_raw, interval))
            btc_df = enrich(_research_resample(btc_raw, interval))
            run = _create_research_run(
                symbol,
                interval,
                asset_df,
                btc_df,
                days=days,
                split_date=split_date,
                initial=initial,
                cfg=cfg,
            )
            runs.append(run)
        except Exception as exc:
            errors.append({"interval": interval, "error": str(exc)})
    return runs, errors
