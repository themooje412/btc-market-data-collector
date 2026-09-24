"""Shared 5-minute execution layer for BTC, ETH, SOL and ZEC.

The hourly collectors own the slow market context. This module applies one
execution policy to all four assets while keeping each asset's state isolated.
The policy is intentionally about 30% more permissive than the original BTC
watcher on trend-following entries, without increasing the 1.25R total-risk
ceiling or relaxing counter-trend confirmation.
"""

import argparse
import csv
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import execution_watcher as base
from .asset_config import asset_config
from .asset_sources import (
    _generic_binance_cvd,
    binance_spot as asset_binance_spot,
    coinbase_spot as asset_coinbase_spot,
    deribit_perpetual as asset_deribit_perpetual,
)
from .core import epoch, iso, metric, missing, number
from .http import Client
from .sources import BINANCE
from .storage import atomic_write, read_json, write_json

ASSETS = ("BTC", "ETH", "SOL", "ZEC")
SCHEMA_VERSION = "1.2.0"
PROFILE = "controlled_aggressive_swing_plus30"
MAX_LEVEL_DISTANCE_PCT = 0.0078       # original 0.60% * 1.30
EARLY_PROXIMITY_PCT = 0.00325         # original 0.25% * 1.30
NORMAL_MIN_RR = 1.60
A_PLUS_EARLY_MIN_RR = 1.45
CORE_BIAS_SCORE = 5.50
COUNTERTREND_MIN_FLOW_SCORE = 3
OPPOSITE_COOLDOWN_SECONDS = 30 * 60
ACTIVE_STATES = base.ACTIVE_STATES


def _metric_value(root, *path):
    return base._metric_value(root, *path)


def _numeric_metric(root, *path):
    return base._numeric_metric(root, *path)


def _strategic_direction(plan):
    bias = (plan.get("strategic_bias") or {}).get("value")
    if bias == "bullish":
        return "long"
    if bias == "bearish":
        return "short"
    return None


def _core_candidate(plan):
    bias = plan.get("strategic_bias") or {}
    score = float(bias.get("score") or 0.0)
    if score >= CORE_BIAS_SCORE:
        direction = "long"
    elif score <= -CORE_BIAS_SCORE:
        direction = "short"
    else:
        direction = None
    magnitude = abs(score)
    if direction is None:
        status = "NONE"
        starter = None
    elif magnitude >= 7.0:
        status = "TREND_ACTIVE"
        starter = [0.50, 0.65]
    else:
        status = "EARLY_ENTRY_CANDIDATE"
        starter = [0.35, 0.45]
    return {
        "status": status,
        "direction": direction,
        "score": round(score, 2),
        "entry_bias_threshold": CORE_BIAS_SCORE,
        "starter_r": starter,
        "horizon": "2-5d_or_longer_if_structure_holds",
        "note": "Core swing can be evaluated even when the 5m tactical state is NO_TRADE.",
    }


def build_plan(snapshot, asset="BTC"):
    asset = str(asset).upper()
    plan = base.build_plan(snapshot)
    plan["schema_version"] = SCHEMA_VERSION
    plan["asset"] = asset
    plan["profile"] = PROFILE
    plan["rules"].update({
        "aggressiveness_vs_original_pct": 30,
        "core_bias_score_threshold": CORE_BIAS_SCORE,
        "core_early_starter_r": [0.35, 0.45],
        "core_normal_starter_r": [0.50, 0.65],
        "confirmation_total_r": [0.80, 1.00],
        "max_total_r": 1.25,
        "normal_min_rr": NORMAL_MIN_RR,
        "a_plus_early_probe_min_rr": A_PLUS_EARLY_MIN_RR,
        "max_level_distance_pct": MAX_LEVEL_DISTANCE_PCT * 100,
        "early_proximity_pct": EARLY_PROXIMITY_PCT * 100,
    })
    plan["semantics"]["core_swing"] = (
        "Slow 2-5d trend position; local 5m noise is timing, not the core directional thesis"
    )
    plan["semantics"]["aggressiveness"] = (
        "About 30% more permissive on trend-following entries; counter-trend gate and 1.25R cap unchanged"
    )
    plan["core_swing"] = _core_candidate(plan)
    return plan


def _quote_volume(candles, count):
    rows = (candles or [])[-count:]
    values = [float(row.get("quote_volume") or 0.0) for row in rows]
    total = sum(values)
    return total if total > 0 else None


