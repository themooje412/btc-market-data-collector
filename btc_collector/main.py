import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import logging
import os
from pathlib import Path
import time
from .core import WINDOWS, epoch, iso, metric, missing, oi_changes, premium
from .http import Client
from .options import collect_options
from .sources import (BINANCE, COINBASE, DERIBIT, FUTURES, binance_spot, coinbase_spot,
                      get_binance_cvd,get_coinbase_cvd,binance_futures,deribit_perpetual,deribit_dated_futures,safe)
from .storage import history_points,read_history,read_json,update_history,validate,write_json

def failed_futures(src,reason):
    return {**{k:missing(src,reason) for k in ('oi','mark_price','index_price','funding')},
            'basis':{k:missing(src,reason) for k in ('absolute','bps','annualized_pct')}}

def failed_options(reason):
    return {'status':'error','contracts':[],'active_contract_count':None,'by_expiry':{},'reason':reason,
            **{k:missing(DERIBIT,reason) for k in ('total_oi','gross_gex_proxy','put_wall','call_wall','gamma_concentrations',
                                                   'net_gex_estimate_usd_per_1pct','gex_by_strike','zero_gamma_flip',
                                                   'spot_to_gamma_flip_pct','gamma_regime')},
            'dealer_gex_estimate':{'status':'error','reason':reason},
            'headline_surface':{k:missing(DERIBIT,reason) for k in ('atm_iv','risk_reversal_25d')}}

def age_metrics(obj,now,path='',ages=None):
    if ages is None: ages={}
    if isinstance(obj,dict):
        if 'value' in obj and 'status' in obj:
            age=max(0,now-epoch(obj['timestamp'])) if obj.get('timestamp') else None
            obj['data_age_seconds']=round(age,3) if age is not None else None
            # Collection can last minutes: expire inputs conservatively at publication.
            if obj['status']=='ok' and age is not None and age>900:
                obj.update(value=None,status='stale',reason='Older than 15 minutes at publication')
            if not path.startswith('options.contracts') and not path.startswith('options.by_expiry'):
                ages[path]=obj['data_age_seconds']
        for k,v in obj.items():
            if k!='data_age_seconds': age_metrics(v,now,f'{path}.{k}'.strip('.'),ages)
    elif isinstance(obj,list):
        for i,v in enumerate(obj): age_metrics(v,now,f'{path}.{i}',ages)
    return ages

