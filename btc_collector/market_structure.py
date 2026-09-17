"""Exact Binance spot VWAP and persisted trade-level volume profiles."""
import csv
from datetime import datetime, timezone, timedelta
import hashlib
import io
import logging
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

from .core import epoch, iso, metric, missing, number
from .sources import BINANCE

BIN_WIDTH=50.0
VALUE_AREA_TARGET=.70
DAY=86400
PROFILE_SOURCE='Binance BTCUSDT aggregate trades (official API/archive)'
VWAP_SOURCE='Binance BTCUSDT /api/v3/klines exact quote volume / base volume'

def exact_vwap(candles,start,end):
    """Return quote/base VWAP only when every closed minute is present exactly once."""
    expected=set(range(int(start),int(end),60)); found={}
    for row in candles:
        minute=int(row[0])//1000
        if minute in found: raise ValueError('Duplicate kline minute')
        found[minute]=(number(row[5],0),number(row[7],0))
    missing_minutes=expected-set(found)
    if missing_minutes: raise ValueError(f'Incomplete kline coverage: {len(expected)-len(missing_minutes)}/{len(expected)} minutes')
    base=sum(found[t][0] for t in expected); quote=sum(found[t][1] for t in expected)
    if base<=0: raise ValueError('Zero base volume')
    return quote/base,base,quote

def price_bin(price,width=BIN_WIDTH):
    return math.floor(number(price,1e-12)/number(width,1e-12))

def aggregate_trade_bins(trades,width=BIN_WIDTH):
    bins={}
    for trade in trades:
        idx=price_bin(trade['price'],width)
        bins[idx]=bins.get(idx,0.0)+number(trade['quantity'],0)
    return bins

def contiguous_value_area(bins,width=BIN_WIDTH,target=VALUE_AREA_TARGET):
    """POC-centered contiguous 70% value area; ties select the lower price bin."""
    clean={int(k):number(v,0) for k,v in bins.items() if number(v,0)>0}
    if not clean: raise ValueError('No positive profile volume')
    total=sum(clean.values()); poc=min(clean,key=lambda k:(-clean[k],k))
    included={poc}; volume=clean[poc]; low=high=poc
    while volume/total<target:
        lower=clean.get(low-1,0.0); upper=clean.get(high+1,0.0)
        if lower==upper==0:
            remaining=[k for k in clean if k<low or k>high]
            if not remaining: break
            nearest=min(remaining,key=lambda k:(min(abs(k-low),abs(k-high)),k))
            while low>nearest:
                low-=1; included.add(low); volume+=clean.get(low,0.0)
            while high<nearest:
                high+=1; included.add(high); volume+=clean.get(high,0.0)
            continue
        if lower>=upper: low-=1; included.add(low); volume+=lower
        else: high+=1; included.add(high); volume+=upper
    return {'poc':(poc+.5)*width,'val':low*width,'vah':(high+1)*width,
            'value_area_volume_pct':volume/total*100,'total_volume_btc':total,
            'included_bin_count':len(included),'profile_bin_width':width,
            'tie_rule':'Lowest-price POC; on equal adjacent volume expand lower first'}

def rolling_profile(minutes,start,end,width=BIN_WIDTH):
    bins={}
    for minute,minute_bins in minutes.items():
        t=int(minute)
        if start<=t<end:
            for key,value in minute_bins.items():
                idx=int(key); bins[idx]=bins.get(idx,0.0)+number(value,0)
    return contiguous_value_area(bins,width)

def _timestamp_seconds(raw):
    value=number(raw,0)
    if value>=1e15: return value/1_000_000
    if value>=1e12: return value/1000
    return value

