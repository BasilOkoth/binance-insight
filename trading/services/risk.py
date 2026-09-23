from __future__ import annotations
from dataclasses import dataclass

@dataclass
class PositionSize:
    quantity: float
    notional: float
    risk_amount: float
    allowed: bool
    reason: str=""

def position_size(equity: float, cash: float, entry: float, stop: float, risk_pct: float=.5, max_asset_pct: float=15.0, max_total_cash_pct: float=95.0) -> PositionSize:
    if equity<=0 or cash<=0 or entry<=0 or stop<=0 or stop>=entry:
        return PositionSize(0,0,0,False,"Invalid account or price inputs")
    risk_amount=equity*(risk_pct/100)
    risk_per_unit=entry-stop
    qty_by_risk=risk_amount/risk_per_unit
    max_notional=min(equity*(max_asset_pct/100), cash*(max_total_cash_pct/100))
    qty_by_exposure=max_notional/entry
    qty=max(0,min(qty_by_risk,qty_by_exposure))
    notional=qty*entry
    if qty<=0 or notional<5:
        return PositionSize(0,0,risk_amount,False,"Position is below practical minimum")
    return PositionSize(qty,notional,risk_amount,True,"")

def daily_loss_limit_breached(realized_today: float, start_equity: float, max_daily_loss_pct: float) -> bool:
    return realized_today <= -(start_equity*max_daily_loss_pct/100)