def collect(root,client=None,max_pages=600,option_budget=720):
    started=time.time(); end=int(started)//60*60; client=client or Client()
    errors=[]
    try: state=read_json(root/'state/coinbase.json',{})
    except Exception as e: state={}; errors.append('State reset after read error: '+str(e))
    history=read_history(root/'history.csv')
    fail_cvd=lambda src,e:{w:missing(src,e,unit='BTC') for w in WINDOWS}
    tasks={
      'binance_spot':(lambda:binance_spot(client),lambda e:missing(BINANCE,e,unit='USDT')),
      'coinbase_spot':(lambda:coinbase_spot(client),lambda e:missing(COINBASE,e,unit='USD')),
      'usdt_usd':(lambda:coinbase_spot(client,'USDT-USD'),lambda e:missing(COINBASE,e,unit='USD')),
      'binance_cvd':(lambda:get_binance_cvd(client,end),lambda e:fail_cvd(BINANCE,e)),
      'coinbase_cvd':(lambda:get_coinbase_cvd(client,end,state,max_pages),lambda e:(fail_cvd(COINBASE,e),state,{'error':e})),
      'binance_futures':(lambda:binance_futures(client),lambda e:failed_futures(FUTURES,e)),
      'deribit_futures':(lambda:deribit_perpetual(client),lambda e:failed_futures(DERIBIT,e)),
      'dated_futures':(lambda:deribit_dated_futures(client),lambda e:missing(DERIBIT,e)),
      'options':(lambda:collect_options(client,option_budget),failed_options)}
    with ThreadPoolExecutor(max_workers=9) as pool:
        jobs={name:pool.submit(safe,fn,fallback) for name,(fn,fallback) in tasks.items()}
        results={name:job.result() for name,job in jobs.items()}
    bn,db=results['binance_futures'],results['deribit_futures']; opt=results['options']
    oi={}
    for venue,data in (('binance',bn),('deribit',db)):
        points=history_points(history,venue)+data.pop('historical_points',[])
        oi[venue]={'current':data['oi'],'changes':oi_changes(data['oi'],points,venue+' same-market OI')}
    cb_cvd,new_state,cb_details=results['coinbase_cvd']
    now=time.time()
    premiums=premium(results['coinbase_spot'],results['binance_spot'],results['usdt_usd'])
    snapshot={'schema_version':'1.1.0','timestamp':iso(now),'collection_started_at':iso(started),
      'snapshot_hour':iso(int(started)//3600*3600),'cvd_window_end':iso(end),
      'collection_duration_seconds':round(now-started,3),'status':'ok','data_age':{},
      'spot':{'binance':results['binance_spot'],'coinbase':results['coinbase_spot'],'usdt_usd':results['usdt_usd']},
      'coinbase_premium':premiums,
      'raw_coinbase_premium':premiums['raw_coinbase_premium'],
      'fx_adjusted_coinbase_premium':premiums['fx_adjusted_coinbase_premium'],
      'cvd':{'binance':results['binance_cvd'],'coinbase':cb_cvd,'coinbase_collection':cb_details},
      'futures':{'binance':{k:v for k,v in bn.items() if k not in ('oi','funding','basis')},
                 'deribit':{k:v for k,v in db.items() if k not in ('oi','funding','basis')}},
      'open_interest':oi,'funding':{'binance':bn['funding'],'deribit':db['funding']},
      'basis':{'binance':bn['basis'],'deribit':db['basis'],'dated_futures':results['dated_futures']},
      'options':opt,'put_wall':opt['put_wall'],'call_wall':opt['call_wall'],
      'atm_iv':opt['headline_surface']['atm_iv'],'skew_25d':opt['headline_surface']['risk_reversal_25d'],
      'gamma_concentrations':opt['gamma_concentrations'],
      'quality':{'notes':errors,'manual_inputs':['CoinGlass liquidation heatmap','Real dealer GEX vendor screenshot'],
                 'gex_warning':'Gross GEX is unsigned; signed dealer GEX is a model estimate using the documented short-call/long-put assumption, not observed positioning',
                 'oi_warning':'Binance BTCUSDT and Deribit BTC-PERPETUAL are separate markets, not total global BTC OI',
                 'freshness_policy':'Age is measured at publication. Consumer must also compare timestamp to current UTC; do not trust cached age alone.'}}
    snapshot['data_age']=age_metrics(snapshot,now)
    counts={'ok':0,'error':0,'stale':0,'not_applicable':0}
    def count(obj):
        if isinstance(obj,dict):
            if 'value' in obj and obj.get('status') in counts: counts[obj['status']]+=1
            for v in obj.values(): count(v)
        elif isinstance(obj,list):
            for v in obj: count(v)
    count(snapshot); snapshot['quality']['metric_status_counts']=counts
    snapshot['status']='partial' if counts['error'] or counts['stale'] else 'ok'
    if counts['ok']==0: snapshot['status']='error'
    # Full option inventory stays downloadable; the raw analysis URL stays compact.
    chain=copy.deepcopy(snapshot['options'])
    chain['snapshot_timestamp']=snapshot['timestamp']
    write_json(root/'options_chain.json',chain)
    snapshot['options'].pop('contracts',None)
    snapshot['options'].pop('by_strike',None)
    snapshot['options']['contracts_file']='options_chain.json'
    for entry in snapshot['options'].get('by_expiry',{}).values():
        entry.pop('by_strike',None)
        entry.pop('gamma_concentrations',None)
    for container in (snapshot,snapshot['options']):
        g=container.get('gamma_concentrations',{})
        if isinstance(g.get('value'),list):
            g['full_strike_count']=len(g['value'])
            g['value']=g['value'][:20]
            g['full_data_file']='options_chain.json'
    # Avoid an age-map larger than the concise snapshot. Every metric retains its age.
    snapshot['data_age']={k:v for k,v in snapshot['data_age'].items()
                          if not k.startswith('options.') and not k.startswith('gamma_concentrations.')}
    validate(snapshot)
    write_json(root/'latest.json',snapshot)
    update_history(root/'history.csv',snapshot)
    write_json(root/'state/coinbase.json',new_state)
    report={'tested_at':iso(now),'status':snapshot['status'],'endpoint_requests':[
        {**x,'requested_at':iso(x['requested_at']),'received_at':iso(x['received_at'])} for x in client.audit]}
    write_json(root/'docs/latest-endpoint-check.json',report)
    summary=f"BTC collector: {snapshot['status']} | {counts['ok']} OK, {counts['error']} errors, {counts['stale']} stale | {iso(now)}"
    logging.info(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:
            f.write('## BTC collector\n\n'+summary+'\n\nSee `latest.json` for per-field reasons and `docs/latest-endpoint-check.json` for HTTP evidence.\n')
            for venue in ('binance','coinbase'):
                f.write(f"\n- {venue} spot: {snapshot['spot'][venue]['status']}; CVD 24h: {snapshot['cvd'][venue]['24h']['status']}\n")
    if snapshot['status']!='ok': print('::warning::'+summary,flush=True)
    return snapshot

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path('.'))
    parser.add_argument('--coinbase-max-pages',type=int,default=600)
    parser.add_argument('--option-budget',type=int,default=720)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)sZ %(levelname)s %(message)s')
    logging.Formatter.converter=time.gmtime
    args.output_dir.mkdir(parents=True,exist_ok=True)
    with open(args.output_dir/'.collector.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        collect(args.output_dir,max_pages=args.coinbase_max_pages,option_budget=args.option_budget)

if __name__=='__main__': main()