def flow_score(fast):
    """Cross-asset fast-flow score normalized by traded quote volume.

    15m direction is always counted. A second 15m point requires meaningful
    imbalance relative to 15m quote volume. The 1h point requires a modest
    normalized imbalance when volume is available. Synthetic/unit-test inputs
    without quote volume fall back to sign-only behavior.
    """
    score = 0
    cvd15 = _numeric_metric(fast, "binance_cvd", "15m", "quote_cvd")
    cvd1h = _numeric_metric(fast, "binance_cvd", "1h", "quote_cvd")
    fx = _numeric_metric(fast, "premium", "fx_adjusted", "bps")
    candles = fast.get("candles_5m") or []
    vol15 = _quote_volume(candles, 3)
    vol1h = _quote_volume(candles, 12)

    if cvd15 is not None and cvd15 != 0:
        sign = 1 if cvd15 > 0 else -1
        score += sign
        if vol15 and abs(cvd15) / vol15 >= 0.04:
            score += sign
    if cvd1h is not None and cvd1h != 0:
        sign = 1 if cvd1h > 0 else -1
        if vol1h is None or abs(cvd1h) / vol1h >= 0.01:
            score += sign
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
    if abs(float(cluster["value"]) - spot) / spot > MAX_LEVEL_DISTANCE_PCT:
        return None
    return cluster


def _is_countertrend(plan, direction):
    strategic = _strategic_direction(plan)
    return strategic is not None and direction in ("long", "short") and direction != strategic


def countertrend_reversal_confirmed(plan, fast, anchor, direction):
    if not _is_countertrend(plan, direction):
        return True
    candles_15m = fast.get("candles_15m") or []
    if not candles_15m or not anchor:
        return False
    lower, upper = base._zone(anchor["value"])
    close15 = float(candles_15m[-1]["close"])
    score = flow_score(fast)
    cvd15 = _numeric_metric(fast, "binance_cvd", "15m", "quote_cvd")
    cvd1h = _numeric_metric(fast, "binance_cvd", "1h", "quote_cvd")
    fx = _numeric_metric(fast, "premium", "fx_adjusted", "bps")
    if direction == "short":
        accepted = close15 < lower
        flow_ok = score <= -COUNTERTREND_MIN_FLOW_SCORE and (cvd15 or 0) < 0 and (cvd1h or 0) < 0
        premium_ok = fx is None or fx <= 0.5
        return accepted and flow_ok and premium_ok
    accepted = close15 > upper
    flow_ok = score >= COUNTERTREND_MIN_FLOW_SCORE and (cvd15 or 0) > 0 and (cvd1h or 0) > 0
    premium_ok = fx is None or fx >= -0.5
    return accepted and flow_ok and premium_ok


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _opposite_cooldown_active(plan, fast, previous, anchor, direction):
    if not previous or not anchor or direction not in ("long", "short"):
        return False
    previous_direction = previous.get("direction")
    previous_anchor = (previous.get("anchor") or {}).get("id")
    if previous_direction not in ("long", "short") or previous_direction == direction:
        return False
    if previous_anchor != anchor.get("id"):
        return False
    if _strategic_direction(plan) == direction:
        return False
    if countertrend_reversal_confirmed(plan, fast, anchor, direction):
        return False
    changed = _parse_time(previous.get("state_changed_at") or previous.get("fast_timestamp"))
    current = _parse_time(fast.get("timestamp"))
    if not changed or not current:
        return True
    return 0 <= (current - changed).total_seconds() < OPPOSITE_COOLDOWN_SECONDS


