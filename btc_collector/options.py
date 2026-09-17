"""Complete BTC option inventory; unsigned gamma exposure, never dealer positions."""
import logging
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from .core import (black_scholes_gamma, epoch, fresh, gross_gamma, interpolate_delta, iso,
                   metric, missing, number, signed_dealer_gamma)
from .sources import DERIBIT

FIELDS=('open_interest','mark_iv','delta','gamma','index_price','underlying_price')

def option_row(client,inst):
    name=inst['instrument_name']; src=DERIBIT+'/public/ticker?instrument_name='+name
    row={k:inst.get(k) for k in ('instrument_name','option_type','settlement_currency','quote_currency','contract_size','instrument_type')}
    row.update(strike=number(inst['strike'],1e-12),expiry=iso(inst['expiration_timestamp']/1000))
    try:
        d,_,received=client.get(DERIBIT,'/public/ticker',instrument_name=name)
        r=d['result']; t=number(r['timestamp'])/1000
        if r['instrument_name']!=name: raise ValueError('Ticker instrument mismatch')
        if r.get('state')!='open': raise ValueError('Instrument is no longer open')
        for key in FIELDS:
            unit={'open_interest':'BTC','mark_iv':'volatility percentage points','delta':'BTC/BTC','gamma':'1/USD',
                  'index_price':'USD','underlying_price':'USD'}[key]
            try:
                raw=r.get('greeks',{}).get(key) if key in ('delta','gamma') else r.get(key)
                low=-1 if key=='delta' else (1e-12 if key in ('index_price','underlying_price','mark_iv') else 0)
                high=1 if key=='delta' else None
                row[key]=fresh(number(raw,low,high),src,t,received,unit,max_age=900)
            except ValueError as e: row[key]=missing(src,str(e),unit=unit)
        if all(row[k]['status']=='ok' for k in ('gamma','open_interest','index_price')):
            row['gross_gex_proxy']=metric(gross_gamma(row['gamma']['value'],row['open_interest']['value'],row['index_price']['value']),
                                         src,t,'USD per 1% BTC move')
        else: row['gross_gex_proxy']=missing(src,'Gamma, OI or index unavailable')
    except Exception as e:
        logging.warning('Option %s failed: %s',name,e)
        for k in (*FIELDS,'gross_gex_proxy'): row[k]=missing(src,str(e))
    return row

