"""Complete BTC option inventory, gross GEX, and an explicitly assumed signed estimate."""
import logging
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from .core import (black_scholes_gamma, epoch, fresh, gross_gamma, interpolate_delta, iso,
                   metric, missing, number, signed_dealer_gamma)
from .sources import DERIBIT

FIELDS=('open_interest','mark_iv','delta','gamma','index_price','underlying_price','interest_rate')

def zero_crossings(xs,ys):
    """Return interpolated roots and their sign orientation for a sampled curve."""
    if len(xs)!=len(ys) or len(xs)<2 or any(b<=a for a,b in zip(xs,xs[1:])):
        raise ValueError('Zero-crossing grid must have equal lengths and increasing x values')
    scale=max((abs(y) for y in ys),default=0)
    tolerance=max(1e-12,scale*1e-12)
    signs=[0 if abs(y)<=tolerance else (1 if y>0 else -1) for y in ys]
    roots=[]; i=0
    while i<len(xs)-1:
        if signs[i] and signs[i+1] and signs[i]!=signs[i+1]:
            x=xs[i]-ys[i]*(xs[i+1]-xs[i])/(ys[i+1]-ys[i])
            roots.append({'price':x,'direction':'negative_to_positive' if signs[i]<signs[i+1] else 'positive_to_negative'})
            i+=1; continue
        if signs[i]==0:
            start=i
            while i+1<len(xs) and signs[i+1]==0: i+=1
            left=signs[start-1] if start else 0
            right=signs[i+1] if i+1<len(xs) else 0
            if left and right and left!=right:
                roots.append({'price':(xs[start]+xs[i])/2,
                              'direction':'negative_to_positive' if left<right else 'positive_to_negative'})
        i+=1
    # Adjacent intervals and exact-zero blocks cannot legitimately yield duplicates,
    # but normalize defensively for compact diagnostics.
    unique=[]
    for root in roots:
        if not any(abs(root['price']-old['price'])<=1e-9 for old in unique): unique.append(root)
    return unique

def primary_zero_crossing(crossings,spot):
    return min(crossings,key=lambda root:abs(root['price']-number(spot,1e-12))) if crossings else None

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
                  'index_price':'USD','underlying_price':'USD','interest_rate':'fraction'}[key]
            try:
                raw=r.get('greeks',{}).get(key) if key in ('delta','gamma') else r.get(key)
                low=-1 if key in ('delta','interest_rate') else (1e-12 if key in ('index_price','underlying_price','mark_iv') else 0)
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

def _model_summary(items, gamma_fn, sign_multiplier=1, strike_range=None):
    """Compact decomposition for one GEX convention over one immutable chain."""
    by_strike={}; by_expiry={}; calls=puts=0.0
    for item in items:
        if strike_range and not strike_range[0] <= item['strike'] <= strike_range[1]:
            continue
        gamma,normalization=gamma_fn(item)
        sign=(-1 if item['option_type']=='call' else 1)*sign_multiplier
        value=sign*gamma*item['oi']*normalization**2*.01
        by_strike[item['strike']]=by_strike.get(item['strike'],0.0)+value
        by_expiry[item['expiry_iso']]=by_expiry.get(item['expiry_iso'],0.0)+value
        if item['option_type']=='call': calls+=value
        else: puts+=value
    values=list(by_strike.values())
    top_strikes=sorted(({'strike':k,'net_gex_usd_per_1pct':v} for k,v in by_strike.items()),
                       key=lambda x:abs(x['net_gex_usd_per_1pct']),reverse=True)[:10]
    top_expiries=sorted(({'expiry':k,'net_gex_usd_per_1pct':v} for k,v in by_expiry.items()),
                        key=lambda x:abs(x['net_gex_usd_per_1pct']),reverse=True)[:10]
    return {'net_gex_usd_per_1pct':sum(values),'call_contribution_usd_per_1pct':calls,
            'put_contribution_usd_per_1pct':puts,
            'gross_positive_usd_per_1pct':sum(v for v in values if v>0),
            'gross_negative_usd_per_1pct':sum(v for v in values if v<0),
            'top_strikes_by_absolute_contribution':top_strikes,
            'top_expiries_by_absolute_contribution':top_expiries,'by_strike':by_strike}

