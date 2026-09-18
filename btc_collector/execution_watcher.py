"""Five-minute execution watcher built on the hourly collector context.

The watcher does not recompute the expensive option surface or 7-day market
profile. It reads ``latest.json`` as the slow context layer, samples a small
set of fast public endpoints, and emits deterministic execution events around
the already-computed structural levels.

``execution_snapshot.json`` and ``execution_state.json`` are refreshed on each
run so downstream consumers can see the latest 5-minute state. History grows
only when the material state signature changes.
"""

import argparse
import csv
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .core import binance_cvd, iso, premium
from .http import Client
from .sources import BINANCE, binance_spot, coinbase_spot, deribit_perpetual
from .storage import atomic_write, read_json, write_json

SCHEMA_VERSION = "1.0.0"
LEVEL_CLUSTER_TOLERANCE_PCT = 0.0015  # 0.15%
MAX_LEVEL_DISTANCE_PCT = 0.006        # 0.60%
RETEST_BUFFER_PCT = 0.0008            # 0.08%
INVALIDATION_BUFFER_PCT = 0.0015      # 0.15%
EARLY_PROXIMITY_PCT = 0.0025          # 0.25%
ACTIVE_STATES = {
    "ARMED_LONG", "ARMED_SHORT", "LONG_TRIGGERED", "SHORT_TRIGGERED",
    "ADD_ALLOWED", "HOLD_MANAGE",
}


def _metric_value(root, *path):
    obj = root
    for part in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    if not isinstance(obj, dict) or obj.get("status") != "ok" or obj.get("value") is None:
        return None
    return obj["value"]


def _numeric_metric(root, *path):
    value = _metric_value(root, *path)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _metric_text(root, *path):
    value = _metric_value(root, *path)
    return value if isinstance(value, str) else None


def cluster_levels(levels, tolerance_pct=LEVEL_CLUSTER_TOLERANCE_PCT):
    """Merge nearby structural levels into stable confluence clusters."""
    valid = sorted((str(name), float(value)) for name, value in levels if value and float(value) > 0)
    clusters = []
    for name, value in valid:
        if not clusters:
            clusters.append({"value": value, "labels": [name], "values": [value]})
            continue
        current = clusters[-1]
        center = sum(current["values"]) / len(current["values"])
        if abs(value - center) / center <= tolerance_pct:
            current["labels"].append(name)
            current["values"].append(value)
            current["value"] = sum(current["values"]) / len(current["values"])
        else:
            clusters.append({"value": value, "labels": [name], "values": [value]})
    for cluster in clusters:
        cluster["value"] = round(cluster["value"], 2)
        cluster["values"] = [round(v, 2) for v in cluster["values"]]
        cluster["confluence_count"] = len(cluster["labels"])
        cluster["id"] = "+".join(cluster["labels"])
    return clusters


def build_plan(snapshot):
    """Build a slow execution map from one hourly collector snapshot."""
    spot = _numeric_metric(snapshot, "spot", "binance")
    if spot is None:
        raise ValueError("Hourly collector has no valid Binance spot reference")

    levels = []
    for window_name, prefix in (
        ("session", "utc_session"),
        ("24h", "rolling_24h"),
        ("7d", "rolling_7d"),
    ):
        for field in ("vwap", "poc", "vah", "val"):
            value = _numeric_metric(snapshot, "market_structure", prefix, field)
            if value is not None:
                levels.append((f"{window_name}_{field}", value))

    for label, path in (
        ("cumulative_gamma_flip", ("options", "zero_gamma_flip_cumulative_strike")),
        ("repriced_gamma_flip", ("options", "zero_gamma_flip_repriced")),
        ("call_wall", ("call_wall",)),
        ("put_wall", ("put_wall",)),
    ):
        value = _numeric_metric(snapshot, *path)
        if value is not None:
            levels.append((label, value))

    clusters = [c for c in cluster_levels(levels) if abs(c["value"] - spot) / spot <= 0.03]
    below = [c for c in clusters if c["value"] <= spot]
    above = [c for c in clusters if c["value"] > spot]
    nearest_below = max(below, key=lambda c: c["value"], default=None)
    nearest_above = min(above, key=lambda c: c["value"], default=None)

    return {
        "schema_version": SCHEMA_VERSION,
        "profile": "controlled_aggressive",
        "context_timestamp": snapshot.get("timestamp"),
        "context_spot": round(spot, 2),
        "gamma_regime": _metric_text(snapshot, "options", "gamma_regime"),
        "levels": clusters,
        "nearest_below": nearest_below,
        "nearest_above": nearest_above,
        "rules": {
            "early_probe_r": [0.40, 0.50],
            "confirmation_total_r": [0.75, 0.90],
            "max_total_r": 1.25,
            "normal_min_rr": 1.8,
            "a_plus_early_probe_min_rr": 1.6,
            "no_chase_atr": 0.65,
            "no_chase_r": 1.0,
            "retest_buffer_pct": RETEST_BUFFER_PCT * 100,
            "invalidation_buffer_pct": INVALIDATION_BUFFER_PCT * 100,
        },
        "semantics": {
            "repriced_gamma_flip": "structural gamma-regime threshold",
            "cumulative_gamma_flip": "tactical strike/OI balance level",
            "note": "Fast watcher detects events; it does not recompute the hourly structural context.",
        },
    }


