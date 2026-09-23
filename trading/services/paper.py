from __future__ import annotations
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum
from trading.constants import STRATEGY_VERSION, MAX_SIGNAL_AGE_MINUTES
from trading.models import AppConfig, MarketSignal, PaperAccount, Trade, AuditEvent
from .risk import position_size, daily_loss_limit_breached
from .binance_client import BinanceClient
from .execution_guards import (
    projected_exposure_allowed,
    signal_can_open,
    signal_key,
    strategy_trade_queryset,
)


def _paper_trades():
    return strategy_trade_queryset("paper")


def _today_realized():
    today = timezone.localdate()
    return (
        _paper_trades().filter(status="closed", closed_at__date=today).aggregate(v=Sum("pnl"))["v"]
        or 0.0
    )


def mark_to_market(account: PaperAccount, client=None):
    client = client or BinanceClient(mode="paper")
    open_trades = list(_paper_trades().filter(status="open"))
    value = 0.0

    for t in open_trades:
        try:
            value += t.quantity * client.price(t.symbol)
        except Exception:
            value += t.quantity * t.entry_price

    account.equity = account.cash + value
    account.peak_equity = max(account.peak_equity, account.equity)
    if account.peak_equity > 0:
        dd = (account.peak_equity - account.equity) / account.peak_equity * 100
        account.max_drawdown_pct = max(account.max_drawdown_pct, dd)

    account.save(update_fields=["equity", "peak_equity", "max_drawdown_pct", "updated_at"])
    return account


@transaction.atomic
def maybe_open_from_signal(signal: MarketSignal):
    cfg = AppConfig.current()
    acct = PaperAccount.primary()
    mark_to_market(acct)

    allowed, reason = signal_can_open("paper", signal)
    if not allowed:
        return None

    if _paper_trades().filter(status="open", symbol=signal.symbol).exists():
        return None
    if _paper_trades().filter(status="open").count() >= cfg.max_open_positions:
        return None

    if daily_loss_limit_breached(_today_realized(), acct.starting_cash, cfg.max_daily_loss_pct):
        AuditEvent.objects.create(
            level="WARN",
            category="paper-risk",
            message="Daily Strategy v2 paper loss limit reached; no new positions",
        )
        return None

    current_exposure = max(float(acct.equity) - float(acct.cash), 0.0)
    client = BinanceClient(mode="paper")
    try:
        current_price = client.price(signal.symbol)
    except Exception:
        current_price = signal.price
    max_drift = max(float(signal.atr) * 0.75, float(signal.price) * 0.005)
    if abs(current_price - float(signal.price)) > max_drift:
        return None
    if current_price <= signal.stop_price:
        return None

    slippage = cfg.slippage_bps / 10000
    expected_entry = current_price * (1 + slippage)
    sizing = position_size(
        acct.equity,
        acct.cash,
        expected_entry,
        signal.stop_price,
        cfg.risk_per_trade_pct,
        cfg.max_asset_exposure_pct,
    )
    if not sizing.allowed:
        return None
    if not projected_exposure_allowed(
        current_exposure,
        sizing.notional,
        acct.equity,
        cfg.max_total_exposure_pct,
    ):
        return None

    fee_rate = cfg.fee_bps / 10000
    entry = expected_entry
    actual_stop = float(signal.stop_price)
    if actual_stop >= entry:
        return None
    actual_risk_per_unit = entry - actual_stop
    actual_target = entry + cfg.target_r_multiple * actual_risk_per_unit
    notional = sizing.quantity * entry
    fee = notional * fee_rate
    if notional + fee > acct.cash:
        return None

    acct.cash -= notional + fee
    acct.save(update_fields=["cash", "updated_at"])

    now_ms = int(timezone.now().timestamp() * 1000)
    trade = Trade.objects.create(
        mode="paper",
        symbol=signal.symbol,
        entry_price=entry,
        quantity=sizing.quantity,
        stop_price=actual_stop,
        target_price=actual_target,
        entry_fee=fee,
        signal_score=signal.score,
        risk_amount=sizing.risk_amount,
        metadata={
            "strategy_version": STRATEGY_VERSION,
            "signal_id": signal.id,
            "signal_key": signal_key(signal),
            "signal_candle_open_time": (signal.data or {}).get("candle_open_time"),
            "signal_candle_close_time": (signal.data or {}).get("candle_close_time"),
            "last_exit_check_ms": now_ms,
        },
    )

    AuditEvent.objects.create(
        category="paper",
        message=f"Strategy v{STRATEGY_VERSION} opened paper trade {signal.symbol}",
        data={"trade_id": trade.id, "entry": entry, "qty": sizing.quantity},
    )
    return trade


