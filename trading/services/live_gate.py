from __future__ import annotations
from django.utils import timezone
from trading.constants import STRATEGY_VERSION
from trading.models import PaperAccount, Trade, LiveGate

DEFAULT_REQUIREMENTS = {
    "min_days": 30,
    "min_closed_trades": 100,
    "min_profit_factor": 1.25,
    "max_drawdown_pct": 12.0,
    "min_expectancy": 0.0,
    "require_positive_net_profit": True,
}


def evaluate(requirements=None):
    req = {**DEFAULT_REQUIREMENTS, **(requirements or {})}
    acct = PaperAccount.primary()
    qs = Trade.objects.filter(
        mode="paper",
        metadata__strategy_version=STRATEGY_VERSION,
    )
    trades = list(qs.filter(status="closed").order_by("closed_at"))
    first_trade = qs.order_by("opened_at").first()
    if first_trade:
        start_date = first_trade.opened_at.date()
        days = (timezone.localdate() - start_date).days + 1
    else:
        days = 0

    profits = [t.pnl for t in trades if t.pnl > 0]
    losses = [abs(t.pnl) for t in trades if t.pnl < 0]
    gross_profit = sum(profits)
    gross_loss = sum(losses)
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    net = sum(t.pnl for t in trades)
    expectancy = net / len(trades) if trades else 0.0

    reasons = []
    if days < req["min_days"]:
        reasons.append(f"Paper period {days} days < {req['min_days']}")
    if len(trades) < req["min_closed_trades"]:
        reasons.append(f"Closed trades {len(trades)} < {req['min_closed_trades']}")
    if pf < req["min_profit_factor"]:
        reasons.append(f"Profit factor {pf:.2f} < {req['min_profit_factor']}")
    if acct.max_drawdown_pct > req["max_drawdown_pct"]:
        reasons.append(f"Max drawdown {acct.max_drawdown_pct:.2f}% > {req['max_drawdown_pct']}%")
    if expectancy <= req["min_expectancy"]:
        reasons.append(f"Expectancy {expectancy:.2f} is not positive")
    if req["require_positive_net_profit"] and net <= 0:
        reasons.append("Net paper profit is not positive")

    gate = LiveGate.objects.create(
        eligible=not reasons,
        paper_days=max(days, 0),
        closed_trades=len(trades),
        net_profit=net,
        profit_factor=pf,
        expectancy=expectancy,
        max_drawdown_pct=acct.max_drawdown_pct,
        reasons=reasons,
        requirements={**req, "strategy_version": STRATEGY_VERSION},
    )
    return gate
