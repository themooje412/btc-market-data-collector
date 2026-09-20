"""Static configuration for the non-BTC hourly collectors.

BTC remains on the audited production path in ``btc_collector.main``.  These
configs extend the same source hierarchy to ETH, SOL and ZEC without changing
BTC output contracts.
"""

ASSETS = {
    "ETH": {
        "binance_symbol": "ETHUSDT",
        "coinbase_product": "ETH-USD",
        "market_profile_bin_width": 2.0,
    },
    "SOL": {
        "binance_symbol": "SOLUSDT",
        "coinbase_product": "SOL-USD",
        "market_profile_bin_width": 0.10,
    },
    "ZEC": {
        "binance_symbol": "ZECUSDT",
        "coinbase_product": "ZEC-USD",
        "market_profile_bin_width": 0.05,
    },
}


def asset_config(asset):
    key = str(asset).upper()
    if key not in ASSETS:
        raise ValueError(f"Unsupported multi-asset collector symbol: {asset}")
    return {"asset": key, **ASSETS[key]}