def _cumulative_strike_crossings(by_strike):
    strikes=sorted(by_strike)
    if len(strikes)<2: return []
    cumulative=[]; total=0.0
    for strike in strikes:
        total+=by_strike[strike]; cumulative.append(total)
    return zero_crossings(strikes,cumulative)

def gamma_reconciliation(rows):
    """Reconcile recomputed Black-Scholes gamma against Deribit's rounded ticker gamma."""
    candidates={'single_latest_index_price':[], 'per_contract_index_price':[],
                'expiry_specific_underlying_price':[]}
    valid=[r for r in rows if r.get('open_interest',{}).get('status')=='ok'
           and r['open_interest']['value']>0 and r.get('gamma',{}).get('status')=='ok'
           and r.get('mark_iv',{}).get('status')=='ok' and r.get('underlying_price',{}).get('status')=='ok'
           and r.get('index_price',{}).get('status')=='ok' and epoch(r['expiry'])>epoch(r['gamma']['timestamp'])]
    if not valid: return {'status':'error','reason':'No positive-OI ticker rows with gamma'}
    latest=max(valid,key=lambda r:epoch(r['index_price']['timestamp']))['index_price']['value']
    for row in valid:
        observed=row['gamma']['value']; tau=(epoch(row['expiry'])-epoch(row['gamma']['timestamp']))/(365.25*86400)
        for name,ref in (('single_latest_index_price',latest),('per_contract_index_price',row['index_price']['value']),
                         ('expiry_specific_underlying_price',row['underlying_price']['value'])):
            computed=black_scholes_gamma(ref,row['strike'],row['mark_iv']['value']/100,tau,0)
            candidates[name].append((row,observed,computed))
    def stats(values):
        rel=sorted(abs(c-o)/o for _,o,c in values if o>0)
        material=sorted(abs(c-o)/o for _,o,c in values if o>=5e-5)
        percentile=lambda xs,p: xs[round((len(xs)-1)*p)] if xs else None
        return {'contracts_compared':len(values),'published_gamma_zero_count':sum(o==0 for _,o,_ in values),
                'nonzero_relative_error_count':len(rel),
                'median_relative_error_pct':percentile(rel,.5)*100 if rel else None,
                'p90_relative_error_pct':percentile(rel,.9)*100 if rel else None,
                'material_relative_error_count':len(material),
                'material_median_relative_error_pct':percentile(material,.5)*100 if material else None,
                'material_p90_relative_error_pct':percentile(material,.9)*100 if material else None,
                'max_material_relative_error_pct':max(material)*100 if material else None,
                'material_gamma_threshold':'published gamma >= 0.00005 1/USD',
                'rounded_5_decimal_match_pct':sum(round(c,5)==o for _,o,c in values)/len(values)*100}
    selected=candidates['expiry_specific_underlying_price']
    by_expiry={}; by_moneyness={k:[] for k in ('deep_itm','itm','atm','otm','deep_otm')}
    for row,obs,computed in selected:
        by_expiry.setdefault(row['expiry'],[]).append((row,obs,computed))
        ref=row['underlying_price']['value']; x=abs(math.log(row['strike']/ref)); itm=(row['option_type']=='call')==(row['strike']<ref)
        bucket='atm' if x<=.025 else (('itm' if x<=.1 else 'deep_itm') if itm else ('otm' if x<=.1 else 'deep_otm'))
        by_moneyness[bucket].append((row,obs,computed))
    chosen=[]
    for side in ('call','put'):
        side_rows=[x for x in selected if x[0]['option_type']==side]
        if side_rows:
            chosen.append(('near_atm_'+side,min(side_rows,key=lambda x:abs(math.log(x[0]['strike']/x[0]['underlying_price']['value'])))))
            otm=[x for x in side_rows if (x[0]['strike']>x[0]['underlying_price']['value'])==(side=='call')
                 and abs(math.log(x[0]['strike']/x[0]['underlying_price']['value']))>=.10]
            if otm: chosen.append(('otm_'+side,min(otm,key=lambda x:abs(abs(math.log(x[0]['strike']/x[0]['underlying_price']['value']))-.15))))
            latest=max(epoch(x[0]['expiry']) for x in side_rows)
            longdated=[x for x in side_rows if epoch(x[0]['expiry'])==latest]
            chosen.append(('long_dated_'+side,min(longdated,key=lambda x:abs(math.log(x[0]['strike']/x[0]['underlying_price']['value'])))))
    traces=[]
    for category,(row,observed,computed) in chosen:
        ref=row['underlying_price']['value']; sign=-1 if row['option_type']=='call' else 1
        traces.append({'category':category,'instrument':row['instrument_name'],'ticker_timestamp':row['gamma']['timestamp'],
          'expiry':row['expiry'],'strike':row['strike'],'option_type':row['option_type'],
          'index_price':row['index_price']['value'],'underlying_price':ref,'mark_iv_pct':row['mark_iv']['value'],
          'deribit_gamma':observed,'our_gamma':computed,'open_interest_btc':row['open_interest']['value'],
          'dealer_sign':sign,'usd_gex_per_1pct':sign*computed*row['open_interest']['value']*ref**2*.01})
    return {'status':'ok','selected_method':'expiry_specific_underlying_price',
            'deribit_gamma_precision_note':'Ticker gamma is rounded to five decimal places; relative errors are unstable near zero',
            'candidate_error_statistics':{k:stats(v) for k,v in candidates.items()},
            'selected_errors_by_expiry':{k:stats(v) for k,v in sorted(by_expiry.items())},
            'selected_errors_by_moneyness':{k:stats(v) for k,v in by_moneyness.items() if v},
            'representative_contract_traces':traces}

