"""Pure calculations and explicit UTC/quality semantics; no third-party dependencies."""
from datetime import datetime, timezone
import math

WINDOWS = {'15m': 15, '1h': 60, '4h': 240, '24h': 1440}

def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

def epoch(text):
    d = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timezone is required')
    return d.timestamp()

def number(x, minimum=None, maximum=None):
    if x is None or isinstance(x, bool):
        raise ValueError('Missing or boolean numeric value')
    v = float(x)
    if not math.isfinite(v) or (minimum is not None and v < minimum) or (maximum is not None and v > maximum):
        raise ValueError(f'Invalid numeric value: {x}')
    return v

def metric(value, source, timestamp=None, unit=None, status=None, reason=None, **extra):
    if value is None:
        status = status or 'error'
    else:
        status = status or 'ok'
    return dict(value=value, source=source, timestamp=iso(timestamp) if timestamp is not None else None,
                unit=unit, status=status, **({'reason': reason} if reason else {}), **extra)

def missing(source, reason, status='error', unit=None, **extra):
    return metric(None, source, unit=unit, status=status, reason=reason, **extra)

def check_time(t, now, max_age=180):
    t = number(t, 0)
    if t > now + 30:
        raise ValueError('Source timestamp is in the future (clock skew)')
    if now - t > max_age:
        raise ValueError(f'Stale source: age {now-t:.1f}s exceeds {max_age}s')
    return t

def fresh(value, source, t, now, unit=None, max_age=180, **extra):
    try:
        check_time(t, now, max_age)
    except ValueError as e:
        return metric(None, source, t, unit, status='stale', reason=str(e), **extra)
    return metric(value, source, t, unit, **extra)

def premium(cb, bn, fx=None, max_skew=60):
    def calc(items, converted):
        src = ('Coinbase BTC-USD minus Binance BTCUSDT converted with Coinbase USDT-USD'
               if converted else 'Coinbase BTC-USD minus Binance BTCUSDT (unadjusted)')
        if any(m['status'] != 'ok' or m['value'] is None for m in items):
            return {k: missing(src, 'Fresh inputs unavailable', unit=u) for k,u in
                    [('usd','USD'),('bps','bp'),('pct','%')]}
        ts = [epoch(m['timestamp']) for m in items]
        if max(ts)-min(ts) > max_skew:
            return {k: missing(src, 'Input timestamps differ by more than 60s', status='stale', unit=u) for k,u in
                    [('usd','USD'),('bps','bp'),('pct','%')]}
        base = number(bn['value'], 1e-12) * (number(fx['value'], 1e-12) if converted else 1)
        diff = number(cb['value'], 1e-12)-base
        return {'usd': metric(diff,src,min(ts),'USD',assumption=None if converted else '1 USDT = 1 USD'),
                'bps': metric(diff/base*10000,src,min(ts),'bp'),
                'pct': metric(diff/base*100,src,min(ts),'%')}
    raw = calc([cb,bn],False)
    fx_adjusted = calc([cb,bn,fx],True) if fx else {
        k:missing('Coinbase BTC-USD / Binance BTCUSDT','USDT-USD quote unavailable',unit=u)
        for k,u in [('usd','USD'),('bps','bp'),('pct','%')]}
    # Keep the original usd/bps/fx_adjusted paths while exposing unambiguous names.
    return dict(**raw, fx_adjusted=fx_adjusted,
                raw_coinbase_premium=raw, fx_adjusted_coinbase_premium=fx_adjusted,
                methodology='Price difference, not the CoinGlass Coinbase Premium Index')

def basis(mark, index, source, t, expiry=None):
    m, s = number(mark,1e-12), number(index,1e-12)
    b = m/s-1
    result = {'absolute':metric(m-s,source,t,'quote currency'), 'bps':metric(b*10000,source,t,'bp')}
    if expiry is not None and expiry > t:
        result['annualized_pct'] = metric(b*365.25*86400/(expiry-t)*100,source,t,'%/year',method='simple annualization to expiry')
    else:
        result['annualized_pct'] = missing(source,'Perpetual has no maturity; annualized basis is undefined',status='not_applicable')
    return result

