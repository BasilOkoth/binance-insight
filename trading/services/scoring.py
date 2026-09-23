from __future__ import annotations
import math
from dataclasses import dataclass
from .indicators import linear, inverse, clamp

WEIGHTS = {"trend":.25,"momentum":.20,"volume":.15,"breakout":.15,"volatility":.10,"liquidity":.10,"regime":.05}

@dataclass
class ScoreResult:
    score: float
    factors: dict
    stop_price: float
    target_price: float
    rationale: list[str]
    warnings: list[str]

def score_latest(row, quote_volume_24h: float, spread_bps: float, regime_score: float, stop_atr: float=1.5, target_r: float=2.2) -> ScoreResult:
    p=float(row.close); atr=max(float(row.atr14 or 0), p*0.002)
    trend_parts=[100 if p>row.ema20 else 20,100 if row.ema20>row.ema50 else 25,100 if row.ema50>row.ema200 else 25,linear(row.ema50_slope,-1.5,1.5)]
    trend=sum(trend_parts)/len(trend_parts)
    rsi=float(row.rsi14); rsi_score = 100 if 52<=rsi<=68 else (linear(rsi,40,52) if rsi<52 else inverse(rsi,68,82))
    momentum=(rsi_score + linear(row.roc12,-4,6))/2
    volume=linear(row.volume_ratio,0.6,2.2)
    prev_high=float(row.high20_prev) if not math.isnan(float(row.high20_prev)) else p
    distance=(p/prev_high-1)*100 if prev_high else 0
    if -1.0 <= distance <= 1.8: breakout=85+max(0,distance)*5
    elif distance < -1.0: breakout=linear(distance,-6,-1)
    else: breakout=inverse(distance,1.8,7.0)
    atr_pct=atr/p*100
    volatility = 100 if 0.7 <= atr_pct <= 3.5 else (linear(atr_pct,0.2,0.7) if atr_pct<0.7 else inverse(atr_pct,3.5,8))
    liq_volume=linear(math.log10(max(quote_volume_24h,1)),5.5,9.0)
    liq_spread=inverse(spread_bps,2,35)
    liquidity=(liq_volume+liq_spread)/2
    factors={"trend":clamp(trend),"momentum":clamp(momentum),"volume":clamp(volume),"breakout":clamp(breakout),"volatility":clamp(volatility),"liquidity":clamp(liquidity),"regime":clamp(regime_score)}
    total=sum(factors[k]*w for k,w in WEIGHTS.items())
    stop=p-stop_atr*atr
    risk=max(p-stop,p*0.001)
    target=p+target_r*risk
    rationale=[]; warnings=[]
    if factors["trend"]>=70: rationale.append("Trend structure is constructive across short, medium and long moving averages")
    if factors["volume"]>=70: rationale.append("Current participation is stronger than the recent volume baseline")
    if factors["breakout"]>=70: rationale.append("Price is near or through a recent 20-candle resistance area without extreme extension")
    if 52<=rsi<=68: rationale.append("Momentum is positive without being deeply overbought")
    if spread_bps>20: warnings.append("Bid/ask spread is wide")
    if atr_pct>5: warnings.append("Volatility is elevated")
    if rsi>75: warnings.append("Momentum is stretched")
    if regime_score<50: warnings.append("BTC market regime is not supportive")
    if not rationale: rationale.append("No single factor is dominant; this is a mixed setup")
    return ScoreResult(round(clamp(total),1), {k:round(v,1) for k,v in factors.items()}, round(stop,8), round(target,8), rationale, warnings)
