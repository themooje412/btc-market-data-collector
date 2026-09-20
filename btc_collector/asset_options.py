"""Capability-aware Deribit option analytics for ETH, SOL and ZEC.

The BTC collector keeps its separately audited option implementation.  This
module gives the additional assets the same core semantics where Deribit has
an active option surface: complete active inventory, OI walls, ATM IV,
model-based 25D skew, gross gamma, an explicitly assumed signed dealer-GEX
estimate, repriced zero-gamma and cumulative-strike zero-gamma.

If Deribit has no active options for an asset, metrics are explicitly
``not_applicable`` rather than silently substituted from another venue.
"""

import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from .core import black_scholes_gamma, epoch, fresh, iso, metric, missing, number
from .sources import DERIBIT


def _na_metric(asset, name, unit=None):
    return missing(DERIBIT, f"No active Deribit {asset} options; {name} unavailable", status="not_applicable", unit=unit)


def unavailable_options(asset):
    keys = (
        "total_oi", "gross_gex_proxy", "put_wall", "call_wall", "gamma_concentrations",
        "net_gex_estimate_usd_per_1pct", "gex_by_strike", "zero_gamma_flip",
        "zero_gamma_flip_repriced", "zero_gamma_flip_cumulative_strike",
        "spot_to_gamma_flip_pct", "gamma_regime", "selected_flip_crossing_direction",
        "crossing_count",
    )
    out = {
        "status": "not_applicable", "source": DERIBIT,
        "scope": f"No active base_currency={asset} Deribit vanilla options",
        "contracts": [], "active_contract_count": 0, "by_expiry": {},
    }
    for key in keys:
        unit = asset if key == "total_oi" else ("USD" if "wall" in key or "flip" in key else None)
        out[key] = _na_metric(asset, key, unit)
    out["dealer_gex_estimate"] = {"status": "not_applicable", "reason": f"No active Deribit {asset} option surface"}
    out["headline_surface"] = {
        "expiry": None,
        "atm_iv": _na_metric(asset, "ATM IV"),
        "risk_reversal_25d": _na_metric(asset, "25D risk reversal"),
        "call_25d_iv": _na_metric(asset, "25D call IV"),
        "put_25d_iv": _na_metric(asset, "25D put IV"),
    }
    return out


def _zero_crossings(xs, ys):
    if len(xs) != len(ys) or len(xs) < 2:
        return []
    scale = max((abs(v) for v in ys), default=0.0)
    tolerance = max(1e-12, scale * 1e-12)
    signs = [0 if abs(v) <= tolerance else (1 if v > 0 else -1) for v in ys]
    roots = []
    for i in range(len(xs) - 1):
        a, b = signs[i], signs[i + 1]
        if a and b and a != b:
            root = xs[i] - ys[i] * (xs[i + 1] - xs[i]) / (ys[i + 1] - ys[i])
            roots.append({"price": root, "direction": "negative_to_positive" if a < b else "positive_to_negative"})
    return roots


def _primary(crossings, spot):
    return min(crossings, key=lambda row: abs(row["price"] - spot)) if crossings else None


def _normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _delta(spot, strike, sigma, tau, option_type):
    root = math.sqrt(tau)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * tau) / (sigma * root)
    call = _normal_cdf(d1)
    return call if option_type == "call" else call - 1.0


def _interpolate_wing(rows, option_type, target):
    pairs = sorted(
        (row["model_delta"], row["mark_iv"], row["instrument_name"])
        for row in rows if row["option_type"] == option_type and row.get("model_delta") is not None
    )
    for delta, iv, name in pairs:
        if abs(delta - target) < 1e-10:
            return iv, [name]
    for (d1, v1, n1), (d2, v2, n2) in zip(pairs, pairs[1:]):
        if d1 < target < d2:
            return v1 + (v2-v1) * (target-d1) / (d2-d1), [n1, n2]
    raise ValueError("25-delta is not bracketed by the active option surface")


