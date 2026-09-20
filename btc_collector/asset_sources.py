"""Generic public market-data sources for ETH, SOL and ZEC.

The BTC production collector intentionally remains untouched.  This module
reuses the same source semantics with per-asset symbols and explicit
not-applicable results when Deribit does not list a matching market.
"""

import logging
import time
from .core import WINDOWS, basis, check_time, epoch, fresh, iso, metric, missing, number
from .sources import BINANCE, COINBASE, DERIBIT, FUTURES


def safe(fn, fallback):
    try:
        return fn()
    except Exception as exc:
        logging.warning("%s: %s", getattr(fn, "__name__", "source"), exc)
        return fallback(str(exc))


def binance_spot(client, symbol, asset):
    source = f"{BINANCE}/api/v3/ticker/24hr?symbol={symbol}"
    data, _, received = client.get(BINANCE, "/api/v3/ticker/24hr", symbol=symbol)
    if data.get("symbol") != symbol:
        raise ValueError("Wrong spot symbol")
    return fresh(
        number(data["lastPrice"], 1e-12), source,
        number(data["closeTime"]) / 1000, received, "USDT",
        timestamp_kind="exchange_rolling_ticker_close", asset=asset,
    )


def coinbase_spot(client, product, asset):
    source = f"{COINBASE}/products/{product}/ticker"
    data, _, received = client.get(COINBASE, f"/products/{product}/ticker")
    return fresh(
        number(data["price"], 1e-12), source, epoch(data["time"]), received,
        "USD", timestamp_kind="last_trade", asset=asset,
    )


def _generic_binance_cvd(klines, end, symbol, asset):
    candles = {}
    for row in klines:
        minute = int(row[0]) // 1000
        if minute % 60 or int(row[6]) != (minute + 60) * 1000 - 1:
            raise ValueError("Unexpected kline timestamp resolution or duration")
        total, buy = number(row[5], 0), number(row[9], 0)
        quote, buy_quote = number(row[7], 0), number(row[10], 0)
        if buy > total + 1e-8 or buy_quote > quote + 1e-5:
            raise ValueError("Taker buy volume exceeds total volume")
        value = (2 * buy - total, 2 * buy_quote - quote)
        if minute in candles and candles[minute] != value:
            raise ValueError("Conflicting duplicate kline")
        candles[minute] = value

    source = f"Binance {symbol} /api/v3/klines (taker volumes)"
    out = {}
    for name, mins in WINDOWS.items():
        keys = range(end - mins * 60, end, 60)
        present = sum(t in candles for t in keys)
        if present != mins:
            out[name] = missing(
                source, "Incomplete closed-minute coverage", unit=asset,
                coverage_minutes=present, expected_minutes=mins,
            )
        else:
            out[name] = metric(
                sum(candles[t][0] for t in keys), source, end, asset,
                quote_cvd=metric(sum(candles[t][1] for t in keys), source, end, "USDT"),
                window_start=iso(end - mins * 60), window_end=iso(end),
                coverage_minutes=mins, expected_minutes=mins,
            )
    return out


def get_binance_cvd(client, end, symbol, asset):
    rows = []
    start = end - 86400
    while start < end:
        data, _, _ = client.get(
            BINANCE, "/api/v3/klines", symbol=symbol, interval="1m",
            startTime=start * 1000, endTime=end * 1000 - 1, limit=1000,
        )
        if not data:
            break
        rows.extend(data)
        following = int(data[-1][0]) // 1000 + 60
        if following <= start:
            raise ValueError("Kline pagination did not advance")
        start = following
    return _generic_binance_cvd(rows, end, symbol, asset)


def _generic_coinbase_cvd(buckets, end, product, asset, request_ok=True, reason=None):
    out = {}
    source = f"Coinbase {product} /products/{product}/trades"
    for name, mins in WINDOWS.items():
        keys = [str(t) for t in range(end - mins * 60, end, 60)]
        count = sum(k in buckets for k in keys)
        if not request_ok or count != mins:
            out[name] = missing(
                source, reason or "Incomplete trade coverage; warming up or pagination limit",
                unit=asset, coverage_minutes=count, expected_minutes=mins,
            )
        else:
            out[name] = metric(
                sum(buckets[k][0] for k in keys), source, end, asset,
                quote_cvd=metric(sum(buckets[k][1] for k in keys), source, end, "USD"),
                window_start=iso(end - mins * 60), window_end=iso(end),
                coverage_minutes=mins, expected_minutes=mins,
            )
    return out


