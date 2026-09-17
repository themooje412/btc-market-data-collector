import csv
import io
import json
import os
from pathlib import Path
from .core import epoch, iso

PATHS = {
 'binance_spot':('spot','binance'), 'coinbase_spot':('spot','coinbase'),
 'usdt_usd':('spot','usdt_usd'), 'premium_usd':('coinbase_premium','usd'),
 'premium_bps':('coinbase_premium','bps'), 'premium_fx_bps':('coinbase_premium','fx_adjusted','bps'),
 'raw_coinbase_premium_bps':('raw_coinbase_premium','bps'),
 'fx_adjusted_coinbase_premium_bps':('fx_adjusted_coinbase_premium','bps'),
 'atm_iv':('atm_iv',), 'skew_25d':('skew_25d',), 'put_wall':('put_wall',), 'call_wall':('call_wall',),
 'option_oi_btc':('options','total_oi'), 'gross_gex_proxy':('options','gross_gex_proxy'),
 'net_gex_estimate':('options','net_gex_estimate_usd_per_1pct'),
 'zero_gamma_flip':('options','zero_gamma_flip'),
 'spot_to_gamma_flip_pct':('options','spot_to_gamma_flip_pct'),
 'gamma_regime':('options','gamma_regime')}
for venue in ('binance','coinbase'):
    for window in ('15m','1h','4h','24h'): PATHS[f'{venue}_cvd_{window}']=('cvd',venue,window)
for venue in ('binance','deribit'):
    PATHS[f'{venue}_oi']=('open_interest',venue,'current')
    PATHS[f'{venue}_mark']=('futures',venue,'mark_price')
    PATHS[f'{venue}_funding']=('funding',venue)
    PATHS[f'{venue}_basis_bps']=('basis',venue,'bps')
    for window in ('1h','4h','24h'): PATHS[f'{venue}_oi_change_{window}']=('open_interest',venue,'changes',window)

def atomic_write(path,text):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with open(tmp,'w',encoding='utf-8',newline='') as f:
        f.write(text); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)

def write_json(path,value):
    atomic_write(path,json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n')

def read_json(path,default):
    p=Path(path)
    if not p.exists(): return default
    # Corrupt state must be reported, not silently accepted.
    return json.loads(p.read_text(encoding='utf-8'))

def read_history(path):
    if not Path(path).exists(): return []
    with open(path,newline='',encoding='utf-8') as f:
        rows=list(csv.DictReader(f))
    if any(not r.get('hour') or not r.get('timestamp') for r in rows): raise ValueError('Malformed history.csv')
    return rows

def history_points(rows,venue):
    out=[]
    for r in rows:
        key=venue+'_oi'
        if r.get(key+'_status')=='ok' and r.get(key):
            out.append((epoch(r[key+'_timestamp']),float(r[key])))
    return out

def snapshot_row(snapshot):
    row={'hour':snapshot['snapshot_hour'],'timestamp':snapshot['timestamp'],'status':snapshot['status']}
    row['option_surface_expiry']=snapshot.get('options',{}).get('headline_surface',{}).get('expiry') or ''
    for name,path in PATHS.items():
        obj=snapshot
        for part in path: obj=obj.get(part,{}) if isinstance(obj,dict) else {}
        row[name]=obj.get('value') if obj.get('value') is not None else ''
        for key in ('status','timestamp','source','unit'): row[name+'_'+key]=obj.get(key) or ''
    return row

def update_history(path,snapshot):
    rows=read_history(path); new=snapshot_row(snapshot)
    # One UTC hour = one record. Same-hour retries replace, never append twice.
    by_hour={r['hour']:r for r in rows}; by_hour[new['hour']]=new
    fields=list(new)
    # Preserve older/additional columns on future schema upgrades.
    fields+=sorted({k for r in rows for k in r}-set(fields))
    buf=io.StringIO(newline=''); w=csv.DictWriter(buf,fieldnames=fields)
    w.writeheader(); w.writerows(by_hour[k] for k in sorted(by_hour))
    atomic_write(path,buf.getvalue())

def validate(snapshot):
    required=('timestamp','data_age','spot','cvd','coinbase_premium','raw_coinbase_premium','fx_adjusted_coinbase_premium','futures','open_interest','funding','basis','options',
              'put_wall','call_wall','atm_iv','skew_25d','gamma_concentrations')
    for key in required:
        if key not in snapshot: raise ValueError('Missing section '+key)
    epoch(snapshot['timestamp'])
    def walk(x):
        if isinstance(x,dict):
            if 'value' in x and 'status' in x:
                if x['status'] in ('error','stale','not_applicable') and x['value'] is not None:
                    raise ValueError('Failed metric must be null')
                if x['status']=='ok' and (x['value'] is None or not x.get('timestamp')):
                    raise ValueError('OK metric requires value and timestamp')
                if x.get('timestamp'): epoch(x['timestamp'])
            for v in x.values(): walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
    walk(snapshot); json.dumps(snapshot,allow_nan=False)