def aggregate(rows,source='Deribit public option tickers'):
    def total(field,unit):
        valid=[r[field] for r in rows if r[field]['status']=='ok']
        # Missing gamma at zero OI need not poison exposure; missing OI always does.
        if field=='gross_gex_proxy':
            required=[r for r in rows if r['open_interest']['status']!='ok' or r['open_interest']['value']>0]
            valid=[r[field] for r in required if r[field]['status']=='ok']
            expected=len(required)
        else: expected=len(rows)
        if len(valid)!=expected:
            return missing(source,f'Incomplete coverage: {len(valid)}/{expected}',unit=unit,
                           covered_contracts=len(valid),expected_contracts=expected)
        ts=[epoch(m['timestamp']) for m in valid]
        return metric(sum(m['value'] for m in valid),source,min(ts) if ts else time.time(),unit,
                      covered_contracts=len(valid),expected_contracts=expected)
    by_strike=[]
    for strike in sorted({r['strike'] for r in rows}):
        group=[r for r in rows if r['strike']==strike]
        line={'strike':strike}
        for side in ('call','put'):
            subset=[r for r in group if r['option_type']==side]
            valid=[r['open_interest'] for r in subset if r['open_interest']['status']=='ok']
            if len(valid)!=len(subset): line[side+'_oi']=missing(source,'Incomplete strike OI',unit='BTC')
            else: line[side+'_oi']=metric(sum(m['value'] for m in valid),source,
                    min((epoch(m['timestamp']) for m in valid),default=time.time()),'BTC')
        required=[r for r in group if r['open_interest']['status']!='ok' or r['open_interest']['value']>0]
        vals=[r['gross_gex_proxy'] for r in required if r['gross_gex_proxy']['status']=='ok']
        line['gross_gex_proxy']=metric(sum(m['value'] for m in vals),source,
            min((epoch(m['timestamp']) for m in vals),default=time.time()),'USD per 1% BTC move') if len(vals)==len(required) else missing(source,'Incomplete strike gamma coverage')
        by_strike.append(line)
    oi_total=total('open_interest','BTC'); gex_total=total('gross_gex_proxy','USD per 1% BTC move')
    walls={}
    for side in ('put','call'):
        vals=[x for x in by_strike if x[side+'_oi']['status']=='ok' and x[side+'_oi']['value']>0]
        if oi_total['status']!='ok' or not vals:
            walls[side+'_wall']=missing(source,'Incomplete or zero OI; wall unavailable',unit='USD strike')
        else:
            top=max(x[side+'_oi']['value'] for x in vals)
            ties=[x['strike'] for x in vals if x[side+'_oi']['value']==top]
            walls[side+'_wall']=metric(min(ties),source,epoch(oi_total['timestamp']),'USD strike',
                oi_btc=top,tied_strikes=ties,definition='Strike with maximum '+side+' OI; not guaranteed support/resistance')
    concentrations=[]
    if gex_total['status']=='ok':
        for x in sorted(by_strike,key=lambda x:x['gross_gex_proxy']['value'],reverse=True):
            concentrations.append({'strike':x['strike'],'gross_gex_proxy':x['gross_gex_proxy'],
                'share_pct':metric(x['gross_gex_proxy']['value']/gex_total['value']*100 if gex_total['value'] else 0,
                                   source,epoch(gex_total['timestamp']),'%')})
    return {'total_oi':oi_total,'gross_gex_proxy':gex_total,'by_strike':by_strike,**walls,
            'gamma_concentrations':metric(concentrations,source,epoch(gex_total['timestamp'])) if gex_total['status']=='ok' else missing(source,'Incomplete gamma coverage')}