def binance_cvd(klines, end):
    """Exact exchange-aggregated taker volume, not candle-direction inference."""
    candles = {}
    for k in klines:
        t = int(k[0]) // 1000
        if t % 60 or int(k[6]) != (t+60)*1000-1:
            raise ValueError('Unexpected kline timestamp resolution or duration')
        total, buy = number(k[5],0), number(k[9],0)
        quote, buyq = number(k[7],0), number(k[10],0)
        if buy > total+1e-8 or buyq > quote+1e-5:
            raise ValueError('Taker buy volume exceeds total volume')
        row = (2*buy-total, 2*buyq-quote)
        if t in candles and candles[t] != row:
            raise ValueError('Conflicting duplicate kline')
        candles[t] = row
    out = {}
    for name, mins in WINDOWS.items():
        keys = range(end-mins*60,end,60)
        src = 'Binance BTCUSDT /api/v3/klines (taker volumes)'
        if not all(t in candles for t in keys):
            out[name] = missing(src,'Incomplete closed-minute coverage',unit='BTC',coverage_minutes=sum(t in candles for t in keys),expected_minutes=mins)
        else:
            out[name] = metric(sum(candles[t][0] for t in keys),src,end,'BTC',
                quote_cvd=metric(sum(candles[t][1] for t in keys),src,end,'USDT'),
                window_start=iso(end-mins*60),window_end=iso(end),coverage_minutes=mins,expected_minutes=mins)
    return out

def coinbase_cvd(buckets, end, request_ok=True, reason=None):
    out = {}
    for name, mins in WINDOWS.items():
        keys = [str(t) for t in range(end-mins*60,end,60)]
        src = 'Coinbase BTC-USD /products/BTC-USD/trades'
        count = sum(k in buckets for k in keys)
        if not request_ok or count != mins:
            out[name] = missing(src,reason or 'Incomplete trade coverage; warming up or pagination limit',unit='BTC',coverage_minutes=count,expected_minutes=mins)
        else:
            out[name] = metric(sum(buckets[k][0] for k in keys),src,end,'BTC',
                quote_cvd=metric(sum(buckets[k][1] for k in keys),src,end,'USD'),
                window_start=iso(end-mins*60),window_end=iso(end),coverage_minutes=mins,expected_minutes=mins)
    return out

def interpolate_delta(rows, target):
    # Delta and IV from the SAME expiry and option type. Never extrapolate.
    pairs = sorted((number(r['delta']['value'],-1,1),number(r['mark_iv']['value'],1e-12),r['instrument_name'])
                   for r in rows if r['delta']['status']=='ok' and r['mark_iv']['status']=='ok')
    for d, iv, name in pairs:
        if abs(d-target)<1e-10:
            return iv,[name]
    for (d1,v1,n1),(d2,v2,n2) in zip(pairs,pairs[1:]):
        if d1 < target < d2:
            return v1+(v2-v1)*(target-d1)/(d2-d1),[n1,n2]
    raise ValueError('25-delta is not bracketed by valid option quotes')

def gross_gamma(gamma, oi_base, spot):
    # Deribit inverse BTC option OI is already BTC; do NOT multiply by contract_size again.
    return number(gamma,0)*number(oi_base,0)*number(spot,1e-12)**2*0.01

def black_scholes_gamma(spot, strike, volatility, time_to_expiry_years, risk_free_rate=0):
    """Spot gamma for a European option; call and put gamma are identical."""
    s=number(spot,1e-12); k=number(strike,1e-12); sigma=number(volatility,1e-12)
    tau=number(time_to_expiry_years,1e-12); r=number(risk_free_rate)
    root=math.sqrt(tau)
    d1=(math.log(s/k)+(r+0.5*sigma*sigma)*tau)/(sigma*root)
    return math.exp(-0.5*d1*d1)/(math.sqrt(2*math.pi)*s*sigma*root)

def signed_dealer_gamma(gamma, oi_base, spot, option_type):
    """Estimated dealer GEX under the explicit short-call / long-put assumption."""
    if option_type not in ('call','put'):
        raise ValueError('Option type must be call or put')
    sign=-1 if option_type=='call' else 1
    return sign*gross_gamma(gamma,oi_base,spot)

def oi_changes(current, points, source, tolerance=1200):
    result = {}
    for hours in (1,4,24):
        name=f'{hours}h'
        if current['status']!='ok':
            result[name]=missing(source,'Current OI unavailable',unit='%')
            continue
        now=epoch(current['timestamp']); target=now-hours*3600
        eligible=[p for p in points if p[0]<now and p[1]>0 and abs(p[0]-target)<=tolerance]
        if not eligible:
            result[name]=missing(source,'No same-venue baseline within 20 minutes; warming up or history gap',unit='%')
            continue
        old_t,old=min(eligible,key=lambda p:abs(p[0]-target))
        result[name]=metric((current['value']/old-1)*100,source,now,'%',baseline_timestamp=iso(old_t),
                            baseline_value=old,actual_lookback_seconds=now-old_t,absolute_change=current['value']-old)
    return result