def dealer_gex_estimate(rows,calculation_time=None,grid_min_ratio=.5,grid_max_ratio=1.5,grid_points=201,
                        snapshot_metadata=None,published_gamma_rows=None):
    """Estimate signed dealer GEX from a coherent Deribit bulk snapshot."""
    now=time.time() if calculation_time is None else number(calculation_time,0)
    source='Deribit public/get_book_summary_by_currency; Black-Scholes repricing'
    assumptions={'dealer_calls':'net short','dealer_puts':'net long','risk_free_rate':0,
                 'dividend_yield':0,'open_interest_unit':'BTC base currency as reported by Deribit',
                 'volatility_unit':'mark_iv percentage points divided by 100','observable_positioning':False}
    methodology={
      'label':'Estimated signed dealer GEX; not observable dealer positioning',
      'dealer_position_assumption':'Dealer short calls (negative gamma), dealer long puts (positive gamma)',
      'gamma_model':'Standard Black-Scholes gamma with r=0 and q=0',
      'volatility_input':'Deribit mark_iv divided by 100',
      'reference_price_input':'Each option expiry underlying_price used by Deribit for IV calculations',
      'open_interest_units':'Deribit option open_interest is underlying BTC; contract_size is not multiplied again',
      'gex_units':'USD per 1% move in the expiry-specific underlying = gamma * OI_BTC * underlying_price^2 * 0.01',
      'repriced_flip_method':f'Parallel-shift each expiry underlying with spot over {grid_min_ratio:.0%}-{grid_max_ratio:.0%} on {grid_points} points; hold contract IV fixed; interpolate sign changes',
      'cumulative_strike_flip_method':'Sort current signed GEX by strike, cumulate low-to-high, and interpolate cumulative zero crossings',
      'regime_rule':'Sign of aggregate estimated dealer GEX at current snapshot',
      'not_retailinterest':'Independent model; RetailInterest is not queried by the collector'}
    names=[r.get('instrument_name') for r in rows]
    required=('open_interest','mark_iv','underlying_price','index_price')
    coverage={'active_instruments_expected':len(rows),'unique_instruments':len(set(names)),
              'duplicate_instruments':len(rows)-len(set(names)),
              'instruments_successfully_fetched':sum(all(r.get(k,{}).get('status')=='ok' for k in required) for r in rows),
              'instruments_used_in_signed_gex':0,'instruments_excluded':0,'positive_oi_contracts':0,
              'covered_positive_oi_contracts':0,'zero_oi_contracts':0,'missing_oi_contracts':0,
              'missing_input_contracts':0,'exclusion_reasons':{'zero_open_interest':0,'missing_open_interest':0,
              'missing_mark_iv':0,'missing_underlying_price':0,'expired_at_calculation':0,
              'invalid_contract_metadata':0,'duplicate_instrument_names':len(rows)-len(set(names))}}
    index=[r['index_price'] for r in rows if r.get('index_price',{}).get('status')=='ok']
    spot_metric=max(index,key=lambda m:epoch(m['timestamp'])) if index else None
    usable=[]; timestamps=[epoch(spot_metric['timestamp'])] if spot_metric else []
    published_map={r.get('instrument_name'):r.get('gamma',{}).get('value') for r in (published_gamma_rows or [])
                   if r.get('gamma',{}).get('status')=='ok'}
    for row in rows:
        oi=row.get('open_interest',{})
        if oi.get('status')!='ok' or oi.get('value') is None:
            coverage['missing_oi_contracts']+=1; coverage['exclusion_reasons']['missing_open_interest']+=1; continue
        if oi['value']==0:
            coverage['zero_oi_contracts']+=1; coverage['exclusion_reasons']['zero_open_interest']+=1; continue
        coverage['positive_oi_contracts']+=1; exclusion=None
        try:
            expiry=epoch(row['expiry']); iv=row.get('mark_iv',{}); underlying=row.get('underlying_price',{})
            if iv.get('status')!='ok' or iv.get('value') is None: exclusion='missing_mark_iv'; raise ValueError
            if underlying.get('status')!='ok' or underlying.get('value') is None: exclusion='missing_underlying_price'; raise ValueError
            if expiry<=now: exclusion='expired_at_calculation'; raise ValueError
            if row.get('option_type') not in ('call','put') or not row.get('instrument_name'):
                exclusion='invalid_contract_metadata'; raise ValueError
            usable.append({'instrument_name':row['instrument_name'],'strike':number(row['strike'],1e-12),
              'option_type':row['option_type'],'oi':number(oi['value'],0),'sigma':number(iv['value'],1e-12)/100,
              'tau':(expiry-now)/(365.25*86400),'expiry':expiry,'expiry_iso':row['expiry'],
              'underlying':number(underlying['value'],1e-12),
              'published_gamma':published_map.get(row['instrument_name'],row.get('gamma',{}).get('value'))})
            timestamps.extend((epoch(oi['timestamp']),epoch(iv['timestamp']),epoch(underlying['timestamp'])))
            coverage['covered_positive_oi_contracts']+=1
        except (KeyError,ValueError):
            coverage['exclusion_reasons'][exclusion or 'invalid_contract_metadata']+=1; coverage['missing_input_contracts']+=1
    coverage['instruments_used_in_signed_gex']=len(usable); coverage['instruments_excluded']=len(rows)-len(usable)
    coverage['fetch_coverage_pct']=coverage['instruments_successfully_fetched']/len(rows)*100 if rows else 0
    coverage['coverage_pct']=(coverage['covered_positive_oi_contracts']/coverage['positive_oi_contracts']*100
                              if coverage['positive_oi_contracts'] else 100.0)
    complete=bool(spot_metric and not coverage['missing_oi_contracts'] and not coverage['missing_input_contracts']
                  and not coverage['duplicate_instruments'])
    metadata={k:v for k,v in (snapshot_metadata or {}).items() if not k.startswith('_')}
    metadata.update(gex_model_version='2.0.0-underlying-forward',
                    gex_reference_price_method='Deribit expiry-specific underlying_price',
                    gex_flip_method='full_surface_parallel_forward_repricing_constant_iv')
    if not complete:
        reason=('Incomplete signed-GEX inputs: '
                f"{coverage['covered_positive_oi_contracts']}/{coverage['positive_oi_contracts']} positive-OI contracts covered; "
                f"{coverage['missing_oi_contracts']} missing OI; {coverage['missing_input_contracts']} missing pricing inputs")
        failed=lambda unit=None: missing(source,reason,unit=unit,**coverage)
        return {'status':'error','diagnostic_reason':reason,'calculation_timestamp':iso(now),'data_coverage':coverage,
                'methodology':methodology,'assumptions':assumptions,**metadata,
                **{k:failed('USD' if 'flip' in k else None) for k in ('net_gex_estimate_usd_per_1pct','gex_by_strike',
                  'zero_gamma_flip','zero_gamma_flip_repriced','zero_gamma_flip_cumulative_strike','spot_to_gamma_flip_pct',
                  'gamma_regime','selected_flip_crossing_direction','crossing_count','all_zero_gamma_crossings')}}
    spot=spot_metric['value']; quote_time=min(timestamps)
    def production_gamma(item,ratio=1.0,iv_shift=0.0):
        ref=item['underlying']*ratio; sigma=max(1e-6,item['sigma']+iv_shift)
        return black_scholes_gamma(ref,item['strike'],sigma,item['tau'],0),ref
    production=_model_summary(usable,lambda item:production_gamma(item))
    legacy=_model_summary(usable,lambda item:(black_scholes_gamma(spot,item['strike'],item['sigma'],item['tau'],0),spot))
    published=_model_summary(usable,lambda item:((item['published_gamma'] if item['published_gamma'] is not None else
        black_scholes_gamma(item['underlying'],item['strike'],item['sigma'],item['tau'],0)),item['underlying']))
    opposite=_model_summary(usable,lambda item:production_gamma(item),sign_multiplier=-1)
    ri_public=_model_summary(usable,lambda item:(black_scholes_gamma(spot,item['strike'],item['sigma'],item['tau'],0),spot),
                             sign_multiplier=-1,strike_range=(.8*spot,1.25*spot))
    current=[{'strike':k,'net_gex_usd_per_1pct':v} for k,v in sorted(production['by_strike'].items())]
    net=production['net_gex_usd_per_1pct']; near=[x for x in current if .75*spot<=x['strike']<=1.25*spot]
    positive=sorted((x for x in near if x['net_gex_usd_per_1pct']>0),key=lambda x:x['net_gex_usd_per_1pct'],reverse=True)[:10]
    negative=sorted((x for x in near if x['net_gex_usd_per_1pct']<0),key=lambda x:x['net_gex_usd_per_1pct'])[:10]
    xs=[spot*(grid_min_ratio+(grid_max_ratio-grid_min_ratio)*i/(grid_points-1)) for i in range(grid_points)]
    def curve(iv_shift=0.0):
        values=[]
        for x in xs:
            ratio=x/spot; total=0.0
            for item in usable:
                gamma,ref=production_gamma(item,ratio,iv_shift)
                sign=-1 if item['option_type']=='call' else 1
                total+=sign*gamma*item['oi']*ref**2*.01
            values.append(total)
        return values
    ys=curve(); roots=zero_crossings(xs,ys); selected=primary_zero_crossing(roots,spot)
    cumulative_roots=_cumulative_strike_crossings(production['by_strike'])
    cumulative_selected=primary_zero_crossing(cumulative_roots,spot)
    common={'source':source,'timestamp':quote_time}
    if selected:
        flip=selected['price']; flip_metric=metric(flip,**common,unit='USD',crossing_count=len(roots),
          selected_crossing_direction=selected['direction'],tested_range_usd=[xs[0],xs[-1]])
        distance=metric((spot-flip)/flip*100,**common,unit='%'); direction=metric(selected['direction'],**common)
    else:
        reason='No aggregate signed-GEX zero crossing inside the tested spot range'
        flip_metric=missing(source,reason,status='not_applicable',unit='USD',tested_range_usd=[xs[0],xs[-1]])
        distance=missing(source,reason,status='not_applicable',unit='%'); direction=missing(source,reason,status='not_applicable')
    if cumulative_selected:
        cumulative_metric=metric(cumulative_selected['price'],**common,unit='USD',
            selected_crossing_direction=cumulative_selected['direction'],crossing_count=len(cumulative_roots),
            definition='Low-to-high cumulative current signed GEX by strike')
    else: cumulative_metric=missing(source,'No cumulative-by-strike zero crossing',status='not_applicable',unit='USD')
    magnitude=sum(abs(v) for v in production['by_strike'].values()); tolerance=max(1e-6,magnitude*1e-9)
    regime_value='long_gamma' if net>tolerance else ('short_gamma' if net<-tolerance else 'zero_gamma')
    iv_sensitivity={}
    for label,shift in (('mark_iv_minus_1_point',-.01),('constant_mark_iv',0),('mark_iv_plus_1_point',.01)):
        shock_roots=zero_crossings(xs,curve(shift)); chosen=primary_zero_crossing(shock_roots,spot)
        iv_sensitivity[label]=chosen['price'] if chosen else None
    comparisons={'model_a_legacy_single_index':{k:v for k,v in legacy.items() if k!='by_strike'},
      'model_b_selected_expiry_underlying':{k:v for k,v in production.items() if k!='by_strike'},
      'model_c_deribit_published_gamma':{k:v for k,v in published.items() if k!='by_strike'},
      'model_d_opposite_sign_diagnostic_only':{k:v for k,v in opposite.items() if k!='by_strike'},
      'model_e_retailinterest_public_behavior_reconstruction':{
        **{k:v for k,v in ri_public.items() if k!='by_strike'},
        'evidence':'Calls positive, puts negative, single spot input, and 80%-125% strike filter reproduce the public API; contradicts its written dealer-sign methodology'}}
    return {'status':'ok','diagnostic_reason':'Complete positive-OI coverage; production uses documented expiry underlying prices',
      'calculation_timestamp':iso(now),'data_coverage':coverage,'methodology':methodology,'assumptions':assumptions,**metadata,
      'index_price':spot_metric,'net_gex_estimate_usd_per_1pct':metric(net,**common,unit='USD per 1% move',**coverage),
      'gex_by_strike':metric(current,**common,covered_strikes=len(current)),
      'zero_gamma_flip':flip_metric,'zero_gamma_flip_repriced':flip_metric,
      'zero_gamma_flip_cumulative_strike':cumulative_metric,'spot_to_gamma_flip_pct':distance,
      'gamma_regime':metric(regime_value,**common,current_net_zero_tolerance_usd_per_1pct=tolerance),
      'selected_flip_crossing_direction':direction,'crossing_count':metric(len(roots),**common,unit='crossings'),
      'all_zero_gamma_crossings':metric(roots,**common,tested_range_usd=[xs[0],xs[-1]]),
      'all_cumulative_strike_crossings':metric(cumulative_roots,**common),
      'largest_positive_gamma_concentrations_near_spot':metric(positive,**common,near_spot_range_pct=25),
      'largest_negative_gamma_concentrations_near_spot':metric(negative,**common,near_spot_range_pct=25),
      'iv_sensitivity_diagnostic':iv_sensitivity,'model_comparison':comparisons,
      'gamma_reconciliation':gamma_reconciliation(published_gamma_rows or [])}

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

