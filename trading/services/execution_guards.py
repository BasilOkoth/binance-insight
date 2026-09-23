from __future__ import annotations
from datetime import timedelta, timezone as dt_timezone
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from trading.constants import (
    MAX_SIGNAL_AGE_MINUTES,
    REENTRY_COOLDOWN_MINUTES,
    STRATEGY_VERSION,
)
from trading.models import Trade


def strategy_trade_queryset(mode: str):
    return Trade.objects.filter(mode=mode, metadata__strategy_version=STRATEGY_VERSION)


def signal_key(signal) -> str:
    data = signal.data or {}
    candle_open = data.get("candle_open_time") or signal.observed_at.isoformat()
    return f"{STRATEGY_VERSION}:{signal.symbol}:{signal.timeframe}:{candle_open}"


def signal_is_current_version(signal) -> bool:
    return (signal.data or {}).get("strategy_version") == STRATEGY_VERSION


def signal_is_fresh(signal, max_age_minutes: int = MAX_SIGNAL_AGE_MINUTES) -> bool:
    if not signal_is_current_version(signal):
        return False
    raw_close = (signal.data or {}).get("candle_close_time")
    if not raw_close:
        return signal.observed_at >= timezone.now() - timedelta(minutes=max_age_minutes)
    dt = parse_datetime(raw_close)
    if dt is None:
        return False
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, dt_timezone.utc)
    age = timezone.now() - dt
    return timedelta(seconds=-5) <= age <= timedelta(minutes=max_age_minutes)


def signal_already_used(mode: str, signal) -> bool:
    return strategy_trade_queryset(mode).filter(metadata__signal_key=signal_key(signal)).exists()


def reentry_cooldown_active(mode: str, symbol: str, minutes: int = REENTRY_COOLDOWN_MINUTES) -> bool:
    cutoff = timezone.now() - timedelta(minutes=minutes)
    return strategy_trade_queryset(mode).filter(
        symbol=symbol,
        status="closed",
        closed_at__gte=cutoff,
    ).exists()


def projected_exposure_allowed(current_exposure: float, proposed_notional: float, equity: float, max_total_pct: float) -> bool:
    if equity <= 0:
        return False
    limit = equity * max_total_pct / 100.0
    return current_exposure + proposed_notional <= limit + 1e-9


def signal_can_open(mode: str, signal) -> tuple[bool, str]:
    if not signal.is_actionable:
        return False, "Signal is not actionable"
    if not signal_is_current_version(signal):
        return False, "Signal belongs to an older strategy version"
    if not signal_is_fresh(signal):
        return False, "Signal is stale"
    if signal_already_used(mode, signal):
        return False, "This completed candle has already been traded"
    if reentry_cooldown_active(mode, signal.symbol):
        return False, f"{signal.symbol} is still in the post-exit cooldown"
    return True, ""
