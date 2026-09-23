from __future__ import annotations
import math
import numpy as np
from django.utils import timezone
from trading.models import AppConfig, BacktestRun
from .binance_client import BinanceClient
from .indicators import candles_to_df, enrich
from .scoring import score_latest

def _max_drawdown(equity_curve):
    peak=-1e99; maxdd=0
    for x in equity_curve:
        peak=max(peak,x)
        if peak>0: maxdd=max(maxdd,(peak-x)/peak*100)
    return maxdd

def run_backtest(symbol="BTCUSDT", interval=None, days=180, initial=10000.0):
    cfg=AppConfig.current(); interval=interval or cfg.scan_interval; client=BinanceClient(mode="paper")
    df=enrich(candles_to_df(client.historical_klines(symbol,interval,days=days)))
    # Use local BTC regime proxy when backtesting BTC; otherwise conservative fixed regime.
    cash=initial; equity_curve=[cash]; trades=[]; pos=None; fee=cfg.fee_bps/10000; slip=cfg.slippage_bps/10000
    for i in range(210,len(df)-1):
        r=df.iloc[i]; nxt=df.iloc[i+1]; qv=max(float(r.quote_volume)*96,1_000_000); spread_bps=5.0; regime=75.0 if r.close>r.ema200 and r.ema50>r.ema200 else 35.0
        scored=score_latest(r,qv,spread_bps,regime,cfg.stop_atr_multiple,cfg.target_r_multiple)
        if pos is None and scored.score>=cfg.signal_threshold and regime>=55:
            entry=float(nxt.open)*(1+slip); risk=max(entry-scored.stop_price,entry*.002); risk_amt=cash*cfg.risk_per_trade_pct/100; qty=min(risk_amt/risk,(cash*.15)/entry)
            if qty*entry>=5:
                entry_fee=qty*entry*fee; cash-=entry_fee; pos={"entry":entry,"qty":qty,"stop":scored.stop_price,"target":scored.target_price,"entry_fee":entry_fee,"open_i":i}
        if pos is not None:
            exit_px=None; reason=None
            if float(nxt.low)<=pos["stop"]: exit_px=pos["stop"]*(1-slip); reason="stop"
            elif float(nxt.high)>=pos["target"]: exit_px=pos["target"]*(1-slip); reason="target"
            elif i-pos["open_i"]>=96: exit_px=float(nxt.close)*(1-slip); reason="time"
            if exit_px is not None:
                exit_fee=pos["qty"]*exit_px*fee; pnl=(exit_px-pos["entry"])*pos["qty"]-pos["entry_fee"]-exit_fee; cash+=pnl; trades.append({"pnl":pnl,"ret":pnl/(pos["entry"]*pos["qty"])*100,"reason":reason}); pos=None
        equity_curve.append(cash)
    wins=[t for t in trades if t["pnl"]>0]; losses=[t for t in trades if t["pnl"]<0]; gp=sum(t["pnl"] for t in wins); gl=abs(sum(t["pnl"] for t in losses)); pf=gp/gl if gl else (999 if gp else 0)
    rets=[t["ret"] for t in trades]; expectancy=(sum(rets)/len(rets) if rets else 0); sharpe=(np.mean(rets)/np.std(rets)*math.sqrt(len(rets)) if len(rets)>1 and np.std(rets)>0 else 0)
    run=BacktestRun.objects.create(symbol=symbol,timeframe=interval,start_date=df.open_time.iloc[0].to_pydatetime(),end_date=df.open_time.iloc[-1].to_pydatetime(),trades=len(trades),win_rate_pct=(len(wins)/len(trades)*100 if trades else 0),net_return_pct=(cash/initial-1)*100,profit_factor=pf,max_drawdown_pct=_max_drawdown(equity_curve),expectancy_pct=expectancy,sharpe=sharpe,results={"trades":trades[-100:],"initial":initial,"final":cash})
    return run
