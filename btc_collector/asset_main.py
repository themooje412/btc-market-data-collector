"""Hourly collector entry point for ETH, SOL and ZEC.

Outputs live under a per-asset directory so the existing BTC contracts and
execution watcher remain backward compatible.
"""

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import logging
import os
from pathlib import Path
import time

from .asset_config import asset_config
from .asset_market_structure import collect_market_structure
from .asset_options import collect_options, unavailable_options
from .asset_sources import (
    binance_spot, coinbase_spot, get_binance_cvd, get_coinbase_cvd,
    binance_futures, deribit_perpetual, deribit_dated_futures, safe,
)
from .asset_storage import history_points, read_history, update_history
from .core import WINDOWS, epoch, iso, metric, missing, oi_changes, number
from .http import Client
from .sources import BINANCE, COINBASE, DERIBIT, FUTURES
from .storage import read_json, validate, write_json


def failed_futures(source, reason, asset):
    out = {
        "oi": missing(source, reason, unit=asset),
        "mark_price": missing(source, reason),
        "index_price": missing(source, reason),
        "funding": missing(source, reason),
    }
    out["basis"] = {k: missing(source, reason) for k in ("absolute", "bps", "annualized_pct")}
    return out


def failed_options(reason, asset):
    out = unavailable_options(asset)
    out["status"] = "error"
    out["reason"] = reason
    for key in (
        "total_oi", "gross_gex_proxy", "put_wall", "call_wall", "gamma_concentrations",
        "net_gex_estimate_usd_per_1pct", "gex_by_strike", "zero_gamma_flip",
        "zero_gamma_flip_repriced", "zero_gamma_flip_cumulative_strike",
        "spot_to_gamma_flip_pct", "gamma_regime", "selected_flip_crossing_direction", "crossing_count",
    ):
        unit = asset if key == "total_oi" else None
        out[key] = missing(DERIBIT, reason, unit=unit)
    out["dealer_gex_estimate"] = {"status": "error", "reason": reason}
    out["headline_surface"] = {
        "expiry": None,
        **{key: missing(DERIBIT, reason) for key in ("atm_iv", "risk_reversal_25d", "call_25d_iv", "put_25d_iv")},
    }
    return out


def failed_market_structure(reason, symbol):
    fields = (
        "vwap", "poc", "vah", "val", "value_area_volume_pct", "spot_location",
        "distance_to_vwap_pct", "distance_to_poc_pct", "distance_to_vah_pct", "distance_to_val_pct",
    )
    source = f"Binance {symbol}"
    return {
        "status": "error", "reason": reason,
        **{
            window: {"status": "error", **{key: missing(source, reason) for key in fields}}
            for window in ("utc_session", "rolling_24h", "rolling_7d")
        },
    }


def age_metrics(obj, now, path="", ages=None):
    if ages is None:
        ages = {}
    if isinstance(obj, dict):
        if "value" in obj and "status" in obj:
            age = max(0, now - epoch(obj["timestamp"])) if obj.get("timestamp") else None
            obj["data_age_seconds"] = round(age, 3) if age is not None else None
            if obj["status"] == "ok" and age is not None and age > 900:
                obj.update(value=None, status="stale", reason="Older than 15 minutes at publication")
            if not path.startswith("options.contracts"):
                ages[path] = obj["data_age_seconds"]
        for key, value in obj.items():
            if key != "data_age_seconds":
                age_metrics(value, now, f"{path}.{key}".strip("."), ages)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            age_metrics(value, now, f"{path}.{index}", ages)
    return ages


def _premium(cb, bn, fx, cfg, max_skew=60):
    asset = cfg["asset"]
    product = cfg["coinbase_product"]
    symbol = cfg["binance_symbol"]

    def calc(items, converted):
        source = (
            f"Coinbase {product} minus Binance {symbol} converted with Coinbase USDT-USD"
            if converted else f"Coinbase {product} minus Binance {symbol} (unadjusted)"
        )
        if any(item.get("status") != "ok" or item.get("value") is None for item in items):
            return {key: missing(source, "Fresh inputs unavailable", unit=unit) for key, unit in (("usd", "USD"), ("bps", "bp"), ("pct", "%"))}
        timestamps = [epoch(item["timestamp"]) for item in items]
        if max(timestamps) - min(timestamps) > max_skew:
            return {key: missing(source, "Input timestamps differ by more than 60s", status="stale", unit=unit) for key, unit in (("usd", "USD"), ("bps", "bp"), ("pct", "%"))}
        base = number(bn["value"], 1e-12) * (number(fx["value"], 1e-12) if converted else 1.0)
        diff = number(cb["value"], 1e-12) - base
        return {
            "usd": metric(diff, source, min(timestamps), "USD", assumption=None if converted else "1 USDT = 1 USD"),
            "bps": metric(diff / base * 10000, source, min(timestamps), "bp"),
            "pct": metric(diff / base * 100, source, min(timestamps), "%"),
        }

    raw = calc([cb, bn], False)
    adjusted = calc([cb, bn, fx], True)
    return {
        **raw,
        "fx_adjusted": adjusted,
        "raw_coinbase_premium": raw,
        "fx_adjusted_coinbase_premium": adjusted,
        "methodology": f"{asset} price difference; not the CoinGlass Coinbase Premium Index",
    }