def _surface(rows, timestamp):
    source = "Deribit bulk option summaries; Black-Scholes model delta"
    out = {}
    if not rows:
        for key in ("atm_iv", "call_25d_iv", "put_25d_iv", "risk_reversal_25d"):
            out[key] = missing(source, "Empty expiry surface")
        return out
    forward = statistics.median(row["underlying_price"] for row in rows)
    calls = {row["strike"] for row in rows if row["option_type"] == "call"}
    puts = {row["strike"] for row in rows if row["option_type"] == "put"}
    common = calls & puts
    if common:
        strike = min(common, key=lambda k: abs(math.log(k / forward)))
        pair = [row for row in rows if row["strike"] == strike]
        out["atm_iv"] = metric(
            statistics.mean(row["mark_iv"] for row in pair), source, timestamp,
            "volatility percentage points", strike=strike, forward_price=forward,
            method="Nearest-forward common strike; mean call/put mark IV",
        )
    else:
        out["atm_iv"] = missing(source, "No common call/put strike")
    try:
        value, names = _interpolate_wing(rows, "call", 0.25)
        out["call_25d_iv"] = metric(value, source, timestamp, "volatility percentage points", instruments=names)
    except ValueError as exc:
        out["call_25d_iv"] = missing(source, str(exc))
    try:
        value, names = _interpolate_wing(rows, "put", -0.25)
        out["put_25d_iv"] = metric(value, source, timestamp, "volatility percentage points", instruments=names)
    except ValueError as exc:
        out["put_25d_iv"] = missing(source, str(exc))
    call = out["call_25d_iv"]
    put = out["put_25d_iv"]
    if call["status"] == "ok" and put["status"] == "ok":
        out["risk_reversal_25d"] = metric(
            call["value"] - put["value"], source, timestamp,
            "volatility percentage points", definition="25D call IV minus 25D put IV",
        )
    else:
        out["risk_reversal_25d"] = missing(source, "Both 25D wings required")
    return out


def _book_snapshot(client, instruments, asset):
    by_name = {row["instrument_name"]: row for row in instruments}
    currencies = sorted({row.get("quote_currency") for row in instruments if row.get("quote_currency")})
    started = time.time()

    def books(currency):
        data, _, received = client.get(DERIBIT, "/public/get_book_summary_by_currency", currency=currency, kind="option")
        return currency, data.get("result", []), received

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(currencies)))) as pool:
        responses = list(pool.map(books, currencies))
    completed = time.time()
    summaries = {}
    exchange_times = []
    for currency, entries, received in responses:
        for entry in entries:
            name = entry.get("instrument_name")
            if name in by_name and entry.get("base_currency") == asset:
                if name in summaries:
                    raise ValueError("Duplicate option in Deribit bulk summaries: " + name)
                summaries[name] = (currency, entry)
                raw_time = entry.get("creation_timestamp")
                exchange_times.append(number(raw_time) / 1000 if raw_time is not None else received)
    return summaries, {
        "snapshot_started_at": iso(started),
        "snapshot_completed_at": iso(completed),
        "snapshot_span_seconds": round(completed-started, 3),
        "exchange_timestamp": min(exchange_times) if exchange_times else completed,
        "expected_instruments": len(instruments),
        "returned_instruments": len(summaries),
        "quote_currencies": currencies,
    }


