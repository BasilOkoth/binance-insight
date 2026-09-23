from __future__ import annotations
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum
from trading.models import AppConfig, MarketSignal, PaperAccount, Trade, AuditEvent
from .risk import position_size, daily_loss_limit_breached
from .binance_client import BinanceClient


def _today_realized():
    today = timezone.localdate()
    return (
        Trade.objects.filter(
            mode="paper",
            status="closed",
            closed_at__date=today,
        ).aggregate(v=Sum("pnl"))["v"]
        or 0.0
    )


def mark_to_market(account: PaperAccount, client=None):
    client = client or BinanceClient(mode="paper")
    open_trades = list(Trade.objects.filter(mode="paper", status="open"))
    value = 0

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

    account.save(
        update_fields=[
            "equity",
            "peak_equity",
            "max_drawdown_pct",
            "updated_at",
        ]
    )
    return account


@transaction.atomic
def maybe_open_from_signal(signal: MarketSignal):
    cfg = AppConfig.current()
    acct = PaperAccount.primary()
    mark_to_market(acct)

    if not signal.is_actionable:
        return None

    if Trade.objects.filter(
        mode="paper",
        status="open",
        symbol=signal.symbol,
    ).exists():
        return None

    if Trade.objects.filter(mode="paper", status="open").count() >= cfg.max_open_positions:
        return None

    if daily_loss_limit_breached(
        _today_realized(),
        acct.starting_cash,
        cfg.max_daily_loss_pct,
    ):
        AuditEvent.objects.create(
            level="WARN",
            category="paper-risk",
            message="Daily paper loss limit reached; no new positions",
        )
        return None

    total_open = sum(
        t.entry_price * t.quantity
        for t in Trade.objects.filter(mode="paper", status="open")
    )

    if total_open >= acct.equity * cfg.max_total_exposure_pct / 100:
        return None

    sizing = position_size(
        acct.equity,
        acct.cash,
        signal.price,
        signal.stop_price,
        cfg.risk_per_trade_pct,
        cfg.max_asset_exposure_pct,
    )

    if not sizing.allowed:
        return None

    slippage = cfg.slippage_bps / 10000
    fee_rate = cfg.fee_bps / 10000
    entry = signal.price * (1 + slippage)
    notional = sizing.quantity * entry
    fee = notional * fee_rate

    if notional + fee > acct.cash:
        return None

    acct.cash -= notional + fee
    acct.save(update_fields=["cash", "updated_at"])

    trade = Trade.objects.create(
        mode="paper",
        symbol=signal.symbol,
        entry_price=entry,
        quantity=sizing.quantity,
        stop_price=signal.stop_price,
        target_price=signal.target_price,
        entry_fee=fee,
        signal_score=signal.score,
        risk_amount=sizing.risk_amount,
        metadata={"signal_id": signal.id},
    )

    AuditEvent.objects.create(
        category="paper",
        message=f"Opened paper trade {signal.symbol}",
        data={
            "trade_id": trade.id,
            "entry": entry,
            "qty": sizing.quantity,
        },
    )

    return trade


@transaction.atomic
def update_open_paper_trades():
    cfg = AppConfig.current()
    acct = PaperAccount.primary()
    client = BinanceClient(mode="paper")
    closed = []

    for t in Trade.objects.select_for_update().filter(
        mode="paper",
        status="open",
    ):
        try:
            px = client.price(t.symbol)
        except Exception as e:
            AuditEvent.objects.create(
                level="WARN",
                category="paper",
                message=f"Price failed {t.symbol}: {e}",
            )
            continue

        reason = None
        if px <= t.stop_price:
            reason = "stop"
        elif px >= t.target_price:
            reason = "target"

        if not reason:
            continue

        slippage = cfg.slippage_bps / 10000
        fee_rate = cfg.fee_bps / 10000
        exit_px = px * (1 - slippage)
        proceeds = t.quantity * exit_px
        fee = proceeds * fee_rate

        acct.cash += proceeds - fee

        pnl = (
            (exit_px - t.entry_price) * t.quantity
            - t.entry_fee
            - fee
        )
        basis = t.entry_price * t.quantity + t.entry_fee

        t.exit_price = exit_px
        t.exit_fee = fee
        t.pnl = pnl
        t.pnl_pct = (pnl / basis * 100 if basis else 0)
        t.exit_reason = reason
        t.status = "closed"
        t.closed_at = timezone.now()
        t.save()

        AuditEvent.objects.create(
            category="paper",
            message=(
                f"Closed paper trade {t.symbol} at {reason} · "
                f"P/L ${pnl:.2f} ({t.pnl_pct:.2f}%)"
            ),
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
    cutoff = timezone.now() - timedelta(minutes=30)

    for sig in (
        MarketSignal.objects.filter(
            is_actionable=True,
            observed_at__gte=cutoff,
        )
        .order_by("-observed_at", "-score")[:200]
    ):
        if sig.symbol in seen:
            continue
        seen.add(sig.symbol)
        latest.append(sig)

    opened = []
    for sig in sorted(latest, key=lambda s: s.score, reverse=True):
        t = maybe_open_from_signal(sig)
        if t:
            opened.append(t)

    mark_to_market(PaperAccount.primary())
    return opened
