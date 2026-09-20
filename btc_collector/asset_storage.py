"""History helpers for ETH/SOL/ZEC snapshots."""

import csv
import io
from pathlib import Path

from .core import epoch
from .storage import atomic_write


PATHS = {
    "binance_spot": ("spot", "binance"),
    "coinbase_spot": ("spot", "coinbase"),
    "premium_bps": ("coinbase_premium", "bps"),
    "premium_fx_bps": ("coinbase_premium", "fx_adjusted", "bps"),
    "binance_oi": ("open_interest", "binance", "current"),
    "deribit_oi": ("open_interest", "deribit", "current"),
    "binance_funding": ("funding", "binance"),
    "deribit_funding": ("funding", "deribit"),
    "binance_basis_bps": ("basis", "binance", "bps"),
    "deribit_basis_bps": ("basis", "deribit", "bps"),
    "vwap_session": ("market_structure", "utc_session", "vwap"),
    "vwap_24h": ("market_structure", "rolling_24h", "vwap"),
    "vwap_7d": ("market_structure", "rolling_7d", "vwap"),
    "poc_session": ("market_structure", "utc_session", "poc"),
    "vah_session": ("market_structure", "utc_session", "vah"),
    "val_session": ("market_structure", "utc_session", "val"),
    "poc_24h": ("market_structure", "rolling_24h", "poc"),
    "vah_24h": ("market_structure", "rolling_24h", "vah"),
    "val_24h": ("market_structure", "rolling_24h", "val"),
    "poc_7d": ("market_structure", "rolling_7d", "poc"),
    "vah_7d": ("market_structure", "rolling_7d", "vah"),
    "val_7d": ("market_structure", "rolling_7d", "val"),
    "option_oi": ("options", "total_oi"),
    "gross_gex_proxy": ("options", "gross_gex_proxy"),
    "net_gex_estimate": ("options", "net_gex_estimate_usd_per_1pct"),
    "zero_gamma_flip_repriced": ("options", "zero_gamma_flip_repriced"),
    "zero_gamma_flip_cumulative": ("options", "zero_gamma_flip_cumulative_strike"),
    "gamma_regime": ("options", "gamma_regime"),
    "atm_iv": ("atm_iv",),
    "skew_25d": ("skew_25d",),
    "put_wall": ("put_wall",),
    "call_wall": ("call_wall",),
}
for venue in ("binance", "coinbase"):
    for window in ("15m", "1h", "4h", "24h"):
        PATHS[f"{venue}_cvd_{window}"] = ("cvd", venue, window)
for venue in ("binance", "deribit"):
    for window in ("1h", "4h", "24h"):
        PATHS[f"{venue}_oi_change_{window}"] = ("open_interest", venue, "changes", window)


def read_history(path):
    if not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if any(not row.get("hour") or not row.get("timestamp") for row in rows):
        raise ValueError("Malformed asset history.csv")
    return rows


def history_points(rows, venue):
    out = []
    key = venue + "_oi"
    for row in rows:
        if row.get(key + "_status") == "ok" and row.get(key):
            out.append((epoch(row[key + "_timestamp"]), float(row[key])))
    return out


def snapshot_row(snapshot):
    row = {
        "asset": snapshot["asset"],
        "hour": snapshot["snapshot_hour"],
        "timestamp": snapshot["timestamp"],
        "status": snapshot["status"],
    }
    row["option_surface_expiry"] = snapshot.get("options", {}).get("headline_surface", {}).get("expiry") or ""
    for name, path in PATHS.items():
        obj = snapshot
        for part in path:
            obj = obj.get(part, {}) if isinstance(obj, dict) else {}
        row[name] = obj.get("value") if isinstance(obj, dict) and obj.get("value") is not None else ""
        for field in ("status", "timestamp", "source", "unit"):
            row[name + "_" + field] = obj.get(field) or "" if isinstance(obj, dict) else ""
    return row


def update_history(path, snapshot):
    rows = read_history(path)
    new = snapshot_row(snapshot)
    by_hour = {row["hour"]: row for row in rows}
    by_hour[new["hour"]] = new
    fields = list(new)
    fields += sorted({key for row in rows for key in row} - set(fields))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(by_hour[key] for key in sorted(by_hour))
    atomic_write(path, buffer.getvalue())
