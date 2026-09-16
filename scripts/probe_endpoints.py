"""Independent, real-network smoke checks. Writes evidence even when APIs fail."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from btc_collector.http import Client
from btc_collector.sources import BINANCE,COINBASE,FUTURES,DERIBIT
from btc_collector.core import iso,number
from btc_collector.storage import write_json

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--output',default='docs/endpoint-smoke.json'); args=parser.parse_args()
    c=Client(); results=[]
    cases=[('Binance spot',BINANCE,'/api/v3/ticker/24hr',{'symbol':'BTCUSDT'},lambda d:number(d['lastPrice'],1e-12)),
      ('Binance CVD',BINANCE,'/api/v3/klines',{'symbol':'BTCUSDT','interval':'1m','limit':2},lambda d:number(d[0][9],0)),
      ('Coinbase BTC price',COINBASE,'/products/BTC-USD/ticker',{},lambda d:number(d['price'],1e-12)),
      ('Coinbase USDT price',COINBASE,'/products/USDT-USD/ticker',{},lambda d:number(d['price'],1e-12)),
      ('Coinbase trades',COINBASE,'/products/BTC-USD/trades',{'limit':5},lambda d:d[0]['side']),
      ('Binance OI',FUTURES,'/fapi/v1/openInterest',{'symbol':'BTCUSDT'},lambda d:number(d['openInterest'],0)),
      ('Binance OI history',FUTURES,'/futures/data/openInterestHist',{'symbol':'BTCUSDT','period':'5m','limit':2},lambda d:number(d[0]['sumOpenInterest'],0)),
      ('Binance mark/funding',FUTURES,'/fapi/v1/premiumIndex',{'symbol':'BTCUSDT'},lambda d:number(d['lastFundingRate'])),
      ('Deribit perpetual',DERIBIT,'/public/ticker',{'instrument_name':'BTC-PERPETUAL'},lambda d:number(d['result']['open_interest'],0)),
      ('Deribit options inventory',DERIBIT,'/public/get_instruments',{'currency':'any','kind':'option','expired':'false'},lambda d:len(d['result'])),
      ('Deribit futures inventory',DERIBIT,'/public/get_instruments',{'currency':'BTC','kind':'future','expired':'false'},lambda d:len(d['result']))]
    def probe(case):
        name,base,path,params,validate=case; row={'name':name,'base':base,'path':path,'parameters':params}
        try:
            d,h,t=c.get(base,path,**params); validate(d)
            row.update(status='ok',received_at=iso(t),response_shape='list' if isinstance(d,list) else 'object')
            if name=='Coinbase trades':
                if not h.get('cb-after'): raise ValueError('Missing cb-after header')
                p,_,_=c.get(base,path,limit=5,after=h['cb-after'])
                if min(int(r['trade_id']) for r in p)>=min(int(r['trade_id']) for r in d): raise ValueError('Pagination did not move backwards')
                row['pagination']='verified'
            if name=='Deribit options inventory':
                btc=[i for i in d['result'] if i.get('base_currency')=='BTC' and i.get('is_active')]
                row['btc_contracts']=len(btc)
                inst=btc[0]['instrument_name']; q,_,_=c.get(base,'/public/ticker',instrument_name=inst)
                for key in ('delta','gamma'): number(q['result']['greeks'][key])
                number(q['result']['mark_iv'],0)
                row['sample_option_ticker']=inst; row['greeks']='verified'
            if name=='Deribit futures inventory':
                dated=[i for i in d['result'] if i.get('is_active') and 'PERPETUAL' not in i['instrument_name']]
                if dated:
                    q,_,_=c.get(base,'/public/ticker',instrument_name=dated[0]['instrument_name'])
                    number(q['result']['mark_price'],1e-12); row['sample_dated_ticker']=dated[0]['instrument_name']
        except Exception as e: row.update(status='error',error=str(e))
        print(name+': '+row['status'],flush=True)
        return row
    with ThreadPoolExecutor(max_workers=4) as ex: results=list(ex.map(probe,cases))
    write_json(args.output,{'tested_at':iso(time.time()),'results':results,'http_audit':c.audit,
        'note':'Actual network responses. Error is not evidence of endpoint deprecation; inspect HTTP/timeout reason.'})
    return 1 if any(r['status']=='error' for r in results) else 0
if __name__=='__main__': sys.exit(main())
