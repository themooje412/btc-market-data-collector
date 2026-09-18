import unittest
from btc_collector.execution_watcher import cluster_levels, aggregate_5m, build_plan, classify_state


def metric(value, status='ok'):
    return {'value': value, 'status': status, 'timestamp': '2026-09-18T10:00:00.000Z'}


def snapshot():
    return {
        'timestamp': '2026-09-18T10:00:00.000Z',
        'spot': {'binance': metric(100.0)},
        'market_structure': {
            'utc_session': {'vwap': metric(99.0), 'poc': metric(100.0), 'vah': metric(101.0), 'val': metric(98.0)},
            'rolling_24h': {'vwap': metric(98.5), 'poc': metric(99.5), 'vah': metric(100.9), 'val': metric(97.5)},
            'rolling_7d': {'vwap': metric(97.0), 'poc': metric(98.0), 'vah': metric(101.1), 'val': metric(96.0)},
        },
        'options': {
            'zero_gamma_flip_cumulative_strike': metric(100.95),
            'zero_gamma_flip_repriced': metric(90.0),
            'gamma_regime': metric('short_gamma'),
        },
        'call_wall': metric(102.5),
        'put_wall': metric(97.0),
    }


def fast(candles, cvd15=2_000_000, cvd1h=4_000_000, fx=1.0, spot=101.0):
    return {
        'timestamp': '2026-09-18T10:10:00.000Z',
        'spot': {'binance': metric(spot)},
        'binance_cvd': {
            '15m': {'status': 'ok', 'value': 1.0, 'quote_cvd': metric(cvd15)},
            '1h': {'status': 'ok', 'value': 1.0, 'quote_cvd': metric(cvd1h)},
        },
        'premium': {'fx_adjusted': {'bps': metric(fx)}},
        'candles_5m': candles,
    }


class ExecutionWatcherTests(unittest.TestCase):
    def test_cluster_levels_merges_confluence(self):
        clusters = cluster_levels([('a', 100.0), ('b', 100.1), ('c', 102.0)])
        self.assertEqual(clusters[0]['confluence_count'], 2)
        self.assertEqual(set(clusters[0]['labels']), {'a', 'b'})
        self.assertEqual(clusters[1]['confluence_count'], 1)

    def test_build_plan_keeps_hourly_context(self):
        plan = build_plan(snapshot())
        self.assertEqual(plan['profile'], 'controlled_aggressive')
        self.assertEqual(plan['gamma_regime'], 'short_gamma')
        ids = [c['id'] for c in plan['levels']]
        self.assertTrue(any('session_vah' in x for x in ids))

    def test_aggregate_5m_requires_complete_bucket(self):
        rows = []
        base = 0
        for i in range(5):
            t = (base + i * 60) * 1000
            rows.append([t, '100', '101', '99', '100', '10', t + 59999, '1000', 1, '6', '600'])
        candles = aggregate_5m(rows)
        self.assertEqual(len(candles), 1)
        self.assertAlmostEqual(candles[0]['quote_cvd'], 1000.0)

    def test_short_gamma_cross_up_can_trigger_early_probe(self):
        plan = build_plan(snapshot())
        anchor = min(plan['levels'], key=lambda c: abs(c['value'] - 101.0))
        level = anchor['value']
        candles = [
            {'open': level-0.12, 'high': level-0.03, 'low': level-0.15, 'close': level-0.10, 'quote_cvd': 0},
            {'open': level-0.05, 'high': level+0.20, 'low': level-0.05, 'close': level+0.08, 'quote_cvd': 0},
        ]
        state = classify_state(plan, fast(candles, spot=level+0.08), None)
        self.assertEqual(state['event'], 'cross_up')
        self.assertEqual(state['state'], 'LONG_TRIGGERED')
        self.assertEqual(state['direction'], 'long')
        self.assertGreaterEqual(state['execution']['rr_to_target1'], 1.6)

    def test_failed_long_is_invalidated_not_averaged_down(self):
        plan = build_plan(snapshot())
        anchor = min(plan['levels'], key=lambda c: abs(c['value'] - 101.0))
        previous = {
            'state': 'LONG_TRIGGERED', 'direction': 'long', 'anchor': anchor,
            'signature': 'LONG_TRIGGERED|long|' + anchor['id'] + '|breakout'
        }
        level = anchor['value']
        candles = [
            {'open': level+0.2, 'high': level+0.3, 'low': level-0.1, 'close': level+0.1, 'quote_cvd': 0},
            {'open': level+0.1, 'high': level+0.1, 'low': level-1.0, 'close': level-0.5, 'quote_cvd': 0},
        ]
        state = classify_state(plan, fast(candles, cvd15=-2_000_000, cvd1h=-4_000_000, fx=-1.0, spot=level-0.5), previous)
        self.assertEqual(state['state'], 'INVALIDATED')
        self.assertEqual(state['anchor']['id'], anchor['id'])

    def test_no_trade_away_from_structure(self):
        plan = build_plan(snapshot())
        candles = [
            {'open': 110, 'high': 111, 'low': 109, 'close': 110, 'quote_cvd': 0},
            {'open': 110, 'high': 111, 'low': 109, 'close': 110, 'quote_cvd': 0},
        ]
        state = classify_state(plan, fast(candles, spot=110), None)
        self.assertEqual(state['state'], 'NO_TRADE')


if __name__ == '__main__':
    unittest.main()
