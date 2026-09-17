import json
from pathlib import Path
import tempfile
import time
import unittest
from btc_collector.main import collect,age_metrics
from btc_collector.core import metric,iso
from btc_collector.sources import get_coinbase_cvd
from btc_collector.storage import read_history,update_history,validate

class DeadClient:
    audit=[]
    def get(self,*a,**kw): raise RuntimeError('Synthetic upstream outage')

class IntegrationTests(unittest.TestCase):
    def test_all_sources_down_still_writes_honest_outputs_and_dedupes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); s=collect(root,DeadClient()); validate(s)
            self.assertEqual(s['status'],'error')
            self.assertIsNone(s['spot']['binance']['value']); self.assertIsNone(s['put_wall']['value'])
            self.assertTrue((root/'history.csv').exists())
            update_history(root/'history.csv',s)
            self.assertEqual(len(read_history(root/'history.csv')),1)
            self.assertEqual(json.loads((root/'latest.json').read_text())['status'],'error')
    def test_publication_age_expires_values(self):
        x={'spot':metric(100,'x',100)}; age_metrics(x,1001)
        self.assertIsNone(x['spot']['value']); self.assertEqual(x['spot']['status'],'stale')
    def test_coinbase_maker_side_pagination_overlap_and_full_window(self):
        end=int(time.time())//60*60
        def trade(i,t,side='sell',qty='1'): return dict(trade_id=i,time=iso(t),side=side,size=qty,price='100')
        class Fake:
            def __init__(self): self.n=0
            def get(self,*args,**kw):
                self.n+=1
                if self.n==1: return [trade(4,end+1),trade(3,end-10),trade(2,end-70,'buy','2')],{'cb-after':'2'},end+5
                assert kw['after']=='2'
                return [trade(2,end-70,'buy','2'),trade(1,end-901)],{'cb-after':'1'},end+5
        cvd,state,details=get_coinbase_cvd(Fake(),end,{},max_pages=2)
        self.assertEqual(cvd['15m']['value'],-1)
        self.assertEqual(cvd['15m']['quote_cvd']['value'],-100)
        self.assertIsNone(cvd['1h']['value']); self.assertEqual(details['unique_trades'],4)
    def test_coinbase_missing_cursor_nulls_instead_of_summing_partial_page(self):
        end=int(time.time())//60*60
        class Fake:
            def get(self,*args,**kw):
                return [dict(trade_id=1,time=iso(end+1),side='sell',size='1',price='100')],{},end+5
        result,_,_=get_coinbase_cvd(Fake(),end,{})
        self.assertIsNone(result['15m']['value'])

