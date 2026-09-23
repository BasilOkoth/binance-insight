from __future__ import annotations
import math
from dataclasses import dataclass
import numpy as np
from .indicators import linear, inverse, clamp
from trading.constants import (
    MAX_ACTIONABLE_SPREAD_BPS,
    MIN_BREAKOUT_VOLUME_RATIO,
    MIN_MOMENTUM_SCORE,
    MIN_TREND_SCORE,
    MAX_BREAKOUT_EXTENSION_PCT,
    MAX_ENTRY_RSI,
)

WEIGHTS = {
    "trend": 0.25,
    "momentum": 0.20,
    "volume": 0.15,
    "breakout": 0.15,
    "volatility": 0.10,
    "liquidity": 0.10,
    "regime": 0.05,
}


@dataclass
class ScoreResult:
    score: float
    factors: dict
    stop_price: float
    target_price: float
    rationale: list[str]
    warnings: list[str]


def smooth_volume_score(volume_ratio: float) -> float:
    """Smooth score that avoids collapsing normal volume into only 0 or 100."""
    try:
        x = float(volume_ratio)
    except (TypeError, ValueError):
        return 50.0
    if math.isnan(x):
        return 50.0
    anchors = [
        (0.30, 5.0),
        (0.50, 15.0),
        (0.75, 30.0),
        (1.00, 50.0),
        (1.25, 65.0),
        (1.50, 75.0),
        (2.00, 90.0),
        (2.50, 100.0),
    ]
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            frac = (x - x0) / (x1 - x0)
            return clamp(y0 + frac * (y1 - y0))
    return 50.0


def regime_score_from_values(close, ema20, ema50, ema200, rsi14) -> float:
    score = 0.0
    score += 35 if float(close) > float(ema200) else 5
    score += 30 if float(ema50) > float(ema200) else 5
    score += 20 if float(close) > float(ema20) else 5
    score += 15 if float(rsi14) >= 50 else 3
    return min(100.0, float(score))


def confirmed_breakout(row) -> bool:
    try:
        prev_high = float(row.high20_prev)
        close = float(row.close)
        return not math.isnan(prev_high) and close > prev_high
    except (TypeError, ValueError, AttributeError):
        return False


def _breakout_score(row) -> float:
    p = float(row.close)
    try:
        prev_high = float(row.high20_prev)
    except (TypeError, ValueError):
        return 30.0
    if math.isnan(prev_high) or prev_high <= 0:
        return 30.0

    distance = (p / prev_high - 1) * 100
    if distance < -2.0:
        return linear(distance, -8.0, -2.0) * 0.35
    if distance < -0.5:
        return 20.0 + linear(distance, -2.0, -0.5) * 0.25
    if distance < 0:
        # Approaching resistance is not a breakout.
        return 45.0 + linear(distance, -0.5, 0.0) * 0.15
    if distance <= 0.5:
        return 80.0 + linear(distance, 0.0, 0.5) * 0.10
    if distance <= 1.5:
        return 90.0 + linear(distance, 0.5, 1.5) * 0.10
    # Penalize chasing an already extended move.
    return 20.0 + inverse(distance, 1.5, 6.0) * 0.70