def _ingest(state,trade_id,timestamp,price,quantity,width=BIN_WIDTH):
    trade_id=int(trade_id); timestamp=_timestamp_seconds(timestamp)
    if state.get('last_agg_id') is not None and trade_id<=int(state['last_agg_id']): return
    minute=str(int(timestamp)//60*60); idx=str(price_bin(price,width))
    state.setdefault('minutes',{}).setdefault(minute,{})
    state['minutes'][minute][idx]=state['minutes'][minute].get(idx,0.0)+number(quantity,0)
    state['last_agg_id']=trade_id
    state['last_trade_timestamp']=timestamp
    if state.get('coverage_started_at') is None: state['coverage_started_at']=timestamp

def _download(url,path,max_time=180):
    error=''
    for attempt in range(3):
        result=subprocess.run(['curl','--silent','--show-error','--fail','--proto','=https','--connect-timeout','20',
          '--max-time',str(max_time),'--max-filesize','100000000','--user-agent','btc-market-data-collector/1.0',
          '--output',str(path),url],capture_output=True,text=True,timeout=max_time+10)
        if result.returncode==0: return
        error=result.stderr.strip()[:200]
        if attempt<2: time.sleep(2**(attempt+1))
    raise RuntimeError(f'Official Binance archive download failed ({url}): {error}')

def _backfill_archives(state,end,width=BIN_WIDTH,cache_dir=None):
    first=datetime.fromtimestamp(end-7*DAY,timezone.utc).date()
    last=datetime.fromtimestamp(end,timezone.utc).date()-timedelta(days=1)
    day=first; expected_next=None; archives=[]
    required_names={f'BTCUSDT-aggTrades-{(first+timedelta(days=i)).isoformat()}.zip'
                    for i in range((last-first).days+1)}
    cache=Path(cache_dir or os.environ.get('BINANCE_ARCHIVE_CACHE','.cache/binance-aggtrades'))
    cache.mkdir(parents=True,exist_ok=True)
    # A temporary folder is retained only for atomic downloads; verified archives live in cache.
    with tempfile.TemporaryDirectory() as folder:
        while day<=last:
            stamp=day.isoformat(); name=f'BTCUSDT-aggTrades-{stamp}.zip'
            base='https://data.binance.vision/data/spot/daily/aggTrades/BTCUSDT/'
            archive=cache/name; checksum=cache/(name+'.CHECKSUM')
            logging.info('Binance archive bootstrap %s: %s',stamp,'cache check' if archive.exists() else 'download')
            if not archive.exists():
                partial=Path(folder)/(name+'.partial'); _download(base+name,partial); partial.replace(archive)
            if not checksum.exists():
                time.sleep(.5); partial_sum=Path(folder)/(name+'.CHECKSUM.partial')
                _download(base+name+'.CHECKSUM',partial_sum,60); partial_sum.replace(checksum)
            wanted=checksum.read_text(encoding='utf-8').split()[0].lower()
            actual=hashlib.sha256(archive.read_bytes()).hexdigest()
            if actual!=wanted: raise ValueError('Binance archive checksum mismatch for '+stamp)
            with zipfile.ZipFile(archive) as z:
                members=[m for m in z.namelist() if m.endswith('.csv')]
                if len(members)!=1: raise ValueError('Unexpected Binance archive members')
                first_id=last_id=None; count=0
                with z.open(members[0]) as raw:
                    text=io.TextIOWrapper(raw,encoding='utf-8',newline='')
                    for row in csv.reader(text):
                        if len(row)<6: raise ValueError('Malformed Binance aggTrades archive row')
                        agg_id=int(row[0]); first_id=agg_id if first_id is None else first_id; last_id=agg_id
                        if expected_next is not None and count==0 and agg_id!=expected_next:
                            raise ValueError(f'Archive aggregate-trade ID gap before {stamp}')
                        _ingest(state,agg_id,row[5],row[1],row[2],width); count+=1
            expected_next=(last_id+1) if last_id is not None else expected_next
            archives.append({'date':stamp,'rows':count,'first_agg_id':first_id,'last_agg_id':last_id,
                             'sha256_verified':True})
            logging.info('Binance archive bootstrap %s: verified and ingested %d aggregate trades',stamp,count)
            day+=timedelta(days=1)
    # The workflow cache is rolling state, not an ever-growing archive mirror.
    for old in cache.glob('BTCUSDT-aggTrades-*.zip*'):
        stem=old.name.removesuffix('.CHECKSUM')
        if stem not in required_names: old.unlink()
    state['archive_backfill']=archives

def _fetch_incremental(client,state,end,width=BIN_WIDTH,max_pages=2000):
    latest,_,received=client.get(BINANCE,'/api/v3/aggTrades',symbol='BTCUSDT',limit=1)
    if not isinstance(latest,list) or not latest: raise ValueError('Malformed latest aggregate-trade response')
    latest_id=int(latest[-1]['a']); pages=1
    if state.get('last_agg_id') is None:
        _ingest(state,latest[-1]['a'],latest[-1]['T'],latest[-1]['p'],latest[-1]['q'],width)
    start=int(state['last_agg_id'])+1
    page_starts=list(range(start,latest_id+1,1000))
    if len(page_starts)+pages>max_pages: raise ValueError('Aggregate-trade pagination budget exhausted')
    previous=int(state['last_agg_id'])
    # Sixteen concurrent official pages is deliberately bounded: it keeps the
    # one-time current-day bootstrap practical without overwhelming the public
    # endpoint (or the runner's HTTPS connection pool).
    batch_size=16
    for offset in range(0,len(page_starts),batch_size):
        batch=page_starts[offset:offset+batch_size]
        def fetch(from_id):
            data,_,recv=client.get(BINANCE,'/api/v3/aggTrades',symbol='BTCUSDT',fromId=from_id,limit=1000)
            return from_id,data,recv
        with ThreadPoolExecutor(max_workers=batch_size) as pool: results=list(pool.map(fetch,batch))
        for requested,data,recv in results:
            pages+=1; received=max(received,recv)
            if not isinstance(data,list) or not data: raise ValueError(f'Empty aggregate-trade page at {requested}')
            if int(data[0]['a'])!=previous+1 or int(data[0]['a'])!=requested:
                raise ValueError(f'Aggregate-trade ID gap before {requested}')
            for row in data:
                agg_id=int(row['a'])
                if agg_id!=previous+1: raise ValueError(f'Aggregate-trade ID gap at {agg_id}')
                _ingest(state,agg_id,row['T'],row['p'],row['q'],width); previous=agg_id
            if previous>=latest_id: break
        logging.info('Binance aggregate-trade API catch-up: %d/%d pages complete',
                     min(offset+len(batch),len(page_starts)),len(page_starts))
        if previous>=latest_id: break
    if previous<latest_id: raise ValueError('Aggregate-trade catch-up did not reach captured latest ID')
    state['synced_through']=min(number(state.get('last_trade_timestamp'),0),received)
    return pages

def _klines(client,end):
    rows=[]; cursor=end-7*DAY
    while cursor<end:
        data,_,_=client.get(BINANCE,'/api/v3/klines',symbol='BTCUSDT',interval='1m',startTime=cursor*1000,
                            endTime=end*1000-1,limit=1000)
        if not data: break
        rows.extend(data); following=int(data[-1][0])//1000+60
        if following<=cursor: raise ValueError('Kline pagination did not advance')
        cursor=following
    return rows

def _window_output(name,start,end,candles,state,spot,width=BIN_WIDTH):
    try:
        vwap,base,quote=exact_vwap(candles,start,end)
        vwap_metric=metric(vwap,VWAP_SOURCE,end,'USDT',base_volume_btc=base,quote_volume_usdt=quote,
                           window_start=iso(start),window_end=iso(end))
    except ValueError as e: vwap_metric=missing(VWAP_SOURCE,str(e),status='warming_up',unit='USDT')
    coverage_start=state.get('coverage_started_at'); synced=state.get('synced_through')
    complete=coverage_start is not None and coverage_start<=start and synced is not None and synced>=end-60
    if complete:
        try: profile=rolling_profile(state.get('minutes',{}),start,end,width)
        except ValueError as e: complete=False; reason=str(e)
    else: reason='Exact aggregate-trade profile is warming up to full window coverage'
    out={'status':'ok' if complete and vwap_metric['status']=='ok' else 'warming_up','window_start':iso(start),'window_end':iso(end),
         'vwap':vwap_metric,'profile_bin_width':width,'profile_method':'Exact aggregate-trade quantity binned by price',
         'profile_coverage_started_at':iso(coverage_start) if coverage_start is not None else None,
         'profile_synced_through':iso(synced) if synced is not None else None}
    if complete:
        for key,unit in (('poc','USDT'),('vah','USDT'),('val','USDT'),('value_area_volume_pct','%')):
            out[key]=metric(profile[key],PROFILE_SOURCE,end,unit,definition='Contiguous POC-centered 70% value area')
        location='above_value' if spot>profile['vah'] else ('below_value' if spot<profile['val'] else 'inside_value')
        out['spot_location']=metric(location,PROFILE_SOURCE,end)
        for key,value in (('vwap',vwap_metric.get('value')),('poc',profile['poc']),('vah',profile['vah']),('val',profile['val'])):
            out['distance_to_'+key+'_pct']=metric((spot/value-1)*100,PROFILE_SOURCE,end,'%',spot_reference=spot) if value else missing(PROFILE_SOURCE,'Reference unavailable',unit='%')
        out['profile_diagnostics']={k:profile[k] for k in ('total_volume_btc','included_bin_count','tie_rule')}
    else:
        for key,unit in (('poc','USDT'),('vah','USDT'),('val','USDT'),('value_area_volume_pct','%'),('spot_location',None),
                         ('distance_to_vwap_pct','%'),('distance_to_poc_pct','%'),('distance_to_vah_pct','%'),('distance_to_val_pct','%')):
            out[key]=missing(PROFILE_SOURCE,reason,status='warming_up',unit=unit)
    return out

def collect_market_structure(client,end,state=None,allow_archive=True,archive_cache=None):
    state=dict(state or {}); state.setdefault('schema_version',1); state.setdefault('profile_bin_width',BIN_WIDTH)
    if state['profile_bin_width']!=BIN_WIDTH: raise ValueError('Persisted profile bin width mismatch')
    state['minutes']={str(k):dict(v) for k,v in state.get('minutes',{}).items()}
    if state.get('last_agg_id') is None and allow_archive: _backfill_archives(state,end,BIN_WIDTH,archive_cache)
    pages=_fetch_incremental(client,state,end,BIN_WIDTH)
    cutoff=end-8*DAY; state['minutes']={k:v for k,v in state['minutes'].items() if int(k)>=cutoff}
    candles=_klines(client,end)
    if not candles: raise ValueError('No Binance klines for market structure')
    spot=number(candles[-1][4],1e-12); session=end//DAY*DAY
    windows={'utc_session':session,'rolling_24h':end-DAY,'rolling_7d':end-7*DAY}
    result={name:_window_output(name,start,end,candles,state,spot,BIN_WIDTH) for name,start in windows.items()}
    result.update(status='ok' if all(v['status']=='ok' for v in result.values() if isinstance(v,dict)) else 'warming_up',
                  source=PROFILE_SOURCE,spot_reference=metric(spot,VWAP_SOURCE,end,'USDT'),
                  profile_bin_width=BIN_WIDTH,value_area_target_pct=VALUE_AREA_TARGET*100,
                  incremental_pages=pages,state_method='Persisted per-minute exact aggregate-trade price bins')
    return result,state
