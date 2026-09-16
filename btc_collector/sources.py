import logging
import time
from concurrent.futures import ThreadPoolExecutor
from .core import (WINDOWS, basis, binance_cvd, check_time, coinbase_cvd, epoch,
                   fresh, iso, metric, missing, number)

BINANCE='https://data-api.binance.vision'
FUTURES='https://fapi.binance.com'
COINBASE='https://api.exchange.coinbase.com'
DERIBIT='https://www.deribit.com/api/v2'

def safe(fn, fallback):
    try: return fn()
    except Exception as e:
        logging.warning('%s: %s',getattr(fn,'__name__','source'),e)
        return fallback(str(e))

def binance_spot(client):
    source=BINANCE+'/api/v3/ticker/24hr?symbol=BTCUSDT'
    # closeTime gives an exchange timestamp, unlike /ticker/price.
    d,_,received=client.get(BINANCE,'/api/v3/ticker/24hr',symbol='BTCUSDT')
    if d['symbol']!='BTCUSDT': raise ValueError('Wrong spot symbol')
    return fresh(number(d['lastPrice'],1e-12),source,number(d['closeTime'])/1000,received,'USDT',timestamp_kind='exchange_rolling_ticker_close')

def coinbase_spot(client, product='BTC-USD'):
    source=COINBASE+'/products/'+product+'/ticker'
    d,_,received=client.get(COINBASE,'/products/'+product+'/ticker')
    return fresh(number(d['price'],1e-12),source,epoch(d['time']),received,'USD',timestamp_kind='last_trade')

def get_binance_cvd(client,end):
    rows=[]; start=end-86400
    while start<end:
        data,_,_=client.get(BINANCE,'/api/v3/klines',symbol='BTCUSDT',interval='1m',
                             startTime=start*1000,endTime=end*1000-1,limit=1000)
        if not data: break
        rows.extend(data)
        following=int(data[-1][0])//1000+60
        if following<=start: raise ValueError('Kline pagination did not advance')
        start=following
    return binance_cvd(rows,end)