def _oi_changes(current, points, source):
    if current.get("status") == "not_applicable":
        return {window: missing(source, "Venue/market not available", status="not_applicable", unit="%") for window in ("1h", "4h", "24h")}
    return oi_changes(current, points, source)


def collect(root, asset, client=None, max_pages=600):
    cfg = asset_config(asset)
    asset = cfg["asset"]
    symbol = cfg["binance_symbol"]
    product = cfg["coinbase_product"]
    width = cfg["market_profile_bin_width"]
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    started = time.time()
    end = int(started) // 60 * 60
    client = client or Client()
    notes = []
    try:
        state = read_json(root / "state/coinbase.json", {})
    except Exception as exc:
        state = {}
        notes.append("Coinbase state reset after read error: " + str(exc))
    try:
        market_state = read_json(root / "state/market_structure.json", {})
    except Exception as exc:
        market_state = {}
        notes.append("Market-structure state reset after read error: " + str(exc))
    history = read_history(root / "history.csv")

    def fail_cvd(source, reason):
        return {window: missing(source, reason, unit=asset) for window in WINDOWS}

    tasks = {
        "binance_spot": (
            lambda: binance_spot(client, symbol, asset),
            lambda exc: missing(BINANCE, exc, unit="USDT"),
        ),
        "coinbase_spot": (
            lambda: coinbase_spot(client, product, asset),
            lambda exc: missing(COINBASE, exc, unit="USD"),
        ),
        "usdt_usd": (
            lambda: coinbase_spot(client, "USDT-USD", "USDT"),
            lambda exc: missing(COINBASE, exc, unit="USD"),
        ),
        "binance_cvd": (
            lambda: get_binance_cvd(client, end, symbol, asset),
            lambda exc: fail_cvd(BINANCE, exc),
        ),
        "coinbase_cvd": (
            lambda: get_coinbase_cvd(client, end, state, product, asset, max_pages),
            lambda exc: (fail_cvd(COINBASE, exc), state, {"error": exc}),
        ),
        "binance_futures": (
            lambda: binance_futures(client, symbol, asset),
            lambda exc: failed_futures(FUTURES, exc, asset),
        ),
        "deribit_futures": (
            lambda: deribit_perpetual(client, asset),
            lambda exc: failed_futures(DERIBIT, exc, asset),
        ),
        "dated_futures": (
            lambda: deribit_dated_futures(client, asset),
            lambda exc: missing(DERIBIT, exc),
        ),
        "options": (
            lambda: collect_options(client, asset),
            lambda exc: failed_options(exc, asset),
        ),
        "market_structure": (
            lambda: collect_market_structure(client, end, market_state, symbol, asset, width, type(client) is Client),
            lambda exc: (failed_market_structure(exc, symbol), market_state),
        ),
    }

    with ThreadPoolExecutor(max_workers=10) as pool:
        jobs = {name: pool.submit(safe, fn, fallback) for name, (fn, fallback) in tasks.items()}
        results = {name: job.result() for name, job in jobs.items()}

    binance_deriv = results["binance_futures"]
    deribit_deriv = results["deribit_futures"]
    options = results["options"]
    oi = {}
    for venue, data in (("binance", binance_deriv), ("deribit", deribit_deriv)):
        historical = data.pop("historical_points", []) if isinstance(data, dict) else []
        current = data["oi"]
        points = history_points(history, venue) + historical
        oi[venue] = {
            "current": current,
            "changes": _oi_changes(current, points, venue + f" same-market {asset} OI"),
        }

    cb_cvd, new_state, cb_details = results["coinbase_cvd"]
    market_structure, new_market_state = results["market_structure"]
    now = time.time()
    premiums = _premium(results["coinbase_spot"], results["binance_spot"], results["usdt_usd"], cfg)

    headline = options.get("headline_surface", {})
    snapshot = {
        "schema_version": "1.0.0-multi-asset",
        "asset": asset,
        "symbols": {"binance": symbol, "coinbase": product},
        "timestamp": iso(now), "collection_started_at": iso(started),
        "snapshot_hour": iso(int(started) // 3600 * 3600), "cvd_window_end": iso(end),
        "collection_duration_seconds": round(now-started, 3), "status": "ok", "data_age": {},
        "spot": {"binance": results["binance_spot"], "coinbase": results["coinbase_spot"], "usdt_usd": results["usdt_usd"]},
        "coinbase_premium": premiums,
        "raw_coinbase_premium": premiums["raw_coinbase_premium"],
        "fx_adjusted_coinbase_premium": premiums["fx_adjusted_coinbase_premium"],
        "cvd": {"binance": results["binance_cvd"], "coinbase": cb_cvd, "coinbase_collection": cb_details},
        "futures": {
            "binance": {key: value for key, value in binance_deriv.items() if key not in ("oi", "funding", "basis")},
            "deribit": {key: value for key, value in deribit_deriv.items() if key not in ("oi", "funding", "basis")},
        },
        "open_interest": oi,
        "funding": {"binance": binance_deriv["funding"], "deribit": deribit_deriv["funding"]},
        "basis": {"binance": binance_deriv["basis"], "deribit": deribit_deriv["basis"], "dated_futures": results["dated_futures"]},
        "market_structure": market_structure,
        "options": options,
        "put_wall": options["put_wall"], "call_wall": options["call_wall"],
        "atm_iv": headline.get("atm_iv", missing(DERIBIT, "Headline surface unavailable")),
        "skew_25d": headline.get("risk_reversal_25d", missing(DERIBIT, "Headline surface unavailable")),
        "gamma_concentrations": options["gamma_concentrations"],
        "quality": {
            "notes": notes,
            "source_policy": "No cross-venue substitution for unavailable derivatives/options; not_applicable is explicit",
            "market_structure_policy": "Exact Binance 1m quote/base VWAP plus exact aggregate-trade volume profile; no candle profile proxy",
            "gex_warning": "Signed dealer GEX is a model estimate using the explicit short-call/long-put assumption, not observed dealer positioning",
            "binance_futures_warning": "GitHub-hosted runners may return HTTP 451 from Binance Futures; null/error is preserved rather than substituted",
            "freshness_policy": "Age is measured at publication; consumers must also compare snapshot timestamp to current UTC",
        },
    }

    snapshot["data_age"] = age_metrics(snapshot, now)
    counts = {"ok": 0, "error": 0, "stale": 0, "not_applicable": 0, "warming_up": 0}

    def count(obj):
        if isinstance(obj, dict):
            if "value" in obj and obj.get("status") in counts:
                counts[obj["status"]] += 1
            for value in obj.values():
                count(value)
        elif isinstance(obj, list):
            for value in obj:
                count(value)

    count(snapshot)
    snapshot["quality"]["metric_status_counts"] = counts
    snapshot["status"] = "partial" if counts["error"] or counts["stale"] or counts["warming_up"] else "ok"
    if counts["ok"] == 0:
        snapshot["status"] = "error"

    chain = copy.deepcopy(snapshot["options"])
    chain["snapshot_timestamp"] = snapshot["timestamp"]
    write_json(root / "options_chain.json", chain)
    snapshot["options"].pop("contracts", None)
    snapshot["options"]["contracts_file"] = "options_chain.json"
    snapshot["data_age"] = {key: value for key, value in snapshot["data_age"].items() if not key.startswith("options.contracts")}

    validate(snapshot)
    write_json(root / "latest.json", snapshot)
    update_history(root / "history.csv", snapshot)
    write_json(root / "state/coinbase.json", new_state)
    write_json(root / "state/market_structure.json", new_market_state)
    report = {
        "tested_at": iso(now), "asset": asset, "status": snapshot["status"],
        "endpoint_requests": [
            {**row, "requested_at": iso(row["requested_at"]), "received_at": iso(row["received_at"])}
            for row in client.audit
        ],
    }
    write_json(root / "docs/latest-endpoint-check.json", report)

    summary = f"{asset} collector: {snapshot['status']} | {counts['ok']} OK, {counts['error']} errors, {counts['not_applicable']} N/A | {iso(now)}"
    logging.info(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(f"## {asset} collector\n\n{summary}\n")
    if snapshot["status"] != "ok":
        print("::warning::" + summary, flush=True)
    return snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True, choices=("ETH", "SOL", "ZEC"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--coinbase-max-pages", type=int, default=600)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime
    root = args.output_dir or Path("assets") / args.asset.lower()
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".collector.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        collect(root, args.asset, max_pages=args.coinbase_max_pages)


if __name__ == "__main__":
    main()