def gex_bulk_snapshot(client,instruments):
    """Fetch a near-atomic full BTC option snapshot from official Deribit bulk endpoints."""
    started=time.time(); instrument_map={i['instrument_name']:i for i in instruments}
    currencies=sorted({i.get('quote_currency') for i in instruments if i.get('quote_currency') in ('BTC','USDC')})
    def books(currency):
        data,_,received=client.get(DERIBIT,'/public/get_book_summary_by_currency',currency=currency,kind='option')
        exchange_time=number(data.get('usOut'),0)/1_000_000 if data.get('usOut') else received
        return currency,data['result'],received,exchange_time
    with ThreadPoolExecutor(max_workers=max(1,len(currencies)+1)) as pool:
        book_jobs=[pool.submit(books,c) for c in currencies]
        index_job=pool.submit(client.get,DERIBIT,'/public/get_index_price',index_name='btc_usd')
        results=[job.result() for job in book_jobs]
        index_data,_,index_received=index_job.result()
    completed=time.time(); index_payload=index_data['result']
    index_time=number(index_data.get('usOut'),0)/1_000_000 if index_data.get('usOut') else index_received
    index_metric=fresh(number(index_payload['index_price'],1e-12),DERIBIT+'/public/get_index_price?index_name=btc_usd',
                       index_time,index_received,'USD',max_age=900)
    books_by_name={}; creation=[]
    for currency,entries,_,_ in results:
        for entry in entries:
            name=entry.get('instrument_name')
            if name in instrument_map and entry.get('base_currency')=='BTC':
                if name in books_by_name: raise ValueError('Duplicate option in bulk summaries: '+name)
                books_by_name[name]=(currency,entry)
                if entry.get('creation_timestamp') is not None: creation.append(number(entry['creation_timestamp'])/1000)
    missing_names=sorted(set(instrument_map)-set(books_by_name)); extra_names=sorted(set(books_by_name)-set(instrument_map))
    rows=[]
    for name,inst in instrument_map.items():
        base={k:inst.get(k) for k in ('instrument_name','option_type','settlement_currency','quote_currency','contract_size','instrument_type')}
        base.update(strike=number(inst['strike'],1e-12),expiry=iso(inst['expiration_timestamp']/1000),index_price=index_metric)
        if name not in books_by_name:
            for key,unit in (('open_interest','BTC'),('mark_iv','volatility percentage points'),
                             ('underlying_price','USD'),('interest_rate','fraction')):
                base[key]=missing(DERIBIT,'Instrument absent from bulk book summary',unit=unit)
            rows.append(base); continue
        currency,entry=books_by_name[name]; t=number(entry.get('creation_timestamp'))/1000
        src=DERIBIT+f'/public/get_book_summary_by_currency?currency={currency}&kind=option'
        for key,unit,low in (('open_interest','BTC',0),('mark_iv','volatility percentage points',1e-12),
                             ('underlying_price','USD',1e-12),('interest_rate','fraction',-1)):
            try: base[key]=fresh(number(entry.get(key),low),src,t,completed,unit,max_age=900)
            except ValueError as e: base[key]=missing(src,str(e),unit=unit)
        rows.append(base)
    exchange_start=min(creation+[index_time]); exchange_end=max(creation+[index_time])
    metadata={'gex_input_snapshot_started_at':iso(started),'gex_input_snapshot_completed_at':iso(completed),
      'gex_input_snapshot_span_seconds':round(completed-started,3),
      'gex_input_exchange_timestamp_span_seconds':round(exchange_end-exchange_start,3),
      'gex_input_method':'Two concurrent Deribit public/get_book_summary_by_currency calls (BTC and USDC) plus public/get_index_price',
      'gex_input_expected_instruments':len(instruments),'gex_input_returned_instruments':len(books_by_name),
      'gex_input_missing_instruments':len(missing_names),'gex_input_extra_instruments':len(extra_names),
      '_calculation_time':exchange_end}
    return rows,metadata