def get_coinbase_cvd(client,end,state,max_pages=600,budget=480):
    """Backfill once, then replace fully observed minute bins with overlapping trades.

    Cached bins are historical intervals, never substitutes for a failed new fetch.
    Newest page must cross end; earliest page must cross the start of each full bin.
    """
    old=state.get('coinbase_minutes',{})
    buckets={k:v for k,v in old.items() if end-26*3600<=int(k)<end}
    contiguous=end-86400
    # Find latest contiguous cached coverage, then re-fetch 2 minutes of overlap.
    if buckets:
        latest=max(map(int,buckets))+60
        if latest<=end:
            contiguous=max(end-86400,latest-120)
    trades={}; after=None; seen_cursors=set(); oldest=None; newest=None
    started=time.monotonic(); failure=None; pages=0
    for _ in range(max_pages):
        if time.monotonic()-started>budget: break
        try:
            params={'limit':1000}
            if after is not None: params['after']=after
            data,headers,received=client.get(COINBASE,'/products/BTC-USD/trades',**params)
            pages+=1
            if not isinstance(data,list) or not data: raise ValueError('Empty/malformed trade page')
            parsed=[]
            for row in data:
                tid=int(row['trade_id']); t=epoch(row['time']); size=number(row['size'],0); price=number(row['price'],1e-12)
                if row['side'] not in ('buy','sell'): raise ValueError('Unknown maker side')
                parsed.append((tid,t,size,price,row['side']))
            page_old=min(r[1] for r in parsed); page_new=max(r[1] for r in parsed)
            if newest is None:
                newest=page_new
                check_time(newest,received,180)
                if newest<end: raise ValueError('Latest trades do not reach the requested window end')
            if oldest is not None and page_old>=oldest: raise ValueError('Trade pagination did not move backward')
            oldest=page_old
            for row in parsed:
                if row[0] in trades and trades[row[0]]!=row: raise ValueError('Conflicting duplicate trade')
                trades[row[0]]=row
            if oldest<=contiguous: break
            after=headers.get('cb-after')
            if not after or after in seen_cursors: raise ValueError('Missing/repeated Coinbase cb-after cursor')
            seen_cursors.add(after)
        except Exception as e:
            failure=str(e); break
    # Commit only complete minutes. Any page error invalidates THIS run's CVD.
    if failure:
        return coinbase_cvd(buckets,end,False,failure), {'coinbase_minutes':buckets}, {'pages':pages,'error':failure}
    if oldest is None:
        return coinbase_cvd(buckets,end,False,'No trades fetched'), {'coinbase_minutes':buckets}, {'pages':pages}
    first_full=(int(oldest)//60+1)*60
    # When exact minute boundary trade was seen, still exclude that minute conservatively.
    fresh_bins={str(t):[0.0,0.0] for t in range(max(first_full,end-86400),end,60)}
    for _,t,size,price,side in trades.values():
        k=str(int(t)//60*60)
        if k in fresh_bins:
            sign=1 if side=='sell' else -1  # API side = maker side.
            fresh_bins[k][0]+=sign*size; fresh_bins[k][1]+=sign*size*price
    buckets.update(fresh_bins)
    result=coinbase_cvd(buckets,end)
    return result,{'coinbase_minutes':buckets},{'pages':pages,'unique_trades':len(trades),
            'oldest_trade_timestamp':oldest,'newest_trade_timestamp':newest,'max_pages':max_pages}

def binance_futures(client):
    src=FUTURES
    result={}
    def mark():
        d,_,received=client.get(src,'/fapi/v1/premiumIndex',symbol='BTCUSDT')
        if d['symbol']!='BTCUSDT': raise ValueError('Wrong perpetual symbol')
        t=number(d['time'])/1000
        result['mark_price']=fresh(number(d['markPrice'],1e-12),src+'/fapi/v1/premiumIndex',t,received,'USDT')
        result['index_price']=fresh(number(d['indexPrice'],1e-12),src+'/fapi/v1/premiumIndex',t,received,'USDT')
        result['funding']=fresh(number(d['lastFundingRate'],-1,1),src+'/fapi/v1/premiumIndex',t,received,'fraction',
                               kind='latest_reported_rate',next_funding_timestamp=iso(number(d['nextFundingTime'])/1000))
        if result['mark_price']['status']=='ok' and result['index_price']['status']=='ok':
            result['basis']=basis(d['markPrice'],d['indexPrice'],src+'/fapi/v1/premiumIndex',t)
    safe(mark,lambda e: result.update({k:missing(src,e) for k in ('mark_price','index_price','funding')}))
    def oi():
        d,_,received=client.get(src,'/fapi/v1/openInterest',symbol='BTCUSDT')
        if d['symbol']!='BTCUSDT': raise ValueError('Wrong OI symbol')
        result['oi']=fresh(number(d['openInterest'],0),src+'/fapi/v1/openInterest',number(d['time'])/1000,received,'BTC')
    safe(oi,lambda e:result.update(oi=missing(src,e,unit='BTC')))
    points=[]
    def history():
        rows,_,received=client.get(src,'/futures/data/openInterestHist',symbol='BTCUSDT',period='5m',limit=310)
        for d in rows:
            t=number(d['timestamp'])/1000
            if t>received+30: raise ValueError('Future OI history timestamp')
            points.append((t,number(d['sumOpenInterest'],0)))
    safe(history,lambda e:result.update(history_error=e))
    result['historical_points']=points
    result.setdefault('basis',{k:missing(src,'Fresh mark/index unavailable') for k in ('absolute','bps','annualized_pct')})
    return result

def deribit_perpetual(client):
    d,_,received=client.get(DERIBIT,'/public/ticker',instrument_name='BTC-PERPETUAL')
    r=d['result']; t=number(r['timestamp'])/1000; src=DERIBIT+'/public/ticker?instrument_name=BTC-PERPETUAL'
    if r['instrument_name']!='BTC-PERPETUAL': raise ValueError('Wrong Deribit perpetual')
    out={'mark_price':fresh(number(r['mark_price'],1e-12),src,t,received,'USD'),
         'index_price':fresh(number(r['index_price'],1e-12),src,t,received,'USD'),
         'oi':fresh(number(r['open_interest'],0),src,t,received,'USD'),
         'funding':fresh(number(r['funding_8h'],-1,1),src,t,received,'fraction',kind='Deribit funding_8h; not interchangeable with Binance settlement rate')}
    out['basis']=basis(r['mark_price'],r['index_price'],src,t) if out['mark_price']['status']=='ok' else {k:missing(src,'Stale mark') for k in ('absolute','bps','annualized_pct')}
    return out

def deribit_dated_futures(client):
    data,_,received=client.get(DERIBIT,'/public/get_instruments',currency='BTC',kind='future',expired='false')
    instruments=[r for r in data['result'] if r.get('is_active') and r.get('base_currency')=='BTC' and r.get('settlement_currency')=='BTC' and r['expiration_timestamp']/1000>received and r.get('settlement_period')!='perpetual' and 'PERPETUAL' not in r['instrument_name']]
    out=[]
    for inst in sorted(instruments,key=lambda r:r['expiration_timestamp']):
        def one():
            data,_,recv=client.get(DERIBIT,'/public/ticker',instrument_name=inst['instrument_name'])
            r=data['result']; t=number(r['timestamp'])/1000; check_time(t,recv)
            return {'instrument_name':inst['instrument_name'],'expiry':iso(inst['expiration_timestamp']/1000),
                    'basis':basis(r['mark_price'],r['index_price'],DERIBIT+'/public/ticker',t,inst['expiration_timestamp']/1000)}
        out.append(safe(one,lambda e:{'instrument_name':inst['instrument_name'],'basis':missing(DERIBIT,e)}))
    return out
