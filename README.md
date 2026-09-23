# Binance Insight Premium

A private, risk-first Binance Spot research and execution platform inspired by the architecture of NSE Insight. It scans liquid USDT pairs, scores setups transparently, backtests the same logic, runs an automated paper portfolio, and contains a deliberately locked testnet/live execution layer.

## What is included

- Premium authenticated Django dashboard
- Opportunity Radar across the most liquid USDT spot pairs
- Transparent 100-point model: trend, momentum, volume, breakout quality, volatility, liquidity and BTC regime
- ATR-based stop and risk/reward target construction
- Fees + slippage aware multi-month backtesting with paginated Binance candle history
- Automated paper trading with cash/equity/drawdown tracking
- Position sizing by account risk rather than arbitrary coin quantity
- Daily-loss, max-position, per-asset and total-exposure circuit breakers
- Paper-to-live graduation gate
- Binance Testnet and live Spot REST integration
- Exchange filter-aware quantity and price rounding
- Live market entry followed by exchange-side OCO protection
- Fail-closed behavior: if protection cannot be placed after an entry, the system attempts to flatten the position and raises a critical error
- Audit log and Render deployment blueprint

## Important architecture decision

The app does **not** treat a high signal score as a guarantee. The score is a ranking of current conditions. Real-money execution is impossible by default and remains gated even if paper statistics qualify.

Default live gate:

- at least 30 paper-trading days
- at least 100 closed paper trades
- profit factor >= 1.25
- positive expectancy
- positive net paper profit
- maximum paper drawdown <= 12%

Passing these checks does not mean a strategy will remain profitable. It only means the minimum configured validation conditions have been met.

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python manage.py migrate
python manage.py bootstrap_app
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000 and sign in.

For environment variables on Windows PowerShell you can use `$env:NAME="value"` before starting Django, or load them through your deployment provider. Django does not automatically parse `.env`; keep it as your reference/source for deployment variables unless you add an env loader.

## First proving sequence

```powershell
python manage.py scan_market
python manage.py run_backtest BTCUSDT
python manage.py run_backtest ETHUSDT
python manage.py market_worker --seconds 300
```

Leave `BINANCE_MODE=paper` and `LIVE_TRADING_ENABLED=false` while the paper engine accumulates evidence.

## Testnet stage

Only after the paper gate has genuinely passed:

1. Create Binance Spot Testnet API credentials.
2. Add Binance API credentials to server environment variables. HMAC is supported for compatibility; for production, the code also supports Ed25519 via `BINANCE_KEY_TYPE=ed25519` and `BINANCE_PRIVATE_KEY_PATH`.
3. Set `BINANCE_MODE=testnet`.
4. Intentionally set `LIVE_TRADING_ENABLED=true`.
5. Set `LIVE_TRADING_ACK=I_ACCEPT_LIVE_TRADING_RISK`.
6. Run `python manage.py run_live_once` for a single controlled test order.
7. Run `python manage.py live_worker --seconds 60` to reconcile protective exits. Add `--auto-entry` only when you intentionally want the unlocked worker to open new qualifying positions.

The same execution code is used for testnet and live except for the API base URL. That reduces the risk of validating one code path and trading with another.

## Live stage

After successful testnet operation, changing `BINANCE_MODE=live` points authenticated orders at the production Spot endpoint. Do not grant the API key withdrawal permission. Restrict the API key to trading only and, where practical, restrict it to your server IP.

The app purposely has no withdrawal code. Binance currently recommends asymmetric API keys such as Ed25519 over HMAC; the integration supports both so you can test with HMAC and migrate to Ed25519 before meaningful live capital.

## Render

`render.yaml` defines:

- a Django web service
- a background scanner/paper worker
- PostgreSQL

Deploy the Blueprint, create your superuser through a Render shell, and keep all Binance credentials in Render environment secrets rather than GitHub.

## Worker behavior

`market_worker` currently scans and runs paper trading every 5 minutes. It does not automatically place live orders. That separation is intentional. Live execution is a separate command so a configuration mistake cannot silently turn your research worker into a real-money bot.

## Strategy model

The 100-point score uses:

| Factor | Weight |
|---|---:|
| Trend | 25% |
| Momentum | 20% |
| Volume | 15% |
| Breakout quality | 15% |
| Volatility | 10% |
| Liquidity/spread | 10% |
| BTC market regime | 5% |

The default actionable threshold is 72/100. All thresholds are editable in the Risk & Strategy Settings page.

## Production hardening before meaningful capital

Before increasing live capital, add monitoring for exchange/user-data events, alerting for rejected/cancelled protective orders, database backups, server uptime monitoring, and periodic reconciliation of local trades against Binance account/order history. Keep initial live size materially smaller than the paper account size until real fills, latency, fees and operational behavior are understood.


### Backtest note
Historical Binance klines do not contain historical bid/ask spreads. The backtester therefore uses a conservative spread proxy plus the configured slippage and fees. Treat backtests as screening evidence, not a reproduction of future fills.
