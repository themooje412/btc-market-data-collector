"""Minimal Bybit V5 demo-trading client.

This module is intentionally demo-only. It refuses non-demo base URLs and keeps
all authentication material in environment variables supplied by the runner.
No API key or secret is ever persisted to the repository.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEMO_BASE_URL = "https://api-demo.bybit.com"
RECV_WINDOW = "5000"


class BybitDemoError(RuntimeError):
    pass


class BybitDemoClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = DEMO_BASE_URL, timeout: int = 15):
        if base_url.rstrip("/") != DEMO_BASE_URL:
            raise ValueError("This client is demo-only and only permits https://api-demo.bybit.com")
        if not api_key or not api_secret:
            raise ValueError("Bybit demo API key and secret are required")
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.base_url = DEMO_BASE_URL
        self.timeout = timeout

    def _signature(self, timestamp: str, payload: str) -> str:
        raw = f"{timestamp}{self.api_key}{RECV_WINDOW}{payload}".encode("utf-8")
        return hmac.new(self.api_secret, raw, hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        params = params or {}
        method = method.upper()
        timestamp = str(int(time.time() * 1000))
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "Content-Type": "application/json",
        }

        if method == "GET":
            payload = urlencode(sorted((str(k), str(v)) for k, v in params.items() if v is not None))
            headers["X-BAPI-SIGN"] = self._signature(timestamp, payload)
            url = f"{self.base_url}{path}" + (f"?{payload}" if payload else "")
            body = None
        elif method == "POST":
            payload = json.dumps(params, separators=(",", ":"), sort_keys=True)
            headers["X-BAPI-SIGN"] = self._signature(timestamp, payload)
            url = f"{self.base_url}{path}"
            body = payload.encode("utf-8")
        else:
            raise ValueError(f"Unsupported method: {method}")

        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - network path
            raise BybitDemoError(f"Bybit demo request failed: {method} {path}: {exc}") from exc

        if int(data.get("retCode", -1)) != 0:
            raise BybitDemoError(f"Bybit demo error {data.get('retCode')}: {data.get('retMsg')}")
        return data

    def get_wallet_balance(self) -> dict:
        return self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})

    def get_total_equity(self) -> float:
        data = self.get_wallet_balance()
        rows = ((data.get("result") or {}).get("list") or [])
        if not rows:
            raise BybitDemoError("Bybit demo wallet returned no UNIFIED account row")
        value = rows[0].get("totalEquity")
        try:
            equity = float(value)
        except (TypeError, ValueError) as exc:
            raise BybitDemoError(f"Invalid totalEquity from Bybit demo: {value!r}") from exc
        if equity <= 0:
            raise BybitDemoError("Bybit demo totalEquity must be positive")
        return equity

    def get_instrument(self, symbol: str) -> dict:
        data = self._request("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
        rows = ((data.get("result") or {}).get("list") or [])
        if not rows:
            raise BybitDemoError(f"No Bybit linear instrument info for {symbol}")
        return rows[0]

    def get_position(self, symbol: str) -> dict | None:
        data = self._request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
        for row in ((data.get("result") or {}).get("list") or []):
            try:
                size = float(row.get("size") or 0)
            except (TypeError, ValueError):
                size = 0.0
            if size > 0:
                return row
        return None

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        order_link_id: str,
        stop_loss: str | None = None,
        reduce_only: bool = False,
    ) -> dict:
        payload = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty,
            "timeInForce": "IOC",
            "positionIdx": 0,
            "orderLinkId": order_link_id,
            "reduceOnly": reduce_only,
        }
        if stop_loss is not None and not reduce_only:
            payload.update({
                "stopLoss": stop_loss,
                "slTriggerBy": "MarkPrice",
                "tpslMode": "Full",
            })
        return self._request("POST", "/v5/order/create", payload)

    def set_trading_stop(self, symbol: str, stop_loss: str) -> dict:
        return self._request("POST", "/v5/position/trading-stop", {
            "category": "linear",
            "symbol": symbol,
            "tpslMode": "Full",
            "positionIdx": 0,
            "stopLoss": stop_loss,
            "slTriggerBy": "MarkPrice",
        })


def quantize_down(value: float, step: str) -> str:
    """Round a positive quantity down to the exchange step size."""
    step_dec = Decimal(str(step))
    value_dec = Decimal(str(value))
    if step_dec <= 0:
        raise ValueError("step must be positive")
    units = (value_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
    result = units * step_dec
    places = max(0, -step_dec.as_tuple().exponent)
    return f"{result:.{places}f}"


def instrument_qty_rules(instrument: dict) -> tuple[str, float]:
    lot = instrument.get("lotSizeFilter") or {}
    step = str(lot.get("qtyStep") or "0")
    minimum = float(lot.get("minOrderQty") or 0.0)
    if Decimal(step) <= 0 or minimum <= 0:
        raise BybitDemoError("Instrument is missing qtyStep/minOrderQty")
    return step, minimum
