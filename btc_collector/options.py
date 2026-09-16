"""Complete BTC option inventory; unsigned gamma exposure, never dealer positions."""
import logging
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from .core import epoch, fresh, gross_gamma, interpolate_delta, iso, metric, missing, number
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
    result={'status':'ok','source':DERIBIT,'inventory_timestamp':iso(received),
            'scope':'All active base_currency=BTC vanilla options; all settlement currencies; combos excluded',
            'contracts':rows,'active_contract_count':len(rows),'by_expiry':{},
            'gex_label':'Gross gamma exposure / GEX proxy; dealer direction unknown',**aggregate(rows)}
    for expiry in sorted({r['expiry'] for r in rows}):
        group=[r for r in rows if r['expiry']==expiry]
        stats=aggregate(group); stats['surfaces']={}
        for currency in sorted({r['settlement_currency'] for r in group}):
            stats['surfaces'][currency]=surface([r for r in group if r['settlement_currency']==currency])
        result['by_expiry'][expiry]=stats
    if (result['total_oi']['status']!='ok' or result['gross_gex_proxy']['status']!='ok'
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