def get_coinbase_cvd(client, end, state, product, asset, max_pages=600, budget=480):
    old = state.get("coinbase_minutes", {})
    buckets = {k: v for k, v in old.items() if end - 26 * 3600 <= int(k) < end}
    contiguous = end - 86400
    if buckets:
        latest = max(map(int, buckets)) + 60
        if latest <= end:
            contiguous = max(end - 86400, latest - 120)

    trades = {}
    after = None
    seen_cursors = set()
    oldest = newest = None
    started = time.monotonic()
    failure = None
    pages = 0

    for _ in range(max_pages):
        if time.monotonic() - started > budget:
            break
        try:
            params = {"limit": 1000}
            if after is not None:
                params["after"] = after
            data, headers, received = client.get(COINBASE, f"/products/{product}/trades", **params)
            pages += 1
            if not isinstance(data, list) or not data:
                raise ValueError("Empty/malformed trade page")
            parsed = []
            for row in data:
                trade_id = int(row["trade_id"])
                timestamp = epoch(row["time"])
                size = number(row["size"], 0)
                price = number(row["price"], 1e-12)
                if row["side"] not in ("buy", "sell"):
                    raise ValueError("Unknown maker side")
                parsed.append((trade_id, timestamp, size, price, row["side"]))
            page_old = min(r[1] for r in parsed)
            page_new = max(r[1] for r in parsed)
            if newest is None:
                newest = page_new
                check_time(newest, received, 180)
                if newest < end:
                    raise ValueError("Latest trades do not reach requested window end")
            if oldest is not None and page_old >= oldest:
                raise ValueError("Trade pagination did not move backward")
            oldest = page_old
            for parsed_row in parsed:
                if parsed_row[0] in trades and trades[parsed_row[0]] != parsed_row:
                    raise ValueError("Conflicting duplicate trade")
                trades[parsed_row[0]] = parsed_row
            if oldest <= contiguous:
                break
            after = headers.get("cb-after")
            if not after or after in seen_cursors:
                raise ValueError("Missing/repeated Coinbase cb-after cursor")
            seen_cursors.add(after)
        except Exception as exc:
            failure = str(exc)
            break

    if failure:
        return (
            _generic_coinbase_cvd(buckets, end, product, asset, False, failure),
            {"coinbase_minutes": buckets},
            {"pages": pages, "error": failure},
        )
    if oldest is None:
        return (
            _generic_coinbase_cvd(buckets, end, product, asset, False, "No trades fetched"),
            {"coinbase_minutes": buckets}, {"pages": pages},
        )

    first_full = (int(oldest) // 60 + 1) * 60
    fresh_bins = {str(t): [0.0, 0.0] for t in range(max(first_full, end - 86400), end, 60)}
    for _, timestamp, size, price, side in trades.values():
        key = str(int(timestamp) // 60 * 60)
        if key in fresh_bins:
            sign = 1 if side == "sell" else -1
            fresh_bins[key][0] += sign * size
            fresh_bins[key][1] += sign * size * price
    buckets.update(fresh_bins)
    result = _generic_coinbase_cvd(buckets, end, product, asset)
    return result, {"coinbase_minutes": buckets}, {
        "pages": pages, "unique_trades": len(trades),
        "oldest_trade_timestamp": oldest, "newest_trade_timestamp": newest,
        "max_pages": max_pages,
    }


def binance_futures(client, symbol, asset):
    result = {}

    def mark():
        data, _, received = client.get(FUTURES, "/fapi/v1/premiumIndex", symbol=symbol)
        if data.get("symbol") != symbol:
            raise ValueError("Wrong perpetual symbol")
        timestamp = number(data["time"]) / 1000
        result["mark_price"] = fresh(number(data["markPrice"], 1e-12), FUTURES + "/fapi/v1/premiumIndex", timestamp, received, "USDT")
        result["index_price"] = fresh(number(data["indexPrice"], 1e-12), FUTURES + "/fapi/v1/premiumIndex", timestamp, received, "USDT")
        result["funding"] = fresh(
            number(data["lastFundingRate"], -1, 1), FUTURES + "/fapi/v1/premiumIndex",
            timestamp, received, "fraction", kind="latest_reported_rate",
            next_funding_timestamp=iso(number(data["nextFundingTime"]) / 1000),
        )
        if result["mark_price"]["status"] == "ok" and result["index_price"]["status"] == "ok":
            result["basis"] = basis(data["markPrice"], data["indexPrice"], FUTURES + "/fapi/v1/premiumIndex", timestamp)

    safe(mark, lambda e: result.update({k: missing(FUTURES, e) for k in ("mark_price", "index_price", "funding")}))

    def oi():
        data, _, received = client.get(FUTURES, "/fapi/v1/openInterest", symbol=symbol)
        if data.get("symbol") != symbol:
            raise ValueError("Wrong OI symbol")
        result["oi"] = fresh(number(data["openInterest"], 0), FUTURES + "/fapi/v1/openInterest", number(data["time"]) / 1000, received, asset)

    safe(oi, lambda e: result.update(oi=missing(FUTURES, e, unit=asset)))
    points = []

    def history():
        rows, _, received = client.get(FUTURES, "/futures/data/openInterestHist", symbol=symbol, period="5m", limit=310)
        for row in rows:
            timestamp = number(row["timestamp"]) / 1000
            if timestamp > received + 30:
                raise ValueError("Future OI history timestamp")
            points.append((timestamp, number(row["sumOpenInterest"], 0)))

    safe(history, lambda e: result.update(history_error=e))
    result["historical_points"] = points
    result.setdefault("basis", {k: missing(FUTURES, "Fresh mark/index unavailable") for k in ("absolute", "bps", "annualized_pct")})
    return result


def _deribit_future_instruments(client, asset):
    data, _, received = client.get(DERIBIT, "/public/get_instruments", currency="any", kind="future", expired="false")
    rows = [
        row for row in data.get("result", [])
        if row.get("is_active") and row.get("base_currency") == asset
        and row.get("expiration_timestamp", 0) / 1000 > received
    ]
    return rows, received


def _not_applicable_future(asset, reason):
    src = DERIBIT
    out = {k: missing(src, reason, status="not_applicable") for k in ("oi", "mark_price", "index_price", "funding")}
    out["basis"] = {k: missing(src, reason, status="not_applicable") for k in ("absolute", "bps", "annualized_pct")}
    out["instrument_name"] = None
    out["asset"] = asset
    return out


def deribit_perpetual(client, asset):
    instruments, _ = _deribit_future_instruments(client, asset)
    candidates = [
        row for row in instruments
        if row.get("settlement_period") == "perpetual" or "PERPETUAL" in row.get("instrument_name", "")
    ]
    if not candidates:
        return _not_applicable_future(asset, f"No active Deribit {asset} perpetual")

    def preference(row):
        name = row.get("instrument_name", "")
        exact = 0 if name == f"{asset}-PERPETUAL" else 1
        usdc = 0 if row.get("settlement_currency") == "USDC" else 1
        return (exact, usdc, name)

    instrument = sorted(candidates, key=preference)[0]
    name = instrument["instrument_name"]
    data, _, received = client.get(DERIBIT, "/public/ticker", instrument_name=name)
    ticker = data["result"]
    timestamp = number(ticker["timestamp"]) / 1000
    source = f"{DERIBIT}/public/ticker?instrument_name={name}"
    if ticker.get("instrument_name") != name:
        raise ValueError("Wrong Deribit perpetual")
    oi_unit = "USD" if instrument.get("instrument_type") == "reversed" else asset
    out = {
        "instrument_name": name,
        "instrument_type": instrument.get("instrument_type"),
        "settlement_currency": instrument.get("settlement_currency"),
        "mark_price": fresh(number(ticker["mark_price"], 1e-12), source, timestamp, received, "USD"),
        "index_price": fresh(number(ticker["index_price"], 1e-12), source, timestamp, received, "USD"),
        "oi": fresh(number(ticker["open_interest"], 0), source, timestamp, received, oi_unit),
        "funding": fresh(number(ticker.get("funding_8h", 0), -1, 1), source, timestamp, received, "fraction", kind="Deribit funding_8h; not interchangeable with Binance settlement rate"),
    }
    out["basis"] = basis(ticker["mark_price"], ticker["index_price"], source, timestamp) if out["mark_price"]["status"] == "ok" else {k: missing(source, "Stale mark") for k in ("absolute", "bps", "annualized_pct")}
    return out


def deribit_dated_futures(client, asset):
    instruments, received = _deribit_future_instruments(client, asset)
    dated = [
        row for row in instruments
        if row.get("settlement_period") != "perpetual" and "PERPETUAL" not in row.get("instrument_name", "")
    ]
    if not dated:
        return []
    out = []
    for instrument in sorted(dated, key=lambda row: row["expiration_timestamp"]):
        name = instrument["instrument_name"]
        try:
            data, _, recv = client.get(DERIBIT, "/public/ticker", instrument_name=name)
            ticker = data["result"]
            timestamp = number(ticker["timestamp"]) / 1000
            check_time(timestamp, recv)
            out.append({
                "instrument_name": name,
                "expiry": iso(instrument["expiration_timestamp"] / 1000),
                "basis": basis(ticker["mark_price"], ticker["index_price"], DERIBIT + "/public/ticker", timestamp, instrument["expiration_timestamp"] / 1000),
            })
        except Exception as exc:
            out.append({"instrument_name": name, "basis": missing(DERIBIT, str(exc))})
    return out
