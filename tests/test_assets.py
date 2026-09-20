import unittest

from btc_collector.asset_config import ASSETS, asset_config
from btc_collector.asset_market_structure import contiguous_value_area
from btc_collector.asset_options import _zero_crossings, unavailable_options


class AssetConfigTests(unittest.TestCase):
    def test_expected_assets_and_unique_symbols(self):
        self.assertEqual(set(ASSETS), {"ETH", "SOL", "ZEC"})
        self.assertEqual(len({cfg["binance_symbol"] for cfg in ASSETS.values()}), 3)
        self.assertEqual(len({cfg["coinbase_product"] for cfg in ASSETS.values()}), 3)
        for asset in ASSETS:
            cfg = asset_config(asset.lower())
            self.assertEqual(cfg["asset"], asset)
            self.assertGreater(cfg["market_profile_bin_width"], 0)

    def test_unknown_asset_rejected(self):
        with self.assertRaises(ValueError):
            asset_config("DOGE")


class AssetMarketStructureTests(unittest.TestCase):
    def test_value_area_uses_asset_specific_width(self):
        result = contiguous_value_area({100: 10, 101: 5, 99: 4}, width=0.1, target=0.70)
        self.assertAlmostEqual(result["poc"], 10.05)
        self.assertAlmostEqual(result["profile_bin_width"], 0.1)
        self.assertGreaterEqual(result["value_area_volume_pct"], 70.0)


class AssetOptionTests(unittest.TestCase):
    def test_no_surface_is_explicitly_not_applicable(self):
        result = unavailable_options("ZEC")
        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["active_contract_count"], 0)
        self.assertEqual(result["gamma_regime"]["status"], "not_applicable")
        self.assertIsNone(result["gamma_regime"]["value"])

    def test_zero_crossing_interpolation(self):
        roots = _zero_crossings([90, 100, 110], [10, -10, -20])
        self.assertEqual(len(roots), 1)
        self.assertAlmostEqual(roots[0]["price"], 95.0)
        self.assertEqual(roots[0]["direction"], "positive_to_negative")


if __name__ == "__main__":
    unittest.main()