def score_latest(
    row,
    quote_volume_24h: float,
    spread_bps: float,
    regime_score: float,
    stop_atr: float = 1.5,
    target_r: float = 2.2,
) -> ScoreResult:
    p = float(row.close)
    atr = max(float(row.atr14 or 0), p * 0.002)

    trend_parts = [
        100 if p > row.ema20 else 20,
        100 if row.ema20 > row.ema50 else 25,
        100 if row.ema50 > row.ema200 else 25,
        linear(row.ema50_slope, -1.5, 1.5),
    ]
    trend = sum(trend_parts) / len(trend_parts)

    rsi = float(row.rsi14)
    rsi_score = (
        100
        if 52 <= rsi <= 68
        else (linear(rsi, 40, 52) if rsi < 52 else inverse(rsi, 68, 82))
    )
    momentum = (rsi_score + linear(row.roc12, -4, 6)) / 2
    volume = smooth_volume_score(row.volume_ratio)
    breakout = _breakout_score(row)

    atr_pct = atr / p * 100
    volatility = (
        100
        if 0.7 <= atr_pct <= 3.5
        else (linear(atr_pct, 0.2, 0.7) if atr_pct < 0.7 else inverse(atr_pct, 3.5, 8))
    )

    liq_volume = linear(math.log10(max(quote_volume_24h, 1)), 5.5, 9.0)
    liq_spread = inverse(spread_bps, 2, 35)
    liquidity = (liq_volume + liq_spread) / 2

    factors = {
        "trend": clamp(trend),
        "momentum": clamp(momentum),
        "volume": clamp(volume),
        "breakout": clamp(breakout),
        "volatility": clamp(volatility),
        "liquidity": clamp(liquidity),
        "regime": clamp(regime_score),
    }
    total = sum(factors[k] * w for k, w in WEIGHTS.items())

    stop = p - stop_atr * atr
    risk = max(p - stop, p * 0.001)
    target = p + target_r * risk

    rationale = []
    warnings = []
    if factors["trend"] >= 70:
        rationale.append("Trend structure is constructive across short, medium and long moving averages")
    if factors["volume"] >= 70:
        rationale.append("Current participation is stronger than the recent volume baseline")
    if confirmed_breakout(row):
        rationale.append("The latest completed candle closed above the prior 20-candle resistance")
    elif factors["breakout"] >= 45:
        rationale.append("Price is approaching resistance, but a breakout has not yet been confirmed")
    if 52 <= rsi <= 68:
        rationale.append("Momentum is positive without being deeply overbought")

    if spread_bps > MAX_ACTIONABLE_SPREAD_BPS:
        warnings.append("Bid/ask spread is wider than the preferred execution threshold")
    if atr_pct > 5:
        warnings.append("Volatility is elevated")
    if rsi > 75:
        warnings.append("Momentum is stretched")
    if regime_score < 55:
        warnings.append("BTC market regime is not supportive")
    if not confirmed_breakout(row):
        warnings.append("Breakout is not confirmed on the latest completed candle")
    if float(row.volume_ratio or 0) < MIN_BREAKOUT_VOLUME_RATIO:
        warnings.append("Breakout participation is not yet strong enough")
    if not rationale:
        rationale.append("No single factor is dominant; this is a mixed setup")

    return ScoreResult(
        round(clamp(total), 1),
        {k: round(v, 1) for k, v in factors.items()},
        round(stop, 8),
        round(target, 8),
        rationale,
        warnings,
    )


def is_actionable_setup(row, scored: ScoreResult, regime_score: float, spread_bps: float, signal_threshold: float) -> bool:
    try:
        volume_ratio = float(row.volume_ratio)
        prev_high = float(row.high20_prev)
        close = float(row.close)
        rsi = float(row.rsi14)
        extension_pct = (close / prev_high - 1) * 100 if prev_high > 0 else 999.0
    except (TypeError, ValueError, AttributeError):
        volume_ratio = 0.0
        extension_pct = 999.0
        rsi = 100.0
    return all(
        [
            scored.score >= float(signal_threshold),
            regime_score >= 55.0,
            spread_bps <= MAX_ACTIONABLE_SPREAD_BPS,
            confirmed_breakout(row),
            0.0 < extension_pct <= MAX_BREAKOUT_EXTENSION_PCT,
            volume_ratio >= MIN_BREAKOUT_VOLUME_RATIO,
            rsi <= MAX_ENTRY_RSI,
            scored.factors.get("trend", 0) >= MIN_TREND_SCORE,
            scored.factors.get("momentum", 0) >= MIN_MOMENTUM_SCORE,
        ]
    )
