import unittest
from btc_collector.core import metric,missing
from btc_collector.options import aggregate,surface

def row(k,side,oi,delta=.25,iv=40,gex=100):
    return {'strike':k,'option_type':side,'instrument_name':str(k)+side,
            'open_interest':metric(oi,'test',1000),'gross_gex_proxy':metric(gex,'test',1000),
            'mark_iv':metric(iv,'test',1000),'delta':metric(delta,'test',1000),
            'underlying_price':metric(100,'test',1000)}

class OptionTests(unittest.TestCase):
    def test_walls_are_oi_maxima(self):
        a=aggregate([row(90,'put',20),row(95,'put',30),row(110,'call',40),row(120,'call',10)])
        self.assertEqual(a['put_wall']['value'],95); self.assertEqual(a['call_wall']['value'],110)
        self.assertEqual(a['gross_gex_proxy']['value'],400)
        self.assertAlmostEqual(sum(r['share_pct']['value'] for r in a['gamma_concentrations']['value']),100)
    def test_missing_oi_invalidates_walls_and_gross_total(self):
        a=row(100,'call',10); a['open_interest']=missing('test','outage'); a['gross_gex_proxy']=missing('test','outage')
        result=aggregate([a,row(90,'put',20)])
        self.assertIsNone(result['put_wall']['value'])
        self.assertIsNone(result['gross_gex_proxy']['value'])
    def test_missing_zero_oi_gamma_does_not_poison_total(self):
        a=row(100,'call',0); a['gross_gex_proxy']=missing('test','no gamma')
        self.assertEqual(aggregate([a,row(90,'put',10)])['gross_gex_proxy']['value'],100)
    def test_skew_sign_and_atm(self):
        a=surface([row(100,'call',10,.25,40),row(100,'put',10,-.25,50)])
        self.assertEqual(a['atm_iv']['value'],45)
        self.assertEqual(a['risk_reversal_25d']['value'],-10)
        self.assertEqual(a['put_minus_call_skew_25d']['value'],10)
