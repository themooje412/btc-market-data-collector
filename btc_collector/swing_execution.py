"""Swing-aware production wrapper for the multi-asset execution watcher.

The 5m watcher is an entry-timing layer for a 2-5 day (or longer) core swing.
It must not turn a tiny 5m candle invalidation into a "hard stop" or treat the
nearest local level as a full take-profit target.

This module deliberately leaves the fast signal engine intact, but replaces
trade geometry and active-trade invalidation semantics before delegating to the
shared BTC/ETH/SOL/ZEC runner.
"""

import argparse
import logging
import time
from copy import deepcopy
from pathlib import Path

from . import execution_watcher as base
from . import multi_asset_execution as multi

SCHEMA_VERSION = "1.3.0"
PROFILE = "controlled_aggressive_swing_plus30_v2"

# Minimum core stop distance. These are risk-geometry floors, not position-risk
# increases: position size must shrink so total R remains bounded.
MIN_CORE_STOP_PCT = {
    "BTC": 0.0065,  # 0.65%
    "ETH": 0.0085,  # 0.85%
    "SOL": 0.0100,  # 1.00%
    "ZEC": 0.0125,  # 1.25%
}
MAX_CORE_STOP_PCT = {
    "BTC": 0.0180,
    "ETH": 0.0220,
    "SOL": 0.0260,
    "ZEC": 0.0320,
}
VOL_RANGE_MULTIPLIER = 0.75
STRUCTURE_BUFFER_PCT = 0.0015
OPEN_PATH_RR = 2.0
SECOND_OPEN_PATH_RR = 3.0

_ORIGINAL_BUILD_PLAN = multi.build_plan
_ORIGINAL_CLASSIFY_STATE = multi.classify_state


def _metric_number(root, *path):
    return base._numeric_metric(root, *path)


def _asset(plan, fast=None):
    return str((plan or {}).get("asset") or (fast or {}).get("asset") or "BTC").upper()


def _recent_range_pct(fast, spot):
    candles = (fast or {}).get("candles_5m") or []
    rows = candles[-12:]
    if not rows or not spot:
        return 0.0
    high = max(float(row["high"]) for row in rows)
    low = min(float(row["low"]) for row in rows)
    return max(0.0, (high - low) / float(spot))


def _stop_distance_pct(asset, fast, spot):
    floor = MIN_CORE_STOP_PCT.get(asset, MIN_CORE_STOP_PCT["ETH"])
    cap = MAX_CORE_STOP_PCT.get(asset, MAX_CORE_STOP_PCT["ETH"])
    adaptive = _recent_range_pct(fast, spot) * VOL_RANGE_MULTIPLIER
    return min(cap, max(floor, adaptive))