def _intrabar_exit(client: BinanceClient, trade: Trade):
    """Return (reason, level) if a 1-second candle touched stop/target since last check.

    If stop and target are both touched within the same 1-second candle, assume stop first.
    That conservative rule avoids overstating paper performance when event order is unknown.
    """
    now_ms = int(timezone.now().timestamp() * 1000)
    metadata = dict(trade.metadata or {})
    start_ms = int(metadata.get("last_exit_check_ms") or int(trade.opened_at.timestamp() * 1000))
    start_ms = max(int(trade.opened_at.timestamp() * 1000), start_ms - 1_000)
    cursor = start_ms
    rows = []

    while cursor <= now_ms and len(rows) < 5000:
        batch = client.klines(trade.symbol, "1s", limit=1000, start_time=cursor, end_time=now_ms)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 1_000
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 1000:
            break

    seen = set()
    for row in rows:
        open_time = int(row[0])
        if open_time < int(trade.opened_at.timestamp() * 1000):
            continue
        if open_time in seen:
            continue
        seen.add(open_time)
        high = float(row[2])
        low = float(row[3])
        hit_stop = low <= trade.stop_price
        hit_target = high >= trade.target_price
        if hit_stop:
            return "stop", float(trade.stop_price), now_ms
        if hit_target:
            return "target", float(trade.target_price), now_ms

    # Fallback catches current-minute moves if a kline response is temporarily empty.
    try:
        px = client.price(trade.symbol)
        if px <= trade.stop_price:
            return "stop", float(trade.stop_price), now_ms
        if px >= trade.target_price:
            return "target", float(trade.target_price), now_ms
    except Exception:
        pass

    return None, None, now_ms


@transaction.atomic
def update_open_paper_trades():
    cfg = AppConfig.current()
    acct = PaperAccount.primary()
    client = BinanceClient(mode="paper")
    closed = []

    for t in _paper_trades().select_for_update().filter(status="open"):
        try:
            reason, trigger_level, now_ms = _intrabar_exit(client, t)
        except Exception as e:
            AuditEvent.objects.create(
                level="WARN",
                category="paper",
                message=f"Intrabar exit check failed {t.symbol}: {e}",
            )
            continue

        if not reason:
            md = dict(t.metadata or {})
            md["last_exit_check_ms"] = now_ms
            t.metadata = md
            t.save(update_fields=["metadata"])
            continue

        slippage = cfg.slippage_bps / 10000
        fee_rate = cfg.fee_bps / 10000
        exit_px = trigger_level * (1 - slippage)
        proceeds = t.quantity * exit_px
        fee = proceeds * fee_rate
        acct.cash += proceeds - fee

        pnl = (exit_px - t.entry_price) * t.quantity - t.entry_fee - fee
        basis = t.entry_price * t.quantity + t.entry_fee
        t.exit_price = exit_px
        t.exit_fee = fee
        t.pnl = pnl
        t.pnl_pct = pnl / basis * 100 if basis else 0
        t.exit_reason = reason
        t.status = "closed"
        t.closed_at = timezone.now()
        md = dict(t.metadata or {})
        md["last_exit_check_ms"] = now_ms
        md["exit_trigger_level"] = trigger_level
        t.metadata = md
        t.save()

        AuditEvent.objects.create(
            category="paper",
            message=f"Closed Strategy v{STRATEGY_VERSION} paper trade {t.symbol} at {reason} · P/L ${pnl:.2f} ({t.pnl_pct:.2f}%)",
            data={
                "trade_id": t.id,
                "symbol": t.symbol,
                "reason": reason,
                "entry": t.entry_price,
                "exit": exit_px,
                "pnl": pnl,
                "pnl_pct": t.pnl_pct,
            },
        )
        closed.append(t)

    acct.save(update_fields=["cash", "updated_at"])
    mark_to_market(acct, client)
    return closed


def paper_cycle():
    update_open_paper_trades()

    latest = []
    seen = set()
    cutoff = timezone.now() - timedelta(minutes=MAX_SIGNAL_AGE_MINUTES)
    signals = MarketSignal.objects.filter(
        is_actionable=True,
        observed_at__gte=cutoff,
        data__strategy_version=STRATEGY_VERSION,
    ).order_by("-observed_at", "-score")[:200]

    for sig in signals:
        if sig.symbol in seen:
            continue
        seen.add(sig.symbol)
        latest.append(sig)

    opened = []
    for sig in sorted(latest, key=lambda s: s.score, reverse=True):
        trade = maybe_open_from_signal(sig)
        if trade:
            opened.append(trade)

    mark_to_market(PaperAccount.primary())
    return opened
