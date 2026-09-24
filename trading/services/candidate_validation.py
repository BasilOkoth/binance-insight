from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading.constants import (
    STRATEGY_VERSION,
    RESEARCH_ENGINE_VERSION,
    RESEARCH_DEFAULT_DAYS,
    RESEARCH_SPLIT_DATE,
    RESEARCH_WALK_FORWARD_DAYS,
    CANDIDATE_VALIDATION_ENGINE_VERSION,
    CANDIDATE_THRESHOLD_GRID,
    CANDIDATE_FORWARD_THRESHOLD,
    CANDIDATE_VALIDATION_SET,
)
from trading.models import AppConfig, BacktestRun
from .binance_client import BinanceClient
from .indicators import enrich, bars_per_24h
from .scoring import score_latest, is_actionable_setup, regime_score_from_values
from .backtest import (
    INTERVAL_MINUTES,
    _attach_btc_regime,
    _cached_raw_research_1h,
    _research_resample,
    _trade_metrics,
    _subset_by_open_time,
    _equity_period_metrics,
    _walk_forward_windows,
)


@dataclass(frozen=True)
class CandidateKey:
    symbol: str
    interval: str


def allowed_candidate(symbol: str, interval: str) -> bool:
    return (symbol.upper(), interval) in set(CANDIDATE_VALIDATION_SET)


def _prepare_signal_rows(asset_df: pd.DataFrame, btc_df: pd.DataFrame, cfg: AppConfig):
    """Score every completed historical bar once, independent of threshold.

    This makes the threshold grid efficient: all non-threshold entry guards are
    frozen once, then each candidate threshold is replayed over the same signal
    stream. No threshold is allowed to alter the indicators or future data.
    """
    df = _attach_btc_regime(asset_df, btc_df)
    if len(df) < 220:
        raise RuntimeError("Not enough completed candles for candidate validation")

    inferred_bars_per_day = bars_per_24h(asset_df)
    prepared = []
    for i in range(210, len(df) - 1):
        row = df.iloc[i]
        nxt = df.iloc[i + 1]
        required = ["btc_close", "btc_ema20", "btc_ema50", "btc_ema200", "btc_rsi14"]
        if any(pd.isna(row[c]) for c in required):
            continue

        regime = regime_score_from_values(
            row.btc_close,
            row.btc_ema20,
            row.btc_ema50,
            row.btc_ema200,
            row.btc_rsi14,
        )
        qv = (
            float(row.quote_volume_24h)
            if not pd.isna(row.quote_volume_24h)
            else max(float(row.quote_volume) * inferred_bars_per_day, 1_000_000)
        )
        spread_bps = 5.0
        scored = score_latest(
            row,
            qv,
            spread_bps,
            regime,
            cfg.stop_atr_multiple,
            cfg.target_r_multiple,
        )
        # Threshold=0 keeps every existing non-score quality guard active.
        base_actionable = is_actionable_setup(row, scored, regime, spread_bps, 0.0)
        prepared.append(
            {
                "i": i,
                "row": row,
                "nxt": nxt,
                "score": float(scored.score),
                "stop": float(scored.stop_price),
                "regime": float(regime),
                "factors": {k: float(v) for k, v in scored.factors.items()},
                "base_actionable": bool(base_actionable),
            }
        )
    return df, prepared


def _max_drawdown(values):
    peak = -1e99
    max_dd = 0.0
    for value in values:
        value = float(value)
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100)
    return max_dd