def collect_options(client, asset):
    inventory, _, received = client.get(DERIBIT, "/public/get_instruments", currency="any", kind="option", expired="false")
    instruments = [
        row for row in inventory.get("result", [])
        if row.get("base_currency") == asset and row.get("is_active")
        and row.get("expiration_timestamp", 0) / 1000 > received
        and row.get("option_type") in ("call", "put")
    ]
    if not instruments:
        return unavailable_options(asset)
    if len({row["instrument_name"] for row in instruments}) != len(instruments):
        raise ValueError("Duplicate Deribit option instrument names")
    instruments.sort(key=lambda row: (row["expiration_timestamp"], row["instrument_name"]))
    summaries, metadata = _book_snapshot(client, instruments, asset)
    calc_time = metadata["exchange_timestamp"]

    rows = []
    missing_inputs = 0
    for instrument in instruments:
        name = instrument["instrument_name"]
        if name not in summaries:
            missing_inputs += 1
            continue
        currency, entry = summaries[name]
        try:
            timestamp = number(entry.get("creation_timestamp")) / 1000
            oi = number(entry.get("open_interest"), 0)
            mark_iv = number(entry.get("mark_iv"), 1e-12)
            underlying = number(entry.get("underlying_price"), 1e-12)
            strike = number(instrument["strike"], 1e-12)
            expiry = number(instrument["expiration_timestamp"]) / 1000
            tau = (expiry - calc_time) / (365.25 * 86400)
            if tau <= 0:
                raise ValueError("Expired option in active inventory")
            sigma = mark_iv / 100.0
            gamma = black_scholes_gamma(underlying, strike, sigma, tau, 0)
            option_type = instrument["option_type"]
            gross = gamma * oi * underlying * underlying * 0.01
            sign = -1 if option_type == "call" else 1
            signed = sign * gross
            rows.append({
                "instrument_name": name, "base_currency": asset,
                "quote_currency": instrument.get("quote_currency"),
                "settlement_currency": instrument.get("settlement_currency"),
                "option_type": option_type, "strike": strike, "expiry": iso(expiry),
                "open_interest": oi, "mark_iv": mark_iv, "underlying_price": underlying,
                "model_gamma": gamma, "model_delta": _delta(underlying, strike, sigma, tau, option_type),
                "gross_gex_proxy": gross, "signed_gex_estimate": signed,
                "source_currency": currency, "timestamp": iso(timestamp),
            })
        except (KeyError, ValueError, TypeError):
            missing_inputs += 1

    source = "Deribit public/get_book_summary_by_currency; Black-Scholes gamma"
    quote_time = metadata["exchange_timestamp"]
    if not rows:
        raise ValueError(f"No usable {asset} Deribit option summaries")

    complete = len(rows) == len(instruments) and missing_inputs == 0
    total_oi_value = sum(row["open_interest"] for row in rows)
    gross_value = sum(row["gross_gex_proxy"] for row in rows)
    signed_value = sum(row["signed_gex_estimate"] for row in rows)
    total_oi = metric(total_oi_value, source, quote_time, asset, covered_contracts=len(rows), expected_contracts=len(instruments)) if complete else missing(source, f"Incomplete option coverage: {len(rows)}/{len(instruments)}", unit=asset)
    gross_metric = metric(gross_value, source, quote_time, f"USD per 1% {asset} move") if complete else missing(source, "Incomplete gamma coverage")

    by_strike = {}
    for row in rows:
        bucket = by_strike.setdefault(row["strike"], {"call_oi": 0.0, "put_oi": 0.0, "gross": 0.0, "signed": 0.0})
        bucket[row["option_type"] + "_oi"] += row["open_interest"]
        bucket["gross"] += row["gross_gex_proxy"]
        bucket["signed"] += row["signed_gex_estimate"]

    def wall(side):
        key = side + "_oi"
        positive = [(strike, values[key]) for strike, values in by_strike.items() if values[key] > 0]
        if not positive:
            return missing(source, f"No positive {side} OI", status="not_applicable", unit="USD strike")
        top = max(value for _, value in positive)
        strikes = [strike for strike, value in positive if value == top]
        return metric(min(strikes), source, quote_time, "USD strike", oi_base=top, tied_strikes=strikes)

    put_wall = wall("put")
    call_wall = wall("call")
    gex_by_strike = [
        {"strike": strike, "net_gex_usd_per_1pct": values["signed"]}
        for strike, values in sorted(by_strike.items())
    ]
    concentrations = sorted(
        ({"strike": strike, "gross_gex_proxy": values["gross"], "share_pct": values["gross"] / gross_value * 100 if gross_value else 0.0}
         for strike, values in by_strike.items()),
        key=lambda row: row["gross_gex_proxy"], reverse=True,
    )

    underlying_values = [row["underlying_price"] for row in rows]
    try:
        index_data, _, index_received = client.get(DERIBIT, "/public/get_index_price", index_name=f"{asset.lower()}_usd")
        spot = number(index_data["result"]["index_price"], 1e-12)
        spot_timestamp = number(index_data.get("usOut"), 0) / 1_000_000 if index_data.get("usOut") else index_received
        index_metric = fresh(spot, DERIBIT + f"/public/get_index_price?index_name={asset.lower()}_usd", spot_timestamp, index_received, "USD", max_age=900)
    except Exception:
        spot = statistics.median(underlying_values)
        index_metric = metric(spot, source, quote_time, "USD", method="Median expiry underlying fallback; Deribit index endpoint unavailable")

    magnitude = sum(abs(values["signed"]) for values in by_strike.values())
    tolerance = max(1e-6, magnitude * 1e-9)
    regime = "long_gamma" if signed_value > tolerance else ("short_gamma" if signed_value < -tolerance else "zero_gamma")

    grid = [spot * (0.5 + i / 200.0) for i in range(201)]
    curve = []
    for candidate in grid:
        ratio = candidate / spot
        total = 0.0
        for row in rows:
            expiry = epoch(row["expiry"])
            tau = (expiry - calc_time) / (365.25 * 86400)
            ref = row["underlying_price"] * ratio
            gamma = black_scholes_gamma(ref, row["strike"], row["mark_iv"] / 100.0, tau, 0)
            sign = -1 if row["option_type"] == "call" else 1
            total += sign * gamma * row["open_interest"] * ref * ref * 0.01
        curve.append(total)
    roots = _zero_crossings(grid, curve)
    selected = _primary(roots, spot)
    if selected:
        flip = selected["price"]
        flip_metric = metric(flip, source, quote_time, "USD", crossing_count=len(roots), selected_crossing_direction=selected["direction"])
        distance = metric((spot / flip - 1) * 100, source, quote_time, "%")
        direction = metric(selected["direction"], source, quote_time)
    else:
        flip_metric = missing(source, "No repriced signed-GEX zero crossing in 50%-150% spot range", status="not_applicable", unit="USD")
        distance = missing(source, "No repriced zero crossing", status="not_applicable", unit="%")
        direction = missing(source, "No repriced zero crossing", status="not_applicable")

    running = 0.0
    strikes = sorted(by_strike)
    cumulative = []
    for strike in strikes:
        running += by_strike[strike]["signed"]
        cumulative.append(running)
    cumulative_roots = _zero_crossings(strikes, cumulative)
    cumulative_selected = _primary(cumulative_roots, spot)
    cumulative_metric = metric(
        cumulative_selected["price"], source, quote_time, "USD",
        selected_crossing_direction=cumulative_selected["direction"], crossing_count=len(cumulative_roots),
        definition="Low-to-high cumulative current signed GEX by strike",
    ) if cumulative_selected else missing(source, "No cumulative-by-strike zero crossing", status="not_applicable", unit="USD")

    by_expiry = {}
    expiry_groups = {}
    for row in rows:
        expiry_groups.setdefault(row["expiry"], []).append(row)
    for expiry, group in sorted(expiry_groups.items()):
        settlement_groups = {}
        for row in group:
            settlement_groups.setdefault(row.get("settlement_currency") or row.get("quote_currency") or "unknown", []).append(row)
        surfaces = {currency: _surface(values, quote_time) for currency, values in settlement_groups.items()}
        by_expiry[expiry] = {
            "total_oi": metric(sum(row["open_interest"] for row in group), source, quote_time, asset),
            "gross_gex_proxy": metric(sum(row["gross_gex_proxy"] for row in group), source, quote_time, f"USD per 1% {asset} move"),
            "surfaces": surfaces,
        }

    finished = time.time()
    candidates = [(expiry, entry) for expiry, entry in by_expiry.items() if 7 <= (epoch(expiry)-finished)/86400 <= 60]
    if candidates:
        expiry, entry = min(candidates, key=lambda pair: abs((epoch(pair[0])-finished)/86400 - 30))
        preferred = asset if asset in entry["surfaces"] else max(
            entry["surfaces"], key=lambda currency: sum(row["open_interest"] for row in expiry_groups[expiry] if (row.get("settlement_currency") or row.get("quote_currency") or "unknown") == currency)
        )
        headline_surface = {"expiry": expiry, "settlement_currency": preferred, **entry["surfaces"][preferred]}
    else:
        headline_surface = {
            "expiry": None,
            "atm_iv": missing(source, "No expiry in 7-60 day headline window", status="not_applicable"),
            "call_25d_iv": missing(source, "No expiry in 7-60 day headline window", status="not_applicable"),
            "put_25d_iv": missing(source, "No expiry in 7-60 day headline window", status="not_applicable"),
            "risk_reversal_25d": missing(source, "No expiry in 7-60 day headline window", status="not_applicable"),
        }

    return {
        "status": "ok" if complete else "partial", "source": DERIBIT,
        "inventory_timestamp": iso(received),
        "scope": f"All active base_currency={asset} vanilla options returned by Deribit; no venue substitution",
        "contracts": rows, "active_contract_count": len(instruments), "by_expiry": by_expiry,
        "total_oi": total_oi, "gross_gex_proxy": gross_metric,
        "put_wall": put_wall, "call_wall": call_wall,
        "gamma_concentrations": metric(concentrations, source, quote_time),
        "net_gex_estimate_usd_per_1pct": metric(signed_value, source, quote_time, "USD per 1% move") if complete else missing(source, "Incomplete signed-GEX inputs"),
        "gex_by_strike": metric(gex_by_strike, source, quote_time),
        "zero_gamma_flip": flip_metric, "zero_gamma_flip_repriced": flip_metric,
        "zero_gamma_flip_cumulative_strike": cumulative_metric,
        "spot_to_gamma_flip_pct": distance,
        "gamma_regime": metric(regime, source, quote_time, dealer_position_assumption="dealers short calls / long puts"),
        "selected_flip_crossing_direction": direction,
        "crossing_count": metric(len(roots), source, quote_time, "crossings"),
        "dealer_gex_estimate": {
            "status": "ok" if complete else "partial",
            "label": "Estimated signed dealer GEX; not observable dealer positioning",
            "dealer_position_assumption": "Dealer short calls (negative gamma), dealer long puts (positive gamma)",
            "open_interest_unit": f"{asset} base units as reported by Deribit",
            "gex_formula": "sign * Black-Scholes gamma * OI_base * expiry_underlying_price^2 * 0.01",
            "repriced_flip_method": "Parallel-shift expiry underlyings with spot; constant IV; 50%-150% grid",
            "input_coverage": {"expected": len(instruments), "usable": len(rows), "missing": missing_inputs},
            **metadata,
        },
        "headline_surface": headline_surface,
        "index_price": index_metric,
    }