def dealer_gex_estimate(rows,calculation_time=None,grid_min_ratio=.5,grid_max_ratio=1.5,grid_points=201):
    """Estimate signed dealer GEX and revalue it to locate a zero-gamma crossing."""
    now=time.time() if calculation_time is None else number(calculation_time,0)
    source='Deribit public option tickers; Black-Scholes repricing'
    methodology={
      'label':'Estimated signed dealer GEX; not observable dealer positioning',
      'dealer_position_assumption':'Dealers net short calls and net long puts relative to customers',
      'gamma_model':'Black-Scholes spot gamma with r=0 and q=0',
      'volatility_input':'Deribit mark_iv divided by 100',
      'spot_input':'Most recent valid Deribit BTC index_price in the collected option chain',
      'open_interest_units':'Deribit BTC option open_interest is BTC; contract_size is not multiplied again',
      'gex_units':'USD per 1% BTC spot move = gamma * OI_BTC * spot^2 * 0.01',
      'flip_method':f'Revalue all covered contracts from {grid_min_ratio:.0%} to {grid_max_ratio:.0%} of current spot on {grid_points} equally spaced points; linearly interpolate sign changes',
      'regime_rule':'long_gamma when spot > flip; short_gamma when spot < flip',
      'spot_to_flip_definition':'(spot - zero_gamma_flip) / zero_gamma_flip * 100',
      'not_retailinterest':'Independent estimate; does not scrape or depend on RetailInterest'}
    coverage={'active_contracts':len(rows),'positive_oi_contracts':0,'covered_positive_oi_contracts':0,
              'zero_oi_contracts':0,'missing_oi_contracts':0,'missing_input_contracts':0}
    index=[r['index_price'] for r in rows if r.get('index_price',{}).get('status')=='ok']
    if not index:
        reason='No valid Deribit BTC index price'
        failed=lambda unit=None: missing(source,reason,unit=unit)
        return {'status':'error','calculation_timestamp':iso(now),'data_coverage':coverage,'methodology':methodology,
                'net_gex_estimate_usd_per_1pct':failed('USD per 1% BTC move'),
                'gex_by_strike':failed(),'zero_gamma_flip':failed('USD'),
                'spot_to_gamma_flip_pct':failed('%'),'gamma_regime':failed(),
                'largest_positive_gamma_concentrations_near_spot':failed(),
                'largest_negative_gamma_concentrations_near_spot':failed()}
    spot_metric=max(index,key=lambda m:epoch(m['timestamp']))
    spot=spot_metric['value']
    usable=[]; timestamps=[epoch(spot_metric['timestamp'])]
    for row in rows:
        oi=row.get('open_interest',{})
        if oi.get('status')!='ok' or oi.get('value') is None:
            coverage['missing_oi_contracts']+=1
            continue
        if oi['value']==0:
            coverage['zero_oi_contracts']+=1
            continue
        coverage['positive_oi_contracts']+=1
        iv=row.get('mark_iv',{})
        try:
            expiry=epoch(row['expiry'])
            if (iv.get('status')!='ok' or iv.get('value') is None or expiry<=now
                    or row.get('option_type') not in ('call','put')):
                raise ValueError('missing or expired input')
            entry={'strike':number(row['strike'],1e-12),'option_type':row['option_type'],
                   'oi':number(oi['value'],0),'sigma':number(iv['value'],1e-12)/100,
                   'tau':(expiry-now)/(365.25*86400)}
            usable.append(entry); timestamps.extend((epoch(oi['timestamp']),epoch(iv['timestamp'])))
            coverage['covered_positive_oi_contracts']+=1
        except (KeyError,ValueError):
            coverage['missing_input_contracts']+=1
    coverage['coverage_pct']=(coverage['covered_positive_oi_contracts']/coverage['positive_oi_contracts']*100
                              if coverage['positive_oi_contracts'] else 100.0)
    complete=(coverage['missing_oi_contracts']==0 and coverage['missing_input_contracts']==0)
    if not complete:
        reason=('Incomplete signed-GEX inputs: '
                f"{coverage['covered_positive_oi_contracts']}/{coverage['positive_oi_contracts']} positive-OI contracts covered; "
                f"{coverage['missing_oi_contracts']} contracts missing OI")
        failed=lambda unit=None: missing(source,reason,unit=unit,**coverage)
        return {'status':'error','calculation_timestamp':iso(now),'data_coverage':coverage,'methodology':methodology,
                'index_price':spot_metric,'net_gex_estimate_usd_per_1pct':failed('USD per 1% BTC move'),
                'gex_by_strike':failed(),'zero_gamma_flip':failed('USD'),
                'spot_to_gamma_flip_pct':failed('%'),'gamma_regime':failed(),
                'largest_positive_gamma_concentrations_near_spot':failed(),
                'largest_negative_gamma_concentrations_near_spot':failed()}
    quote_time=min(timestamps)
    def contributions(test_spot):
        values=[]
        for item in usable:
            gamma=black_scholes_gamma(test_spot,item['strike'],item['sigma'],item['tau'],0)
            values.append((item['strike'],signed_dealer_gamma(gamma,item['oi'],test_spot,item['option_type'])))
        return values
    current=contributions(spot)
    net=sum(v for _,v in current)
    strikes=[]
    for strike in sorted({k for k,_ in current}):
        strikes.append({'strike':strike,'net_gex_usd_per_1pct':sum(v for k,v in current if k==strike)})
    near=[x for x in strikes if .75*spot<=x['strike']<=1.25*spot]
    positive=sorted((x for x in near if x['net_gex_usd_per_1pct']>0),
                    key=lambda x:x['net_gex_usd_per_1pct'],reverse=True)[:10]
    negative=sorted((x for x in near if x['net_gex_usd_per_1pct']<0),
                    key=lambda x:x['net_gex_usd_per_1pct'])[:10]
    xs=[spot*(grid_min_ratio+(grid_max_ratio-grid_min_ratio)*i/(grid_points-1)) for i in range(grid_points)]
    ys=[sum(v for _,v in contributions(x)) for x in xs]
    roots=[]
    if not all(abs(y)<=1e-12 for y in ys):
        for x1,y1,x2,y2 in zip(xs,ys,xs[1:],ys[1:]):
            if y1==0: roots.append(x1)
            elif y1*y2<0: roots.append(x1-y1*(x2-x1)/(y2-y1))
        if ys[-1]==0: roots.append(xs[-1])
    roots=sorted({round(x,10) for x in roots})
    common={'source':source,'timestamp':quote_time}
    net_metric=metric(net,**common,unit='USD per 1% BTC move',**coverage)
    strike_metric=metric(strikes,**common,covered_strikes=len(strikes))
    pos_metric=metric(positive,**common,near_spot_range_pct=25)
    neg_metric=metric(negative,**common,near_spot_range_pct=25)
    if roots:
        flip=min(roots,key=lambda x:abs(x-spot))
        flip_metric=metric(flip,**common,unit='USD',crossing_count=len(roots),tested_range_usd=[xs[0],xs[-1]])
        distance=metric((spot-flip)/flip*100,**common,unit='%')
        regime=metric('long_gamma' if spot>flip else ('short_gamma' if spot<flip else 'at_flip'),**common)
    else:
        reason='No aggregate signed-GEX zero crossing inside the tested spot range'
        flip_metric=missing(source,reason,status='not_applicable',unit='USD',tested_range_usd=[xs[0],xs[-1]])
        distance=missing(source,reason,status='not_applicable',unit='%')
        regime=missing(source,reason,status='not_applicable')
    return {'status':'ok','calculation_timestamp':iso(now),'data_coverage':coverage,'methodology':methodology,
            'index_price':spot_metric,'net_gex_estimate_usd_per_1pct':net_metric,
            'gex_by_strike':strike_metric,'zero_gamma_flip':flip_metric,
            'spot_to_gamma_flip_pct':distance,'gamma_regime':regime,
            'largest_positive_gamma_concentrations_near_spot':pos_metric,
            'largest_negative_gamma_concentrations_near_spot':neg_metric}