def _level_values(plan):
    values = []
    for cluster in (plan or {}).get("levels") or []:
        try:
            value = float(cluster["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return sorted(set(values))


def swing_trade_geometry(plan, fast, anchor, direction):
    """Build 2-5d swing geometry while preserving 5m entry timing.

    Key rules:
    - the hard stop cannot collapse to a 0.1%-style scalp stop;
    - local levels are checkpoints, not automatic TP1;
    - R/R gating uses the first genuine swing target (or a clearly labelled
      open-path R projection when no higher/lower structural level qualifies).
    """
    spot = _metric_number(fast, "spot", "binance")
    candles = (fast or {}).get("candles_5m") or []
    if spot is None or not anchor or len(candles) < 2 or direction not in ("long", "short"):
        return None

    asset = _asset(plan, fast)
    level = float(anchor["value"])
    stop_pct = _stop_distance_pct(asset, fast, spot)
    cap_pct = MAX_CORE_STOP_PCT.get(asset, MAX_CORE_STOP_PCT["ETH"])
    levels = _level_values(plan)

    if direction == "long":
        floor_stop = spot * (1.0 - stop_pct)
        anchor_stop = level * (1.0 - base.INVALIDATION_BUFFER_PCT)
        stop = min(floor_stop, anchor_stop)
        risk_side = [v for v in levels if v < level * (1.0 - 0.0005)]
        if risk_side:
            structural = max(risk_side)
            structural_stop = structural * (1.0 - STRUCTURE_BUFFER_PCT)
            if 0 < (spot - structural_stop) / spot <= cap_pct:
                stop = min(stop, structural_stop)
        checkpoints = [v for v in levels if v > spot * 1.0005]
    else:
        floor_stop = spot * (1.0 + stop_pct)
        anchor_stop = level * (1.0 + base.INVALIDATION_BUFFER_PCT)
        stop = max(floor_stop, anchor_stop)
        risk_side = [v for v in levels if v > level * (1.0 + 0.0005)]
        if risk_side:
            structural = min(risk_side)
            structural_stop = structural * (1.0 + STRUCTURE_BUFFER_PCT)
            if 0 < (structural_stop - spot) / spot <= cap_pct:
                stop = max(stop, structural_stop)
        checkpoints = sorted((v for v in levels if v < spot * 0.9995), reverse=True)

    risk = (spot - stop) if direction == "long" else (stop - spot)
    if risk <= 0:
        return None

    checkpoint1 = checkpoints[0] if checkpoints else None
    checkpoint2 = checkpoints[1] if len(checkpoints) > 1 else None

    min_rr = float((plan.get("rules") or {}).get("normal_min_rr") or multi.NORMAL_MIN_RR)
    swing_target = None
    swing_target_type = None
    for candidate in checkpoints:
        reward = (candidate - spot) if direction == "long" else (spot - candidate)
        if reward > 0 and reward / risk >= min_rr:
            swing_target = candidate
            swing_target_type = "structural"
            break

    if swing_target is None:
        swing_target = spot + OPEN_PATH_RR * risk if direction == "long" else spot - OPEN_PATH_RR * risk
        swing_target_type = "open_path_2r_projection"

    later_structural = []
    if direction == "long":
        later_structural = [v for v in checkpoints if v > swing_target * 1.0005]
        target2 = later_structural[0] if later_structural else spot + SECOND_OPEN_PATH_RR * risk
    else:
        later_structural = [v for v in checkpoints if v < swing_target * 0.9995]
        target2 = later_structural[0] if later_structural else spot - SECOND_OPEN_PATH_RR * risk

    reward = (swing_target - spot) if direction == "long" else (spot - swing_target)
    rr = reward / risk if reward > 0 else None

    return {
        "entry_reference": round(spot, 8),
        "hard_stop": round(stop, 8),
        "hard_stop_type": "core_swing_structure_volatility",
        "stop_basis": {
            "asset_floor_pct": round(MIN_CORE_STOP_PCT.get(asset, MIN_CORE_STOP_PCT["ETH"]) * 100, 4),
            "adaptive_stop_pct": round(stop_pct * 100, 4),
            "recent_1h_range_pct": round(_recent_range_pct(fast, spot) * 100, 4),
            "note": "Position size must adapt to this stop; total trade risk cap does not increase.",
        },
        "checkpoint1": round(checkpoint1, 8) if checkpoint1 is not None else None,
        "checkpoint2": round(checkpoint2, 8) if checkpoint2 is not None else None,
        "target1": round(swing_target, 8),
        "target1_type": swing_target_type,
        "target2": round(target2, 8),
        "target2_type": "structural" if later_structural else "open_path_3r_projection",
        "risk_usd": round(risk, 8),
        "risk_pct": round(risk / spot * 100, 4),
        "rr_to_target1": round(rr, 3) if rr is not None else None,
        "horizon": "2-5d_or_longer_if_structure_holds",
        "semantics": "5m is entry timing; hard stop and targets are swing geometry, local levels are checkpoints.",
    }


def swing_build_plan(snapshot, asset="BTC"):
    plan = _ORIGINAL_BUILD_PLAN(snapshot, asset)
    asset = str(asset).upper()
    plan["schema_version"] = SCHEMA_VERSION
    plan["profile"] = PROFILE
    plan["rules"].update({
        "minimum_core_stop_pct": MIN_CORE_STOP_PCT.get(asset, MIN_CORE_STOP_PCT["ETH"]) * 100,
        "maximum_core_stop_pct": MAX_CORE_STOP_PCT.get(asset, MAX_CORE_STOP_PCT["ETH"]) * 100,
        "core_stop_recent_range_multiplier": VOL_RANGE_MULTIPLIER,
        "local_level_semantics": "checkpoint_not_automatic_take_profit",
        "active_trade_invalidation": "core_hard_stop_not_5m_anchor_noise",
    })
    plan["semantics"]["swing_geometry"] = (
        "Hard stop is structure/volatility based for a 2-5d core swing; nearest local level is only a checkpoint"
    )
    plan["semantics"]["active_trade_management"] = (
        "Once triggered, a 5m anchor wobble cannot invalidate the trade before the frozen core hard stop is breached"
    )
    return plan


def _preserve_active_trade(previous, fast, reason):
    state = deepcopy(previous)
    state["schema_version"] = SCHEMA_VERSION
    state["state"] = "HOLD_MANAGE"
    state["reason"] = reason
    state["fast_timestamp"] = fast.get("timestamp")
    spot = _metric_number(fast, "spot", "binance")
    state["fast_spot"] = round(spot, 8) if spot is not None else state.get("fast_spot")
    state["flow_score"] = multi.flow_score(fast)
    state["signature"] = "|".join([
        str(state.get("asset") or fast.get("asset") or "BTC"),
        "HOLD_MANAGE",
        str(state.get("direction") or "none"),
        str((state.get("anchor") or {}).get("id") or "none"),
        str(state.get("setup_type") or "none"),
        str(state.get("strategic_bias") or "neutral"),
    ])
    return state


def swing_classify_state(plan, fast, previous=None):
    result = _ORIGINAL_CLASSIFY_STATE(plan, fast, previous)
    if not previous or result.get("state") != "INVALIDATED":
        return result

    old = previous.get("state")
    direction = previous.get("direction")
    if old not in ("LONG_TRIGGERED", "SHORT_TRIGGERED", "ADD_ALLOWED", "HOLD_MANAGE"):
        return result
    if direction not in ("long", "short"):
        return result

    stop = (previous.get("execution") or {}).get("hard_stop")
    try:
        stop = float(stop)
    except (TypeError, ValueError):
        return result

    spot = _metric_number(fast, "spot", "binance")
    if spot is None:
        return result
    breached = spot <= stop if direction == "long" else spot >= stop
    if breached:
        result["reason"] = "Core swing hard stop breached"
        return result

    return _preserve_active_trade(
        previous,
        fast,
        "Local 5m anchor failure ignored; frozen core swing hard stop has not been breached",
    )


def install_policy():
    """Install swing semantics into the existing shared runner."""
    multi.SCHEMA_VERSION = SCHEMA_VERSION
    multi.PROFILE = PROFILE
    multi.build_plan = swing_build_plan
    multi.classify_state = swing_classify_state
    base.trade_geometry = swing_trade_geometry


def run(asset="BTC", root=None, client=None):
    install_policy()
    return multi.run(asset, root, client)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", choices=multi.ASSETS, default="BTC")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime
    run(args.asset, args.output_dir)


if __name__ == "__main__":
    main()