def _direction_gate(plan, fast, previous, anchor, direction):
    if _is_countertrend(plan, direction) and not countertrend_reversal_confirmed(plan, fast, anchor, direction):
        bias = (plan.get("strategic_bias") or {}).get("value", "neutral")
        return False, f"5m {direction} move is counter-trend to {bias} strategic bias; need completed 15m acceptance + strong flow"
    if _opposite_cooldown_active(plan, fast, previous, anchor, direction):
        return False, "Opposite-direction setup at same anchor is inside 30m hysteresis cooldown"
    return True, None


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
    geometry = base.trade_geometry(plan, fast, anchor, direction) if direction else None
    if state in ("LONG_TRIGGERED", "SHORT_TRIGGERED"):
        rr = geometry.get("rr_to_target1") if geometry else None
        early_exception = (
            plan.get("gamma_regime") == "short_gamma"
            and abs(score) >= 2
            and event in ("cross_up", "cross_down")
        )
        minimum = A_PLUS_EARLY_MIN_RR if early_exception else NORMAL_MIN_RR
        if rr is None or rr < minimum:
            state = "MISSED_DO_NOT_CHASE"
            reason = f"Trigger present but first-target R/R is below {minimum:.2f}"
    bias = plan.get("strategic_bias") or {"value": "neutral", "score": 0.0}
    strategic_direction = _strategic_direction(plan)
    if direction is None:
        alignment = "none"
    elif strategic_direction is None:
        alignment = "neutral"
    elif direction == strategic_direction:
        alignment = "aligned"
    else:
        alignment = "countertrend_confirmed"
    result = {
        "schema_version": SCHEMA_VERSION,
        "asset": plan.get("asset") or fast.get("asset") or "BTC",
        "state": state,
        "direction": direction,
        "setup_type": setup_map.get(event),
        "event": event,
        "anchor": anchor,
        "flow_score": score,
        "strategic_bias": bias.get("value", "neutral"),
        "strategic_bias_score": bias.get("score", 0.0),
        "bias_alignment": alignment,
        "core_swing": plan.get("core_swing"),
        "reason": reason,
        "context_timestamp": plan.get("context_timestamp"),
        "fast_timestamp": fast.get("timestamp"),
        "fast_spot": round(spot, 8) if spot is not None else None,
        "execution": geometry,
    }
    anchor_id = anchor.get("id") if anchor else "none"
    result["signature"] = "|".join([
        str(result["asset"]), str(state), str(direction or "none"), anchor_id,
        str(result["setup_type"] or "none"), str(result["strategic_bias"]),
    ])
    return result


