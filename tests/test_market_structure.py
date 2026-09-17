import unittest

from btc_collector.market_structure import (aggregate_trade_bins,collect_market_structure,
    contiguous_value_area,exact_vwap,rolling_profile)

END=1_800_000_000//60*60

def candle(t,base=2,quote=200,close=100):
    return [t*1000,'100','101','99',str(close),str(base),(t+60)*1000-1,str(quote),1,'1','100',0]

class MarketStructureTests(unittest.TestCase):
    def test_exact_quote_base_vwap(self):
        rows=[candle(END-120,2,180),candle(END-60,3,330)]
        value,base,quote=exact_vwap(rows,END-120,END)
        self.assertEqual((value,base,quote),(102,5,510))

    def test_missing_candle_coverage(self):
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            exact_vwap([candle(END-60)],END-120,END)

    def test_zero_volume(self):
        with self.assertRaisesRegex(ValueError,'Zero base'):
            exact_vwap([candle(END-60,0,0)],END-60,END)

    def test_trade_to_bin_aggregation(self):
        bins=aggregate_trade_bins([{'price':101,'quantity':2},{'price':149,'quantity':3},
                                   {'price':151,'quantity':4}],50)
        self.assertEqual(bins,{2:5,3:4})

    def test_poc_tie_is_lower_price(self):
        profile=contiguous_value_area({2:10,3:10},50,.5)
        self.assertEqual(profile['poc'],125)

    def test_value_area_is_contiguous_and_expands_larger_side(self):
        profile=contiguous_value_area({0:5,1:20,2:40,3:30,4:1},10,.70)
        self.assertEqual(profile['poc'],25)
        self.assertEqual(profile['val'],20)
        self.assertEqual(profile['vah'],40)
        self.assertGreaterEqual(profile['value_area_volume_pct'],70)

    def test_rolling_window_evicts_old_minutes_logically(self):
        minutes={str(END-180):{'1':100},str(END-60):{'2':10}}
        profile=rolling_profile(minutes,END-120,END,50)
        self.assertEqual(profile['poc'],125)

    def test_persisted_state_recovery(self):
        class Fake:
            def get(self,base,path,**params):
                if path=='/api/v3/aggTrades':
                    return [{'a':10,'p':'100','q':'1','T':(END-1)*1000}],{},END
                if path=='/api/v3/klines':
                    start=params['startTime']//1000; stop=min(END,start+60_000)
                    return [candle(t) for t in range(start,stop,60)],{},END
                raise ValueError(path)
        minutes={str(t):{'2':1} for t in range(END-7*86400,END,60)}
        state={'schema_version':1,'profile_bin_width':50.0,'last_agg_id':10,
               'last_trade_timestamp':END-1,'coverage_started_at':END-7*86400,'minutes':minutes}
        result,new_state=collect_market_structure(Fake(),END,state,allow_archive=False)
        self.assertEqual(result['rolling_7d']['status'],'ok')
        self.assertEqual(result['rolling_7d']['poc']['value'],125)
        self.assertEqual(new_state['last_agg_id'],10)

    def test_incomplete_profile_is_explicitly_warming_up(self):
        class Fake:
            def get(self,base,path,**params):
                if path=='/api/v3/aggTrades':
                    return [{'a':1,'p':'100','q':'1','T':(END-1)*1000}],{},END
                if path=='/api/v3/klines':
                    start=params['startTime']//1000; stop=min(END,start+60_000)
                    return [candle(t) for t in range(start,stop,60)],{},END
                raise ValueError(path)
        result,_=collect_market_structure(Fake(),END,{},allow_archive=False)
        self.assertEqual(result['rolling_24h']['status'],'warming_up')
        self.assertIsNone(result['rolling_24h']['poc']['value'])
        self.assertEqual(result['rolling_24h']['poc']['status'],'warming_up')

if __name__=='__main__': unittest.main()