def aggregate_5m(klines):
    """Aggregate exact closed Binance 1m klines into complete 5m candles."""
    buckets = {}
    for row in klines:
        t = int(row[0]) // 1000
        bucket = t - t % 300
        buckets.setdefault(bucket, []).append(row)

    out = []
    for bucket in sorted(buckets):
        rows = sorted(buckets[bucket], key=lambda r: int(r[0]))
        expected = [bucket + i * 60 for i in range(5)]
        actual = [int(r[0]) // 1000 for r in rows]
        if actual != expected:
            continue
        out.append({
            "start": iso(bucket),
            "end": iso(bucket + 300),
            "open": float(rows[0][1]),
            "high": max(float(r[2]) for r in rows),
            "low": min(float(r[3]) for r in rows),
            "close": float(rows[-1][4]),
            "quote_volume": sum(float(r[7]) for r in rows),
            "quote_cvd": sum(2 * float(r[10]) - float(r[7]) for r in rows),
        })
    return out


def flow_score(fast):
    """Small deterministic fast-flow score; not a probability model."""
    score = 0
    cvd15 = _numeric_metric(fast, "binance_cvd", "15m", "quote_cvd")
    cvd1h = _numeric_metric(fast, "binance_cvd", "1h", "quote_cvd")
    fx = _numeric_metric(fast, "premium", "fx_adjusted", "bps")

    if cvd15 is not None:
        if cvd15 > 1_000_000:
            score += 2
        elif cvd15 > 0:
            score += 1
        elif cvd15 < -1_000_000:
            score -= 2
        elif cvd15 < 0:
            score -= 1
    if cvd1h is not None:
        if cvd1h > 3_000_000:
            score += 1
        elif cvd1h < -3_000_000:
            score -= 1
    if fx is not None:
        if fx > 0.5:
            score += 1
        elif fx < -0.5:
            score -= 1
    return max(-4, min(4, score))


def nearest_cluster(plan, spot):
    levels = plan.get("levels") or []
    if not levels or spot is None:
        return None
    cluster = min(levels, key=lambda c: abs(float(c["value"]) - spot))
    distance = abs(float(cluster["value"]) - spot) / spot
    if distance > MAX_LEVEL_DISTANCE_PCT:
        return None
    return cluster


def price_event(candles, anchor):
    if len(candles) < 2:
        return "none"
    previous, current = candles[-2], candles[-1]
    level = float(anchor)
    up = level * (1 + RETEST_BUFFER_PCT)
    down = level * (1 - RETEST_BUFFER_PCT)

    if current["low"] < down and current["close"] > level:
        return "sweep_reclaim_long"
    if current["high"] > up and current["close"] < level:
        return "sweep_reject_short"
    if previous["close"] > level and current["low"] <= up and current["close"] > level:
        return "retest_hold_long"
    if previous["close"] < level and current["high"] >= down and current["close"] < level:
        return "retest_hold_short"
    if previous["close"] <= level < current["close"]:
        return "cross_up"
    if previous["close"] >= level > current["close"]:
        return "cross_down"
    return "none"


def trade_geometry(plan, fast, anchor, direction):
    """Derive stop/targets from current 5m structure and hourly level map."""
    spot = _numeric_metric(fast, "spot", "binance")
    candles = fast.get("candles_5m") or []
    if spot is None or not anchor or len(candles) < 2 or direction not in ("long", "short"):
        return None

    level = float(anchor["value"])
    recent = candles[-2:]
    levels = sorted(float(c["value"]) for c in plan.get("levels", []))

    if direction == "long":
        recent_extreme = min(float(c["low"]) for c in recent)
        stop = min(level * (1 - INVALIDATION_BUFFER_PCT), recent_extreme * 0.9995)
        targets = [v for v in levels if v > spot * 1.0005]
    else:
        recent_extreme = max(float(c["high"]) for c in recent)
        stop = max(level * (1 + INVALIDATION_BUFFER_PCT), recent_extreme * 1.0005)
        targets = sorted((v for v in levels if v < spot * 0.9995), reverse=True)

    risk = (spot - stop) if direction == "long" else (stop - spot)
    if risk <= 0:
        return None
    target1 = targets[0] if targets else None
    target2 = targets[1] if len(targets) > 1 else None
    reward = None
    if target1 is not None:
        reward = (target1 - spot) if direction == "long" else (spot - target1)
    rr = reward / risk if reward is not None and reward > 0 else None

    return {
        "entry_reference": round(spot, 2),
        "hard_stop": round(stop, 2),
        "target1": round(target1, 2) if target1 is not None else None,
        "target2": round(target2, 2) if target2 is not None else None,
        "risk_usd": round(risk, 2),
        "risk_pct": round(risk / spot * 100, 4),
        "rr_to_target1": round(rr, 3) if rr is not None else None,
    }


def classify_state(plan, fast, previous=None):
    spot = _numeric_metric(fast, "spot", "binance")
    candles = fast.get("candles_5m") or []
    if spot is None or len(candles) < 2:
        return _state("NO_TRADE", None, None, "none", 0, plan, fast, "Insufficient fast data")

    score = flow_score(fast)
    if previous and previous.get("state") in ACTIVE_STATES and previous.get("anchor"):
        # Once armed/triggered, the setup remains tied to its original anchor.
        # Do not silently hop to a lower/higher nearby level and turn a failed
        # trade into an opposite fresh signal.
        anchor = previous["anchor"]
    else:
        anchor = nearest_cluster(plan, spot)

    if anchor is None:
        return _state("NO_TRADE", None, None, "none", score, plan, fast, "No structural level within 0.60%")

    event = price_event(candles, anchor["value"])
    level = float(anchor["value"])
    close = float(candles[-1]["close"])
    gamma = plan.get("gamma_regime")
    old = previous.get("state") if previous else None
    direction = previous.get("direction") if previous else None

    if previous and old in ACTIVE_STATES:
        if old in ("LONG_TRIGGERED", "ADD_ALLOWED", "HOLD_MANAGE") and direction == "long":
            if close < level * (1 - INVALIDATION_BUFFER_PCT) and score <= -1:
                return _state("INVALIDATED", "long", anchor, event, score, plan, fast,
                              "Anchor lost with opposing fast flow")
            if event == "retest_hold_long" and score >= 2:
                return _state("ADD_ALLOWED", "long", anchor, event, score, plan, fast,
                              "Retest held and fast flow strengthened")
            return _state("HOLD_MANAGE", "long", anchor, event, score, plan, fast,
                          "Triggered long thesis remains tied to original anchor")
        if old in ("SHORT_TRIGGERED", "ADD_ALLOWED", "HOLD_MANAGE") and direction == "short":
            if close > level * (1 + INVALIDATION_BUFFER_PCT) and score >= 1:
                return _state("INVALIDATED", "short", anchor, event, score, plan, fast,
                              "Anchor reclaimed with opposing fast flow")
            if event == "retest_hold_short" and score <= -2:
                return _state("ADD_ALLOWED", "short", anchor, event, score, plan, fast,
                              "Retest held and fast flow strengthened")
            return _state("HOLD_MANAGE", "short", anchor, event, score, plan, fast,
                          "Triggered short thesis remains tied to original anchor")
        if old == "ARMED_LONG":
            if event in ("retest_hold_long", "sweep_reclaim_long") and score >= 1:
                return _state("LONG_TRIGGERED", "long", anchor, event, score, plan, fast,
                              "Armed long received retest/reclaim confirmation")
            if close < level * (1 - INVALIDATION_BUFFER_PCT) and score < 0:
                return _state("INVALIDATED", "long", anchor, event, score, plan, fast,
                              "Armed long lost original anchor")
            return _state("ARMED_LONG", "long", anchor, event, score, plan, fast,
                          "Awaiting long trigger at original anchor")
        if old == "ARMED_SHORT":
            if event in ("retest_hold_short", "sweep_reject_short") and score <= -1:
                return _state("SHORT_TRIGGERED", "short", anchor, event, score, plan, fast,
                              "Armed short received retest/rejection confirmation")
            if close > level * (1 + INVALIDATION_BUFFER_PCT) and score > 0:
                return _state("INVALIDATED", "short", anchor, event, score, plan, fast,
                              "Armed short lost original anchor")
            return _state("ARMED_SHORT", "short", anchor, event, score, plan, fast,
                          "Awaiting short trigger at original anchor")

    if event in ("sweep_reclaim_long", "retest_hold_long") and score >= 0:
        return _state("LONG_TRIGGERED", "long", anchor, event, score, plan, fast,
                      "Sweep/retest long at structural anchor")
    if event in ("sweep_reject_short", "retest_hold_short") and score <= 0:
        return _state("SHORT_TRIGGERED", "short", anchor, event, score, plan, fast,
                      "Sweep/retest short at structural anchor")
    if event == "cross_up":
        if gamma == "short_gamma" and score >= 2:
            return _state("LONG_TRIGGERED", "long", anchor, event, score, plan, fast,
                          "Controlled-aggressive short-gamma breakout probe")
        if score >= 1:
            return _state("ARMED_LONG", "long", anchor, event, score, plan, fast,
                          "Breakout seen; waiting for retest")
        return _state("EARLY_SETUP", "long", anchor, event, score, plan, fast,
                      "Breakout lacks fast-flow confirmation")
    if event == "cross_down":
        if gamma == "short_gamma" and score <= -2:
            return _state("SHORT_TRIGGERED", "short", anchor, event, score, plan, fast,
                          "Controlled-aggressive short-gamma breakdown probe")
        if score <= -1:
            return _state("ARMED_SHORT", "short", anchor, event, score, plan, fast,
                          "Breakdown seen; waiting for retest")
        return _state("EARLY_SETUP", "short", anchor, event, score, plan, fast,
                      "Breakdown lacks fast-flow confirmation")

    proximity = abs(spot - level) / spot
    if proximity <= EARLY_PROXIMITY_PCT and score >= 2:
        return _state("EARLY_SETUP", "long", anchor, event, score, plan, fast,
                      "Positive fast flow near structural level")
    if proximity <= EARLY_PROXIMITY_PCT and score <= -2:
        return _state("EARLY_SETUP", "short", anchor, event, score, plan, fast,
                      "Negative fast flow near structural level")
    return _state("NO_TRADE", None, anchor, event, score, plan, fast, "No executable trigger")


def _state(state, direction, anchor, event, score, plan, fast, reason):
    setup_map = {
        "sweep_reclaim_long": "sweep-reclaim",
        "sweep_reject_short": "failed-break/sweep-reject",
        "retest_hold_long": "breakout-retest/trend-pullback",
        "retest_hold_short": "breakdown-retest/trend-pullback",
        "cross_up": "breakout",
        "cross_down": "breakdown",
    }
    spot = _numeric_metric(fast, "spot", "binance")
    geometry = trade_geometry(plan, fast, anchor, direction) if direction else None

    # Real triggers must still have acceptable geometry. A strong location is
    # not permission to chase into a poor first-target R/R.
    if state in ("LONG_TRIGGERED", "SHORT_TRIGGERED"):
        rr = geometry.get("rr_to_target1") if geometry else None
        early_exception = plan.get("gamma_regime") == "short_gamma" and abs(score) >= 2 and event in ("cross_up", "cross_down")
        minimum = 1.6 if early_exception else 1.8
        if rr is None or rr < minimum:
            state = "MISSED_DO_NOT_CHASE"
            reason = f"Trigger present but first-target R/R is below {minimum:.1f}"

    result = {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "direction": direction,
        "setup_type": setup_map.get(event),
        "event": event,
        "anchor": anchor,
        "flow_score": score,
        "reason": reason,
        "context_timestamp": plan.get("context_timestamp"),
        "fast_timestamp": fast.get("timestamp"),
        "fast_spot": round(spot, 2) if spot is not None else None,
        "execution": geometry,
    }
    anchor_id = anchor.get("id") if anchor else "none"
    result["signature"] = "|".join([
        str(state), str(direction or "none"), anchor_id, str(result["setup_type"] or "none")
    ])
    return result


def _fetch_binance_minutes(client, end, minutes=70):
    rows, _, _ = client.get(
        BINANCE,
        "/api/v3/klines",
        symbol="BTCUSDT",
        interval="1m",
        startTime=(end - minutes * 60) * 1000,
        endTime=end * 1000 - 1,
        limit=1000,
    )
    return rows


def collect_fast(client=None):
    started = time.time()
    end = int(started) // 60 * 60
    client = client or Client(timeout=20, attempts=2)
    tasks = {
        "binance_spot": lambda: binance_spot(client),
        "coinbase_spot": lambda: coinbase_spot(client),
        "usdt_usd": lambda: coinbase_spot(client, "USDT-USD"),
        "deribit": lambda: deribit_perpetual(client),
        "minutes": lambda: _fetch_binance_minutes(client, end),
    }
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {name: pool.submit(fn) for name, fn in tasks.items()}
        for name, job in jobs.items():
            try:
                results[name] = job.result()
            except Exception as exc:
                errors[name] = str(exc)

    premiums = None
    if all(k in results for k in ("coinbase_spot", "binance_spot", "usdt_usd")):
        try:
            premiums = premium(results["coinbase_spot"], results["binance_spot"], results["usdt_usd"])
        except Exception as exc:
            errors["premium"] = str(exc)

    cvd, candles = {}, []
    if "minutes" in results:
        try:
            all_cvd = binance_cvd(results["minutes"], end)
            cvd = {name: all_cvd[name] for name in ("15m", "1h")}
            candles = aggregate_5m(results["minutes"])[-12:]
        except Exception as exc:
            errors["binance_fast_flow"] = str(exc)

    now = time.time()
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": iso(now),
        "window_end": iso(end),
        "collection_duration_seconds": round(now - started, 3),
        "status": "partial" if errors else "ok",
        "errors": errors,
        "spot": {
            "binance": results.get("binance_spot"),
            "coinbase": results.get("coinbase_spot"),
            "usdt_usd": results.get("usdt_usd"),
        },
        "premium": premiums or {},
        "binance_cvd": cvd,
        "deribit": results.get("deribit") or {},
        "candles_5m": candles,
    }