def collect_options(client,budget=720):
    started=time.monotonic()
    data,_,received=client.get(DERIBIT,'/public/get_instruments',currency='any',kind='option',expired='false')
    instruments=[i for i in data['result'] if i.get('base_currency')=='BTC' and i.get('is_active')
                 and i['expiration_timestamp']/1000>received and i.get('option_type') in ('call','put')]
    if not instruments: raise ValueError('No active BTC option instruments')
    if len({i['instrument_name'] for i in instruments})!=len(instruments): raise ValueError('Duplicate instrument names')
    instruments.sort(key=lambda i:(i['expiration_timestamp'],i['instrument_name']))
    gex_rows,gex_metadata=gex_bulk_snapshot(client,instruments)
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
    dealer=dealer_gex_estimate(gex_rows,gex_metadata['_calculation_time'],snapshot_metadata=gex_metadata,
                               published_gamma_rows=rows)
    result={'status':'ok','source':DERIBIT,'inventory_timestamp':iso(received),
            'scope':'All active base_currency=BTC vanilla options; all settlement currencies; combos excluded',
            'contracts':rows,'active_contract_count':len(rows),'by_expiry':{},
            'gex_label':'Gross gamma exposure / GEX proxy; dealer direction unknown',**aggregate(rows),
            'dealer_gex_estimate':{k:v for k,v in dealer.items() if k not in
                ('net_gex_estimate_usd_per_1pct','gex_by_strike','zero_gamma_flip',
                 'zero_gamma_flip_repriced','zero_gamma_flip_cumulative_strike','spot_to_gamma_flip_pct',
                 'gamma_regime','selected_flip_crossing_direction')},
            **{k:dealer[k] for k in ('net_gex_estimate_usd_per_1pct','gex_by_strike','zero_gamma_flip',
                                     'zero_gamma_flip_repriced','zero_gamma_flip_cumulative_strike',
                                     'spot_to_gamma_flip_pct','gamma_regime','selected_flip_crossing_direction',
                                     'crossing_count','gex_model_version','gex_reference_price_method',
                                     'gex_flip_method','gex_input_snapshot_span_seconds')}}
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