def surface(rows):
    src='Deribit mark IV; same expiry and settlement currency'
    out={}
    valid=[r for r in rows if r['mark_iv']['status']=='ok' and r['underlying_price']['status']=='ok']
    if valid:
        forward=statistics.median(r['underlying_price']['value'] for r in valid)
        # Nearest common call/put strike to expiry forward; no mixing strikes silently.
        cs={r['strike'] for r in valid if r['option_type']=='call'}
        ps={r['strike'] for r in valid if r['option_type']=='put'}
        if cs&ps:
            k=min(cs&ps,key=lambda k:abs(math.log(k/forward)))
            pair=[r for r in valid if r['strike']==k]
            t=min(epoch(r['mark_iv']['timestamp']) for r in pair)
            out['atm_iv']=metric(statistics.mean(r['mark_iv']['value'] for r in pair),src,t,'volatility percentage points',
                strike=k,forward_price=forward,method='Nearest-forward common strike; mean call/put mark IV')
    out.setdefault('atm_iv',missing(src,'No valid common ATM strike'))
    for side,target in (('call',0.25),('put',-0.25)):
        subset=[r for r in rows if r['option_type']==side]
        try:
            iv,names=interpolate_delta(subset,target)
            t=min(epoch(r['mark_iv']['timestamp']) for r in subset if r['instrument_name'] in names)
            out[side+'_25d_iv']=metric(iv,src,t,'volatility percentage points',instruments=names,method='Linear interpolation in exchange delta; no extrapolation')
        except ValueError as e: out[side+'_25d_iv']=missing(src,str(e))
    c,p=out['call_25d_iv'],out['put_25d_iv']
    if c['status']=='ok' and p['status']=='ok':
        t=min(epoch(c['timestamp']),epoch(p['timestamp']))
        out['risk_reversal_25d']=metric(c['value']-p['value'],src,t,'volatility percentage points',definition='25D call IV minus 25D put IV')
        out['put_minus_call_skew_25d']=metric(p['value']-c['value'],src,t,'volatility percentage points')
    else:
        out['risk_reversal_25d']=missing(src,'Both 25D wings required')
        out['put_minus_call_skew_25d']=missing(src,'Both 25D wings required')
    return out