def _simulate_threshold(prepared, df, interval: str, threshold: float, cfg: AppConfig, initial: float):
    balance = float(initial)
    fee_rate = float(cfg.fee_bps) / 10000.0
    slip = float(cfg.slippage_bps) / 10000.0
    max_hold_bars = max(1, int(round((24 * 60) / INTERVAL_MINUTES[interval])))
    trades = []
    pos = None

    first_time = pd.Timestamp(df.open_time.iloc[210]).isoformat()
    equity_points = [{"time": first_time, "equity": balance}]

    for signal in prepared:
        i = signal["i"]
        nxt = signal["nxt"]

        if pos is None and signal["base_actionable"] and signal["score"] >= float(threshold):
            entry = float(nxt.open) * (1 + slip)
            stop = float(signal["stop"])
            if stop < entry:
                risk_per_unit = max(entry - stop, entry * 0.002)
                risk_amount = balance * float(cfg.risk_per_trade_pct) / 100.0
                qty_by_risk = risk_amount / risk_per_unit
                qty_by_asset = (balance * float(cfg.max_asset_exposure_pct) / 100.0) / entry
                qty = max(0.0, min(qty_by_risk, qty_by_asset))
                notional = qty * entry
                if notional >= 5:
                    equity_before = balance
                    entry_fee = notional * fee_rate
                    balance -= entry_fee
                    target = entry + float(cfg.target_r_multiple) * (entry - stop)
                    pos = {
                        "entry": entry,
                        "qty": qty,
                        "stop": stop,
                        "target": target,
                        "entry_fee": entry_fee,
                        "open_i": i,
                        "opened_at": pd.Timestamp(nxt.open_time).isoformat(),
                        "signal_time": pd.Timestamp(signal["row"].open_time).isoformat(),
                        "score": signal["score"],
                        "regime": signal["regime"],
                        "factors": signal["factors"],
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
                initial_risk = (
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
                        "r": float(pnl / initial_risk if initial_risk > 0 else 0.0),
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
        exit_px = float(final_row.close) * (1 - slip)
        exit_fee = pos["qty"] * exit_px * fee_rate
        gross = (exit_px - pos["entry"]) * pos["qty"]
        balance += gross - exit_fee
        pnl = gross - pos["entry_fee"] - exit_fee
        planned_stop_fill = pos["stop"] * (1 - slip)
        planned_stop_fee = planned_stop_fill * pos["qty"] * fee_rate
        initial_risk = (
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
                "exit": float(exit_px),
                "stop": pos["stop"],
                "target": pos["target"],
                "qty": pos["qty"],
                "score": pos["score"],
                "regime": pos["regime"],
                "factors": pos["factors"],
                "pnl": float(pnl),
                "ret": float(pnl / (pos["entry"] * pos["qty"]) * 100),
                "r": float(pnl / initial_risk if initial_risk > 0 else 0.0),
                "reason": "end",
                "equity_before": float(pos["equity_before"]),
                "equity_after": float(balance),
            }
        )
        equity_points.append({"time": pd.Timestamp(final_row.close_time).isoformat(), "equity": float(balance)})

    return trades, equity_points, balance


def _period_metrics(trades, equity_points, *, start=None, end=None):
    subset = _subset_by_open_time(trades, start=start, end=end)
    metrics = _trade_metrics(subset)
    equity = _equity_period_metrics(equity_points, start=start, end=end)
    metrics.update(equity)
    return metrics


def _threshold_pass(oos: dict, walk: dict) -> bool:
    return bool(
        oos.get("trades", 0) >= 20
        and oos.get("net_return_pct", 0) > 0
        and oos.get("profit_factor", 0) >= 1.25
        and oos.get("expectancy_r", 0) > 0
        and walk.get("evaluable_windows", 0) >= 2
        and walk.get("positive_window_pct", 0) >= 60
    )


def run_candidate_validation(
    symbol: str,
    interval: str,
    *,
    days: int = RESEARCH_DEFAULT_DAYS,
    split_date: str = RESEARCH_SPLIT_DATE,
    initial: float = 10000.0,
):
    symbol = symbol.upper().strip()
    if not allowed_candidate(symbol, interval):
        raise ValueError("This pair/timeframe is not in the candidate-validation set")
    if int(days) != RESEARCH_DEFAULT_DAYS:
        raise ValueError("Candidate validation is frozen to the 3-year research horizon")

    cfg = AppConfig.current()
    client = BinanceClient(mode="paper")
    asset_raw = _cached_raw_research_1h(client, symbol, int(days))
    btc_raw = asset_raw.copy() if symbol == "BTCUSDT" else _cached_raw_research_1h(client, "BTCUSDT", int(days))
    asset_df = enrich(_research_resample(asset_raw, interval))
    btc_df = enrich(_research_resample(btc_raw, interval))
    df, prepared = _prepare_signal_rows(asset_df, btc_df, cfg)

    threshold_rows = []
    selected = None
    final_date = pd.Timestamp(df.open_time.iloc[-1])
    for threshold in CANDIDATE_THRESHOLD_GRID:
        trades, equity_points, final_balance = _simulate_threshold(
            prepared, df, interval, float(threshold), cfg, initial
        )
        full = _period_metrics(trades, equity_points)
        train = _period_metrics(trades, equity_points, end=split_date)
        oos = _period_metrics(trades, equity_points, start=split_date)
        walk = _walk_forward_windows(
            trades,
            equity_points,
            split_date,
            final_date,
            RESEARCH_WALK_FORWARD_DAYS,
        )
        row = {
            "threshold": float(threshold),
            "full": full,
            "train": train,
            "oos": oos,
            "walk_forward": walk,
            "passes_internal_checks": _threshold_pass(oos, walk),
            "forward_hypothesis": float(threshold) == float(CANDIDATE_FORWARD_THRESHOLD),
            "final_balance": float(final_balance),
        }
        threshold_rows.append(row)
        if row["forward_hypothesis"]:
            selected = row

    if selected is None:
        selected = threshold_rows[0]

    oos_exp = [float(row["oos"].get("expectancy_r", 0.0)) for row in threshold_rows]
    finite_pf = [
        float(row["oos"].get("profit_factor", 0.0))
        for row in threshold_rows
        if float(row["oos"].get("profit_factor", 0.0)) < 900
    ]
    higher_steps = sum(
        1 for left, right in zip(oos_exp, oos_exp[1:]) if right >= left
    )
    smoothness_pct = higher_steps / max(len(oos_exp) - 1, 1) * 100

    top = selected["full"]
    run = BacktestRun.objects.create(
        symbol=symbol,
        timeframe=interval,
        start_date=df.open_time.iloc[0].to_pydatetime(),
        end_date=df.open_time.iloc[-1].to_pydatetime(),
        trades=int(top.get("trades", 0)),
        win_rate_pct=float(top.get("win_rate_pct", 0.0)),
        net_return_pct=float(top.get("net_return_pct", 0.0)),
        profit_factor=float(top.get("profit_factor", 0.0)),
        max_drawdown_pct=float(top.get("max_drawdown_pct", 0.0)),
        expectancy_pct=float(top.get("expectancy_pct", 0.0)),
        sharpe=float(top.get("sharpe", 0.0)),
        results={
            "strategy_version": STRATEGY_VERSION,
            "research_engine_version": RESEARCH_ENGINE_VERSION,
            "candidate_validation_engine_version": CANDIDATE_VALIDATION_ENGINE_VERSION,
            "candidate_validation_mode": True,
            "lookback_days": int(days),
            "split_date": split_date,
            "source_interval": "1h",
            "selected_forward_threshold": float(CANDIDATE_FORWARD_THRESHOLD),
            "threshold_grid": threshold_rows,
            "selected_threshold_result": selected,
            "oos_expectancy_non_decreasing_steps_pct": float(smoothness_pct),
            "median_oos_pf": float(np.median(finite_pf)) if finite_pf else 0.0,
            "note": "Threshold 80 is a forward-test hypothesis chosen before this grid; grid results are exploratory and must not be treated as independent confirmation.",
        },
    )
    return run


def run_candidate_validation_set():
    runs = []
    errors = []
    for symbol, interval in CANDIDATE_VALIDATION_SET:
        try:
            runs.append(run_candidate_validation(symbol, interval))
        except Exception as exc:
            errors.append({"symbol": symbol, "interval": interval, "error": str(exc)})
    return runs, errors
