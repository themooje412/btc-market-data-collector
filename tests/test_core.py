import unittest
from btc_collector.core import *

class CoreTests(unittest.TestCase):
    def test_timezone_required(self):
        self.assertEqual(epoch('2026-09-16T11:00:00Z'),epoch('2026-09-16T13:00:00+02:00'))
        with self.assertRaises(ValueError): epoch('2026-09-16T11:00:00')
    def test_nonfinite_and_bool_rejected(self):
        for x in ('NaN','inf',None,True):
            with self.assertRaises(ValueError): number(x)
    def test_stale_and_future_values_are_null(self):
        for t in (1,2000):
            m=fresh(10,'test',t,1000)
            self.assertIsNone(m['value']); self.assertEqual(m['status'],'stale')
    def test_premium_bps_and_fx(self):
        result=premium(metric(101,'cb',1000),metric(100,'bn',1000),metric(.99,'fx',1000))
        self.assertEqual(result['usd']['value'],1)
        self.assertEqual(result['bps']['value'],100)
        self.assertEqual(result['fx_adjusted']['usd']['value'],2)
        self.assertAlmostEqual(result['fx_adjusted']['bps']['value'],2/99*10000)
    def test_premium_time_mismatch(self):
        p=premium(metric(101,'cb',1000),metric(100,'bn',900))
        self.assertIsNone(p['bps']['value']); self.assertEqual(p['bps']['status'],'stale')
    def test_perpetual_basis_not_annualized(self):
        p=basis(101,100,'test',1000)
        self.assertAlmostEqual(p['bps']['value'],100)
        self.assertIsNone(p['annualized_pct']['value'])
        self.assertAlmostEqual(basis(101,100,'test',1000,1000+365.25*86400)['annualized_pct']['value'],1)
    def candle(self,t,total,buy):
        return [t*1000,0,0,0,0,str(total),(t+60)*1000-1,str(total*100),1,str(buy),str(buy*100),0]
    def test_cvd_uses_takers_not_candle_color(self):
        result=binance_cvd([self.candle(t,10,7) for t in range(0,86400,60)],86400)
        self.assertEqual(result['15m']['value'],60)
        self.assertEqual(result['24h']['value'],5760)
        self.assertEqual(result['1h']['quote_cvd']['value'],24000)
    def test_cvd_gap_is_not_zero(self):
        result=binance_cvd([self.candle(t,10,7) for t in range(0,840,60)],900)
        self.assertIsNone(result['15m']['value'])
    def test_cvd_invalid_taker_volume(self):
        with self.assertRaises(ValueError): binance_cvd([self.candle(0,10,11)],60)
    def test_microseconds_not_misread_as_milliseconds(self):
        c=self.candle(960,10,7); c[0]*=1000; c[6]*=1000
        with self.assertRaises(ValueError): binance_cvd([c],1200)
    def test_coinbase_fetch_error_does_not_use_cached_cvd(self):
        bins={str(t):[1,100] for t in range(0,900,60)}
        self.assertEqual(coinbase_cvd(bins,900)['15m']['value'],15)
        self.assertIsNone(coinbase_cvd(bins,900,False)['15m']['value'])
    def test_gross_gamma_units_no_double_contract_multiplier(self):
        self.assertEqual(gross_gamma(.00002,100,100000),200000)
    def test_oi_actual_time_not_row_number(self):
        out=oi_changes(metric(110,'bn',100000),[(96400,100),(85600,80),(13600,50)],'bn')
        self.assertAlmostEqual(out['1h']['value'],10)
        self.assertAlmostEqual(out['4h']['value'],37.5)
        self.assertAlmostEqual(out['24h']['value'],120)
        self.assertIsNone(oi_changes(metric(110,'bn',100000),[(90000,100)],'bn')['1h']['value'])
    def test_25d_interpolation_and_no_extrapolation(self):
        def r(d,iv): return {'delta':metric(d,'x',1),'mark_iv':metric(iv,'x',1),'instrument_name':str(d)}
        v,_=interpolate_delta([r(.2,40),r(.3,50)],.25)
        self.assertAlmostEqual(v,45)
        with self.assertRaises(ValueError): interpolate_delta([r(.3,50),r(.4,60)],.25)