def collect_options(client,budget=720):
    started=time.monotonic()
    data,_,received=client.get(DERIBIT,'/public/get_instruments',currency='any',kind='option',expired='false')
    instruments=[i for i in data['result'] if i.get('base_currency')=='BTC' and i.get('is_active')
                 and i['expiration_timestamp']/1000>received and i.get('option_type') in ('call','put')]
    if not instruments: raise ValueError('No active BTC option instruments')
    if len({i['instrument_name'] for i in instruments})!=len(instruments): raise ValueError('Duplicate instrument names')
    instruments.sort(key=lambda i:(i['expiration_timestamp'],i['instrument_name']))
    def one(inst):
        if time.monotonic()-started>budget:
            row={k:inst.get(k) for k in ('instrument_name','option_type','settlement_currency','quote_currency','contract_size','instrument_type')}
            row.update(strike=number(inst['strike']),expiry=iso(inst['expiration_timestamp']/1000))
            row.update({k:missing(DERIBIT,'Option collection time budget exceeded') for k in (*FIELDS,'gross_gex_proxy')})
            return row
        return option_row(client,inst)
    with ThreadPoolExecutor(max_workers=32) as pool: rows=list(pool.map(one,instruments))
    finished=time.time()
    # Guard against a long collection run making early quotes stale or contracts expired.
    for row in rows:
        for key in (*FIELDS,'gross_gex_proxy'):
            m=row[key]
            if m['status']=='ok' and (finished-epoch(m['timestamp'])>900 or epoch(row['expiry'])<=finished):
                m.update(value=None,status='stale',reason='Expired contract or quote older than 15 minutes at collection end')
    dealer=dealer_gex_estimate(rows,finished)
    result={'status':'ok','source':DERIBIT,'inventory_timestamp':iso(received),
            'scope':'All active base_currency=BTC vanilla options; all settlement currencies; combos excluded',
            'contracts':rows,'active_contract_count':len(rows),'by_expiry':{},
            'gex_label':'Gross gamma exposure / GEX proxy; dealer direction unknown',**aggregate(rows),
            'dealer_gex_estimate':{k:v for k,v in dealer.items() if k not in
                ('net_gex_estimate_usd_per_1pct','gex_by_strike','zero_gamma_flip',
                 'spot_to_gamma_flip_pct','gamma_regime')},
            **{k:dealer[k] for k in ('net_gex_estimate_usd_per_1pct','gex_by_strike','zero_gamma_flip',
                                     'spot_to_gamma_flip_pct','gamma_regime')}}
    for expiry in sorted({r['expiry'] for r in rows}):
        group=[r for r in rows if r['expiry']==expiry]
        stats=aggregate(group); stats['surfaces']={}
        for currency in sorted({r['settlement_currency'] for r in group}):
            stats['surfaces'][currency]=surface([r for r in group if r['settlement_currency']==currency])
        result['by_expiry'][expiry]=stats
    if (result['total_oi']['status']!='ok' or result['gross_gex_proxy']['status']!='ok'
            or result['net_gex_estimate_usd_per_1pct']['status']!='ok'
            or any(row[k]['status']!='ok' for row in rows for k in FIELDS)):
        result['status']='partial'
    # Headline = inverse BTC expiry nearest 30d in [7,60] days. No hidden expiry mixing.
    candidates=[(e,v) for e,v in result['by_expiry'].items() if 7<=(epoch(e)-finished)/86400<=60 and 'BTC' in v['surfaces']]
    if candidates:
        expiry,entry=min(candidates,key=lambda pair:abs((epoch(pair[0])-finished)/86400-30))
        result['headline_surface']={'expiry':expiry,'settlement_currency':'BTC',**entry['surfaces']['BTC']}
    else:
        result['headline_surface']={'expiry':None,**{k:missing(DERIBIT,'No BTC-settled expiry in 7–60 day range') for k in ('atm_iv','risk_reversal_25d','call_25d_iv','put_25d_iv')}}
    return result
