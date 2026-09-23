from __future__ import annotations
from .binance_client import BinanceClient
from .indicators import candles_to_df, enrich
from .scoring import score_latest, is_actionable_setup, regime_score_from_values, confirmed_breakout
from trading.constants import CORE_BASES, STRATEGY_VERSION
from trading.models import AppConfig, MarketSignal, AuditEvent

EXCLUDED_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "TRY", "BRL", "BIDR", "AEUR"}
EXCLUDED_MARKERS = ("UP", "DOWN", "BULL", "BEAR")


def market_regime(client: BinanceClient, interval="15m") -> float:
    df = enrich(candles_to_df(client.klines("BTCUSDT", interval, 250)))
    r = df.iloc[-2]  # last completed candle only
    return regime_score_from_values(r.close, r.ema20, r.ema50, r.ema200, r.rsi14)


def universe(client: BinanceClient, quote="USDT", size=30):
    tickers = client.ticker_24h()
    exchange_info = client.exchange_info()
    eligible = set()
    for s in exchange_info.get("symbols", []):
        if s.get("quoteAsset") != quote or s.get("status") != "TRADING":
            continue
        if s.get("isSpotTradingAllowed") is False:
            continue
        base = s.get("baseAsset", "")
        if base not in CORE_BASES:
            continue
        eligible.add(s.get("symbol"))

    rows = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if symbol not in eligible:
            continue
        base = symbol[: -len(quote)]
        if base in EXCLUDED_BASES or any(base.endswith(m) for m in EXCLUDED_MARKERS):
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
            last = float(t.get("lastPrice", 0))
        except (TypeError, ValueError):
            continue
        if qv <= 0 or last <= 0:
            continue
        rows.append((symbol, qv, last))

    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:size]


def scan_market(save=True):
    cfg = AppConfig.current()
    client = BinanceClient(mode="paper")
    regime = market_regime(client, cfg.scan_interval)
    results = []

    for symbol, qv, _ in universe(client, cfg.quote_asset, cfg.scan_universe_size):
        try:
            book = client.book_ticker(symbol)
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
            mid = (bid + ask) / 2
            spread_bps = ((ask - bid) / mid * 10000) if mid else 999

            df = enrich(candles_to_df(client.klines(symbol, cfg.scan_interval, 250)))
            if len(df) < 210:
                continue
            row = df.iloc[-2]
            scored = score_latest(
                row,
                qv,
                spread_bps,
                regime,
                cfg.stop_atr_multiple,
                cfg.target_r_multiple,
            )
            actionable = is_actionable_setup(row, scored, regime, spread_bps, cfg.signal_threshold)
            payload = {
                "symbol": symbol,
                "price": float(row.close),
                "score": scored.score,
                "stop": scored.stop_price,
                "target": scored.target_price,
                "factors": scored.factors,
                "rationale": scored.rationale,
                "warnings": scored.warnings,
                "actionable": actionable,
                "rsi": float(row.rsi14),
                "atr": float(row.atr14),
                "volume_ratio": float(row.volume_ratio or 0),
                "spread_bps": spread_bps,
                "regime": regime,
                "quote_volume_24h": qv,
                "confirmed_breakout": confirmed_breakout(row),
                "candle_open_time": row.open_time.isoformat(),
                "candle_close_time": row.close_time.isoformat(),
            }
            results.append(payload)

            if save:
                MarketSignal.objects.create(
                    symbol=symbol,
                    timeframe=cfg.scan_interval,
                    price=payload["price"],
                    score=scored.score,
                    trend_score=scored.factors["trend"],
                    momentum_score=scored.factors["momentum"],
                    volume_score=scored.factors["volume"],
                    breakout_score=scored.factors["breakout"],
                    volatility_score=scored.factors["volatility"],
                    liquidity_score=scored.factors["liquidity"],
                    regime_score=scored.factors["regime"],
                    atr=payload["atr"],
                    rsi=payload["rsi"],
                    volume_ratio=payload["volume_ratio"],
                    spread_bps=spread_bps,
                    stop_price=scored.stop_price,
                    target_price=scored.target_price,
                    rationale="; ".join(scored.rationale),
                    warnings="; ".join(scored.warnings),
                    is_actionable=actionable,
                    data={
                        "strategy_version": STRATEGY_VERSION,
                        "quote_volume_24h": qv,
                        "confirmed_breakout": payload["confirmed_breakout"],
                        "candle_open_time": payload["candle_open_time"],
                        "candle_close_time": payload["candle_close_time"],
                    },
                )
        except Exception as e:
            AuditEvent.objects.create(level="WARN", category="scanner", message=f"{symbol}: {e}")

    results.sort(key=lambda x: x["score"], reverse=True)
    AuditEvent.objects.create(
        category="scanner",
        message=f"Strategy v{STRATEGY_VERSION} scan complete: {len(results)} core symbols, BTC regime {regime:.0f}",
        data={"regime": regime, "strategy_version": STRATEGY_VERSION},
    )
    return results