class HappyClient:
    def __init__(self):
        self.audit=[]; self.now=time.time(); self.end=int(self.now)//60*60
    def get(self,base,path,**kw):
        t=self.now; end=self.end; ms=int(t*1000)
        if path=='/api/v3/ticker/24hr': d={'symbol':'BTCUSDT','lastPrice':'100','closeTime':ms}
        elif path.endswith('/ticker') and 'coinbase' in base:
            d={'price':'1' if 'USDT' in path else '101','time':iso(t)}
        elif path.endswith('/trades'):
            d=[{'trade_id':3,'time':iso(end+1),'side':'sell','size':'1','price':'100'},
               {'trade_id':2,'time':iso(end-10),'side':'sell','size':'2','price':'100'},
               {'trade_id':1,'time':iso(end-86401),'side':'buy','size':'1','price':'100'}]
        elif path=='/api/v3/aggTrades':
            d=[{'a':1,'p':'100','q':'2','T':(end-1)*1000,'m':False,'M':True}]
        elif path=='/api/v3/klines':
            start=kw['startTime']//1000
            d=[[i*1000,'100','100','100','100','10',(i+60)*1000-1,'1000',1,'7','700',0]
                for i in range(start,min(start+60000,end),60)]
        elif path=='/fapi/v1/premiumIndex':
            d={'symbol':'BTCUSDT','time':ms,'markPrice':'101','indexPrice':'100','lastFundingRate':'.0001','nextFundingTime':ms+3600000}
        elif path=='/fapi/v1/openInterest': d={'symbol':'BTCUSDT','time':ms,'openInterest':'110'}
        elif path=='/futures/data/openInterestHist':
            d=[{'timestamp':int((t-h*3600)*1000),'sumOpenInterest':'100'} for h in (1,4,24)]
        elif path=='/public/get_instruments':
            if kw['kind']=='future': d={'result':[]}
            else:
                d={'result':[dict(instrument_name='TEST-'+side,option_type=side,strike=100,
                                 expiration_timestamp=ms+30*86400000,base_currency='BTC',is_active=True,
                                 settlement_currency='BTC',quote_currency='BTC',contract_size=1,instrument_type='reversed')
                             for side in ('call','put')]}
        elif path=='/public/get_book_summary_by_currency':
            rows=[]
            if kw['currency']=='BTC':
                rows=[dict(instrument_name='TEST-'+side,base_currency='BTC',quote_currency='BTC',
                           creation_timestamp=ms,open_interest=10,mark_iv=40 if side=='call' else 50,
                           underlying_price=100,interest_rate=0) for side in ('call','put')]
            d={'result':rows,'usOut':int(t*1_000_000)}
        elif path=='/public/get_index_price':
            d={'result':{'index_price':100,'estimated_delivery_price':100},'usOut':int(t*1_000_000)}
        elif path=='/public/ticker':
            name=kw['instrument_name']
            d={'result':dict(instrument_name=name,timestamp=ms,state='open',mark_price=101,index_price=100,
                            underlying_price=100,open_interest=10,funding_8h=.0001,mark_iv=40 if 'call' in name else 50,
                            interest_rate=0,
                            greeks={'delta':.25 if 'call' in name else -.25,'gamma':.00002})}
        else: raise ValueError(path)
        return d,{'cb-after':'1'},t

class SuccessIntegrationTests(unittest.TestCase):
    def test_successful_run_and_history_baselines(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            s=collect(root,HappyClient()); validate(s)
            self.assertEqual(s['spot']['binance']['value'],100)
            self.assertEqual(s['coinbase_premium']['bps']['value'],100)
            self.assertEqual(s['raw_coinbase_premium']['bps']['value'],100)
            self.assertEqual(s['fx_adjusted_coinbase_premium']['bps']['value'],100)
            self.assertEqual(s['cvd']['coinbase']['24h']['value'],2)
            self.assertEqual(s['cvd']['binance']['24h']['value'],5760)
            self.assertEqual(s['skew_25d']['value'],-10)
            self.assertEqual(s['put_wall']['value'],100)
            self.assertAlmostEqual(s['open_interest']['binance']['changes']['24h']['value'],10)
            # Deribit has no invented cross-venue history on first run.
            self.assertIsNone(s['open_interest']['deribit']['changes']['1h']['value'])
            self.assertNotIn('contracts',s['options'])
            chain=json.loads((root/'options_chain.json').read_text())
            self.assertEqual(len(chain['contracts']),2)
            self.assertGreater(chain['gross_gex_proxy']['value'],0)
            self.assertIn('net_gex_estimate_usd_per_1pct',s['options'])
            self.assertIn('zero_gamma_flip',s['options'])
            self.assertIn('zero_gamma_flip_cumulative_strike',s['options'])
            self.assertIn('market_structure',s)
            history=read_history(root/'history.csv')[0]
            for key in ('net_gex_estimate','zero_gamma_flip','spot_to_gamma_flip_pct','gamma_regime',
                        'raw_coinbase_premium_bps','fx_adjusted_coinbase_premium_bps','vwap_session','poc_24h'):
                self.assertIn(key,history)
            self.assertEqual(len(read_history(root/'history.csv')),1)