def classify_state(plan, fast, previous=None):
    spot = _numeric_metric(fast, "spot", "binance")
    candles = fast.get("candles_5m") or []
    if spot is None or len(candles) < 2:
        return _state("NO_TRADE", None, None, "none", 0, plan, fast, "Insufficient fast data")
    score = flow_score(fast)
    if previous and previous.get("state") in ACTIVE_STATES and previous.get("anchor"):
        anchor = previous["anchor"]
    else:
        anchor = nearest_cluster(plan, spot)
    if anchor is None:
        return _state("NO_TRADE", None, None, "none", score, plan, fast, "No structural level within 0.78%")
    event = base.price_event(candles, anchor["value"])
    level = float(anchor["value"])
    close = float(candles[-1]["close"])
    gamma = plan.get("gamma_regime")
    old = previous.get("state") if previous else None
    direction = previous.get("direction") if previous else None

    if previous and old in ACTIVE_STATES:
        if old in ("LONG_TRIGGERED", "ADD_ALLOWED", "HOLD_MANAGE") and direction == "long":
            if close < level * (1 - base.INVALIDATION_BUFFER_PCT) and score <= -1:
                return _state("INVALIDATED", "long", anchor, event, score, plan, fast, "Anchor lost with opposing fast flow")
            if event == "retest_hold_long" and score >= 2:
                return _state("ADD_ALLOWED", "long", anchor, event, score, plan, fast, "Retest held and fast flow strengthened")
            return _state("HOLD_MANAGE", "long", anchor, event, score, plan, fast, "Triggered long thesis remains tied to original anchor")
        if old in ("SHORT_TRIGGERED", "ADD_ALLOWED", "HOLD_MANAGE") and direction == "short":
            if close > level * (1 + base.INVALIDATION_BUFFER_PCT) and score >= 1:
                return _state("INVALIDATED", "short", anchor, event, score, plan, fast, "Anchor reclaimed with opposing fast flow")
            if event == "retest_hold_short" and score <= -2:
                return _state("ADD_ALLOWED", "short", anchor, event, score, plan, fast, "Retest held and fast flow strengthened")
            return _state("HOLD_MANAGE", "short", anchor, event, score, plan, fast, "Triggered short thesis remains tied to original anchor")
        if old == "ARMED_LONG":
            if event in ("retest_hold_long", "sweep_reclaim_long") and score >= 1:
                return _state("LONG_TRIGGERED", "long", anchor, event, score, plan, fast, "Armed long received retest/reclaim confirmation")
            if close < level * (1 - base.INVALIDATION_BUFFER_PCT) and score < 0:
                return _state("INVALIDATED", "long", anchor, event, score, plan, fast, "Armed long lost original anchor")
            return _state("ARMED_LONG", "long", anchor, event, score, plan, fast, "Awaiting long trigger at original anchor")
        if old == "ARMED_SHORT":
            if event in ("retest_hold_short", "sweep_reject_short") and score <= -1:
                return _state("SHORT_TRIGGERED", "short", anchor, event, score, plan, fast, "Armed short received retest/rejection confirmation")
            if close > level * (1 + base.INVALIDATION_BUFFER_PCT) and score > 0:
                return _state("INVALIDATED", "short", anchor, event, score, plan, fast, "Armed short lost original anchor")
            return _state("ARMED_SHORT", "short", anchor, event, score, plan, fast, "Awaiting short trigger at original anchor")

    if event in ("sweep_reclaim_long", "retest_hold_long") and score >= 0:
        allowed, reason = _direction_gate(plan, fast, previous, anchor, "long")
        if not allowed:
            return _state("NO_TRADE", None, anchor, event, score, plan, fast, reason)
        return _state("LONG_TRIGGERED", "long", anchor, event, score, plan, fast, "Sweep/retest long at structural zone")
    if event in ("sweep_reject_short", "retest_hold_short") and score <= 0:
        allowed, reason = _direction_gate(plan, fast, previous, anchor, "short")
        if not allowed:
            return _state("NO_TRADE", None, anchor, event, score, plan, fast, reason)
        return _state("SHORT_TRIGGERED", "short", anchor, event, score, plan, fast, "Sweep/retest short at structural zone")
    if event == "cross_up":
        allowed, reason = _direction_gate(plan, fast, previous, anchor, "long")
        if not allowed:
            return _state("NO_TRADE", None, anchor, event, score, plan, fast, reason)
        if gamma == "short_gamma" and score >= 2:
            return _state("LONG_TRIGGERED", "long", anchor, event, score, plan, fast, "Plus30 short-gamma breakout probe")
        if score >= 1:
            return _state("ARMED_LONG", "long", anchor, event, score, plan, fast, "Breakout seen; waiting for retest")
        return _state("EARLY_SETUP", "long", anchor, event, score, plan, fast, "Breakout lacks fast-flow confirmation")
    if event == "cross_down":
        allowed, reason = _direction_gate(plan, fast, previous, anchor, "short")
        if not allowed:
            return _state("NO_TRADE", None, anchor, event, score, plan, fast, reason)
        if gamma == "short_gamma" and score <= -2:
            return _state("SHORT_TRIGGERED", "short", anchor, event, score, plan, fast, "Plus30 short-gamma breakdown probe")
        if score <= -1:
            return _state("ARMED_SHORT", "short", anchor, event, score, plan, fast, "Breakdown seen; waiting for retest")
        return _state("EARLY_SETUP", "short", anchor, event, score, plan, fast, "Breakdown lacks fast-flow confirmation")

    proximity = abs(spot - level) / spot
    strategic = _strategic_direction(plan)
    long_min = 1 if strategic == "long" else 2
    short_max = -1 if strategic == "short" else -2
    if proximity <= EARLY_PROXIMITY_PCT and score >= long_min:
        allowed, _ = _direction_gate(plan, fast, previous, anchor, "long")
        if allowed:
            return _state("EARLY_SETUP", "long", anchor, event, score, plan, fast, "Positive fast flow near structural zone under plus30 profile")
    if proximity <= EARLY_PROXIMITY_PCT and score <= short_max:
        allowed, _ = _direction_gate(plan, fast, previous, anchor, "short")
        if allowed:
            return _state("EARLY_SETUP", "short", anchor, event, score, plan, fast, "Negative fast flow near structural zone under plus30 profile")
    return _state("NO_TRADE", None, anchor, event, score, plan, fast, "No executable trigger")


