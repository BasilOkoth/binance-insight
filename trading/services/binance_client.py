from __future__ import annotations
import base64, hashlib, hmac, time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode
import requests
from django.conf import settings

class BinanceAPIError(RuntimeError):
    pass

class BinanceClient:
    def __init__(self, mode: str | None = None, timeout: int = 12):
        self.mode = (mode or settings.BINANCE_MODE).lower()
        self.timeout = timeout
        self.api_key = settings.BINANCE_API_KEY
        self.api_secret = settings.BINANCE_API_SECRET
        self.key_type = settings.BINANCE_KEY_TYPE
        self.private_key_path = settings.BINANCE_PRIVATE_KEY_PATH
        if self.mode == "testnet":
            self.trade_base = settings.BINANCE_TESTNET_BASE_URL.rstrip("/")
        else:
            self.trade_base = settings.BINANCE_LIVE_BASE_URL.rstrip("/")
        self.public_base = settings.BINANCE_PUBLIC_BASE_URL.rstrip("/")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"X-MBX-APIKEY": self.api_key})

    def _handle(self, response: requests.Response):
        try:
            data = response.json()
        except Exception:
            data = {"message": response.text[:500]}
        if not response.ok:
            raise BinanceAPIError(f"Binance HTTP {response.status_code}: {data}")
        return data

    def public_get(self, path: str, params: dict | None = None):
        r = self.session.get(self.public_base + path, params=params or {}, timeout=self.timeout)
        return self._handle(r)

    def signed_request(self, method: str, path: str, params: dict | None = None):
        if not self.api_key:
            raise BinanceAPIError("BINANCE_API_KEY is required for authenticated trading")
        payload = dict(params or {})
        payload.setdefault("timestamp", int(time.time() * 1000))
        payload.setdefault("recvWindow", 5000)
        query = urlencode(payload, doseq=True)
        if self.key_type == "ed25519":
            if not self.private_key_path:
                raise BinanceAPIError("BINANCE_PRIVATE_KEY_PATH is required for Ed25519 signing")
            try:
                from cryptography.hazmat.primitives import serialization
                key_bytes = open(self.private_key_path, "rb").read()
                private_key = serialization.load_pem_private_key(key_bytes, password=None)
                signature = base64.b64encode(private_key.sign(query.encode())).decode()
            except Exception as e:
                raise BinanceAPIError(f"Ed25519 signing failed: {e}") from e
        else:
            if not self.api_secret:
                raise BinanceAPIError("BINANCE_API_SECRET is required for HMAC signing")
            signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        payload["signature"] = signature
        url = self.trade_base + path
        r = self.session.request(method.upper(), url, params=payload, timeout=self.timeout)
        return self._handle(r)

    def exchange_info(self):
        return self.public_get("/api/v3/exchangeInfo")

    def ticker_24h(self):
        return self.public_get("/api/v3/ticker/24hr")

    def book_ticker(self, symbol: str):
        return self.public_get("/api/v3/ticker/bookTicker", {"symbol": symbol})

    def price(self, symbol: str) -> float:
        return float(self.public_get("/api/v3/ticker/price", {"symbol": symbol})["price"])

    def klines(self, symbol: str, interval: str = "15m", limit: int = 500, start_time: int | None = None, end_time: int | None = None):
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
        if start_time is not None: params["startTime"] = start_time
        if end_time is not None: params["endTime"] = end_time
        return self.public_get("/api/v3/klines", params)

    def historical_klines(self, symbol: str, interval: str = "15m", days: int = 180, max_candles: int = 20000):
        interval_ms = self.interval_ms(interval)
        end_ms = int(time.time() * 1000)
        cursor = end_ms - int(days * 86400 * 1000)
        out = []
        while cursor < end_ms and len(out) < max_candles:
            batch = self.klines(symbol, interval, limit=1000, start_time=cursor, end_time=end_ms)
            if not batch:
                break
            out.extend(batch)
            nxt = int(batch[-1][0]) + interval_ms
            if nxt <= cursor:
                break
            cursor = nxt
            if len(batch) < 1000:
                break
        # de-duplicate by open time in case the exchange returns an overlapping boundary candle
        unique = {int(r[0]): r for r in out}
        return [unique[k] for k in sorted(unique)][:max_candles]

    @staticmethod
    def interval_ms(interval: str) -> int:
        unit = interval[-1]; value = int(interval[:-1])
        factors = {"s":1000,"m":60_000,"h":3_600_000,"d":86_400_000,"w":604_800_000}
        if unit == "M":
            return value * 30 * 86_400_000
        if unit not in factors:
            raise ValueError(f"Unsupported interval {interval}")
        return value * factors[unit]

    def account(self):
        return self.signed_request("GET", "/api/v3/account")

    def market_buy_quote(self, symbol: str, quote_amount: float):
        return self.signed_request("POST", "/api/v3/order", {
            "symbol": symbol, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": f"{quote_amount:.8f}", "newOrderRespType": "FULL",
        })

    def market_sell_qty(self, symbol: str, quantity: str):
        return self.signed_request("POST", "/api/v3/order", {
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "quantity": quantity, "newOrderRespType": "FULL",
        })

    def query_order(self, symbol: str, order_id: str | int):
        return self.signed_request("GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id})

    def query_order_list(self, order_list_id: str | int):
        return self.signed_request("GET", "/api/v3/orderList", {"orderListId": order_list_id})

    def place_sell_oco(self, symbol: str, quantity: str, target_price: str, stop_price: str):
        return self.signed_request("POST", "/api/v3/orderList/oco", {
            "symbol": symbol, "side": "SELL", "quantity": quantity,
            "aboveType": "LIMIT_MAKER", "abovePrice": target_price,
            "belowType": "STOP_LOSS", "belowStopPrice": stop_price,
            "newOrderRespType": "RESULT",
        })

    @staticmethod
    def symbol_rules(exchange_info: dict, symbol: str) -> dict:
        row = next((s for s in exchange_info.get("symbols", []) if s.get("symbol") == symbol), None)
        if not row: raise BinanceAPIError(f"Unknown symbol {symbol}")
        filters = {f["filterType"]: f for f in row.get("filters", [])}
        return {"symbol": row, "filters": filters}

    @staticmethod
    def floor_to_step(value: float, step: str) -> str:
        v, s = Decimal(str(value)), Decimal(str(step))
        if s == 0: return format(v, 'f')
        units = (v / s).to_integral_value(rounding=ROUND_DOWN)
        out = units * s
        return format(out.normalize(), 'f')

    @staticmethod
    def floor_price(value: float, tick: str) -> str:
        return BinanceClient.floor_to_step(value, tick)