def _append_history(path, state):
    path = Path(path)
    rows = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    row = {
        "timestamp": state.get("fast_timestamp") or "",
        "context_timestamp": state.get("context_timestamp") or "",
        "state": state.get("state") or "",
        "direction": state.get("direction") or "",
        "setup_type": state.get("setup_type") or "",
        "event": state.get("event") or "",
        "anchor_id": (state.get("anchor") or {}).get("id", ""),
        "anchor_value": (state.get("anchor") or {}).get("value", ""),
        "flow_score": state.get("flow_score", ""),
        "fast_spot": state.get("fast_spot", ""),
        "reason": state.get("reason") or "",
        "signature": state.get("signature") or "",
    }
    rows.append(row)
    rows = rows[-1000:]
    fields = list(row)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(path, buffer.getvalue())


def run(root, client=None):
    root = Path(root)
    context = read_json(root / "latest.json", {})
    if not context:
        raise ValueError("latest.json is required before the execution watcher can run")

    old_plan = read_json(root / "execution_plan.json", {})
    plan = build_plan(context)
    if old_plan.get("context_timestamp") != plan.get("context_timestamp"):
        write_json(root / "execution_plan.json", plan)
    else:
        plan = old_plan

    fast = collect_fast(client)
    write_json(root / "execution_snapshot.json", fast)

    previous = read_json(root / "execution_state.json", {})
    state = classify_state(plan, fast, previous)
    changed = previous.get("signature") != state.get("signature")
    if changed:
        state["previous_state"] = previous.get("state")
        state["state_changed_at"] = state.get("fast_timestamp")
        _append_history(root / "execution_history.csv", state)
    else:
        state["previous_state"] = previous.get("previous_state")
        state["state_changed_at"] = previous.get("state_changed_at")
    write_json(root / "execution_state.json", state)

    summary = (
        f"BTC execution watcher: {fast['status']} | {state.get('state','NO_TRADE')} | "
        f"flow {flow_score(fast):+d} | {fast['timestamp']}"
    )
    logging.info(summary)
    return plan, fast, state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime
    run(args.output_dir)


if __name__ == "__main__":
    main()