def _generic_premium(cb, bn, fx, asset, symbol, max_skew=60):
    product = f"{asset}-USD"

    def calc(items, converted):
        src = (
            f"Coinbase {product} minus Binance {symbol} converted with Coinbase USDT-USD"
            if converted else f"Coinbase {product} minus Binance {symbol} (unadjusted)"
        )
        if any(m.get("status") != "ok" or m.get("value") is None for m in items):
            return {k: missing(src, "Fresh inputs unavailable", unit=u) for k, u in (("usd", "USD"), ("bps", "bp"), ("pct", "%"))}
        ts = [epoch(m["timestamp"]) for m in items]
        if max(ts) - min(ts) > max_skew:
            return {k: missing(src, "Input timestamps differ by more than 60s", status="stale", unit=u) for k, u in (("usd", "USD"), ("bps", "bp"), ("pct", "%"))}
        converted_bn = number(bn["value"], 1e-12) * (number(fx["value"], 1e-12) if converted else 1.0)
        diff = number(cb["value"], 1e-12) - converted_bn
        return {
            "usd": metric(diff, src, min(ts), "USD", assumption=None if converted else "1 USDT = 1 USD"),
            "bps": metric(diff / converted_bn * 10000, src, min(ts), "bp"),
            "pct": metric(diff / converted_bn * 100, src, min(ts), "%"),
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


def _fetch_binance_minutes(client, end, symbol, minutes=70):
    rows, _, _ = client.get(
        BINANCE, "/api/v3/klines", symbol=symbol, interval="1m",
        startTime=(end - minutes * 60) * 1000, endTime=end * 1000 - 1, limit=1000,
    )
    return rows


def collect_fast(asset="BTC", client=None):
    asset = str(asset).upper()
    if asset not in ASSETS:
        raise ValueError(f"Unsupported execution asset: {asset}")
    if asset == "BTC":
        fast = base.collect_fast(client)
        fast["schema_version"] = SCHEMA_VERSION
        fast["asset"] = "BTC"
        return fast

    cfg = asset_config(asset)
    symbol = cfg["binance_symbol"]
    product = cfg["coinbase_product"]
    started = time.time()
    end = int(started) // 60 * 60
    client = client or Client(timeout=20, attempts=2)
    tasks = {
        "binance_spot": lambda: asset_binance_spot(client, symbol, asset),
        "coinbase_spot": lambda: asset_coinbase_spot(client, product, asset),
        "usdt_usd": lambda: asset_coinbase_spot(client, "USDT-USD", "USDT"),
        "deribit": lambda: asset_deribit_perpetual(client, asset),
        "minutes": lambda: _fetch_binance_minutes(client, end, symbol),
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
            premiums = _generic_premium(
                results["coinbase_spot"], results["binance_spot"], results["usdt_usd"], asset, symbol
            )
        except Exception as exc:
            errors["premium"] = str(exc)

    cvd, candles_5m, candles_15m = {}, [], []
    if "minutes" in results:
        try:
            all_cvd = _generic_binance_cvd(results["minutes"], end, symbol, asset)
            cvd = {name: all_cvd[name] for name in ("15m", "1h")}
            candles_5m = base.aggregate_5m(results["minutes"])[-12:]
            candles_15m = base.aggregate_15m(results["minutes"])[-4:]
        except Exception as exc:
            errors["binance_fast_flow"] = str(exc)

    now = time.time()
    return {
        "schema_version": SCHEMA_VERSION,
        "asset": asset,
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
        "candles_5m": candles_5m,
        "candles_15m": candles_15m,
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
        "asset": state.get("asset") or "",
        "state": state.get("state") or "",
        "direction": state.get("direction") or "",
        "strategic_bias": state.get("strategic_bias") or "",
        "strategic_bias_score": state.get("strategic_bias_score", ""),
        "bias_alignment": state.get("bias_alignment") or "",
        "core_swing_status": (state.get("core_swing") or {}).get("status", ""),
        "core_swing_direction": (state.get("core_swing") or {}).get("direction", ""),
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
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(row), lineterminator="\n")
    writer.writeheader()
    for existing in rows:
        writer.writerow({key: existing.get(key, "") for key in row})
    atomic_write(path, buffer.getvalue())


def default_root(asset):
    asset = str(asset).upper()
    return Path(".") if asset == "BTC" else Path("assets") / asset.lower()


def run(asset="BTC", root=None, client=None):
    asset = str(asset).upper()
    if asset not in ASSETS:
        raise ValueError(f"Unsupported execution asset: {asset}")
    root = Path(root) if root is not None else default_root(asset)
    context = read_json(root / "latest.json", {})
    if not context:
        raise ValueError(f"{root / 'latest.json'} is required before the execution watcher can run")
    context_asset = str(context.get("asset") or asset).upper()
    if asset != "BTC" and context_asset != asset:
        raise ValueError(f"Asset context mismatch: requested {asset}, found {context_asset}")

    old_plan = read_json(root / "execution_plan.json", {})
    plan = build_plan(context, asset)
    if (
        old_plan.get("context_timestamp") != plan.get("context_timestamp")
        or old_plan.get("schema_version") != SCHEMA_VERSION
        or old_plan.get("profile") != PROFILE
    ):
        write_json(root / "execution_plan.json", plan)
    else:
        plan = old_plan

    fast = collect_fast(asset, client)
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
    logging.info(
        "%s execution watcher: %s | bias %s %.2f | core %s | %s | flow %+d | %s",
        asset, fast["status"], state.get("strategic_bias", "neutral"),
        state.get("strategic_bias_score", 0.0),
        (state.get("core_swing") or {}).get("status", "NONE"),
        state.get("state", "NO_TRADE"), flow_score(fast), fast["timestamp"],
    )
    return plan, fast, state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", choices=ASSETS, default="BTC")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime
    run(args.asset, args.output_dir)


if __name__ == "__main__":
    main()
