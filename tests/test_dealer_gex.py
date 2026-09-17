import math
import unittest

from btc_collector.core import black_scholes_gamma, metric, missing, signed_dealer_gamma
from btc_collector.options import dealer_gex_estimate

NOW=1_800_000_000
YEAR=365.25*86400

def option(strike,side,oi,iv=50,years=1,index=100):
    return {'strike':strike,'option_type':side,'expiry':'2028-01-15T08:00:00.000Z',
            'open_interest':metric(oi,'test',NOW-1,'BTC'),
            'mark_iv':metric(iv,'test',NOW-1,'volatility percentage points'),
            'index_price':metric(index,'test',NOW-1,'USD')}

def with_expiry(row,years):
    from btc_collector.core import iso
    row['expiry']=iso(NOW+years*YEAR)
    return row

class DealerGexTests(unittest.TestCase):
    def test_black_scholes_gamma(self):
        expected=1/math.sqrt(2*math.pi)*math.exp(-.5*.25**2)/(100*.5)
        self.assertAlmostEqual(black_scholes_gamma(100,100,.5,1),expected,12)

    def test_call_put_dealer_sign_convention(self):
        self.assertEqual(signed_dealer_gamma(.01,2,100,'call'),-2)
        self.assertEqual(signed_dealer_gamma(.01,2,100,'put'),2)

    def test_contract_and_oi_units_are_not_double_multiplied(self):
        row=with_expiry(option(100,'put',3),1)
        row['contract_size']=10
        result=dealer_gex_estimate([row],NOW)
        gamma=black_scholes_gamma(100,100,.5,1)
        self.assertAlmostEqual(result['net_gex_estimate_usd_per_1pct']['value'],gamma*3*100**2*.01)

    def test_aggregation_across_expiries_and_strike(self):
        rows=[with_expiry(option(90,'put',2),.5),with_expiry(option(90,'put',3),1),
              with_expiry(option(110,'call',1),.75)]
        result=dealer_gex_estimate(rows,NOW)
        expected=(black_scholes_gamma(100,90,.5,.5)*2+
                  black_scholes_gamma(100,90,.5,1)*3-
                  black_scholes_gamma(100,110,.5,.75))*100**2*.01
        self.assertAlmostEqual(result['net_gex_estimate_usd_per_1pct']['value'],expected)
        self.assertEqual(len(result['gex_by_strike']['value']),2)

    def test_zero_gamma_crossing_and_interpolation(self):
        rows=[with_expiry(option(80,'put',10,20),.5),with_expiry(option(120,'call',10,20),.5)]
        coarse=dealer_gex_estimate(rows,NOW,.5,1.5,6)
        fine=dealer_gex_estimate(rows,NOW,.5,1.5,1001)
        self.assertEqual(coarse['zero_gamma_flip']['status'],'ok')
        self.assertAlmostEqual(coarse['zero_gamma_flip']['value'],fine['zero_gamma_flip']['value'],delta=1.0)
        grid=[100*(.5+i*(1.0/5)) for i in range(6)]
        self.assertFalse(any(abs(coarse['zero_gamma_flip']['value']-x)<1e-9 for x in grid))

    def test_no_crossing_is_null(self):
        result=dealer_gex_estimate([with_expiry(option(100,'put',10),1)],NOW)
        self.assertIsNone(result['zero_gamma_flip']['value'])
        self.assertEqual(result['zero_gamma_flip']['status'],'not_applicable')
        self.assertIn('No aggregate',result['zero_gamma_flip']['reason'])

    def test_missing_positive_oi_contract_nulls_estimate(self):
        good=with_expiry(option(100,'put',10),1)
        bad=with_expiry(option(110,'call',5),1)
        bad['mark_iv']=missing('test','outage')
        result=dealer_gex_estimate([good,bad],NOW)
        self.assertEqual(result['status'],'error')
        self.assertIsNone(result['net_gex_estimate_usd_per_1pct']['value'])
        self.assertEqual(result['data_coverage']['covered_positive_oi_contracts'],1)

    def test_missing_zero_oi_iv_is_safe(self):
        good=with_expiry(option(100,'put',10),1)
        zero=with_expiry(option(110,'call',0),1); zero['mark_iv']=missing('test','outage')
        result=dealer_gex_estimate([good,zero],NOW)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['data_coverage']['zero_oi_contracts'],1)

    def test_output_schema_and_regime(self):
        rows=[with_expiry(option(80,'put',10,20),.5),with_expiry(option(120,'call',10,20),.5)]
        result=dealer_gex_estimate(rows,NOW)
        for key in ('net_gex_estimate_usd_per_1pct','gex_by_strike','zero_gamma_flip',
                    'spot_to_gamma_flip_pct','gamma_regime','methodology','calculation_timestamp','data_coverage'):
            self.assertIn(key,result)
        if result['zero_gamma_flip']['status']=='ok':
            expected='long_gamma' if 100>result['zero_gamma_flip']['value'] else 'short_gamma'
            self.assertEqual(result['gamma_regime']['value'],expected)

if __name__=='__main__': unittest.main()
