"""Fail only on corruption, not an honest upstream outage."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from btc_collector.storage import validate, read_history
from btc_collector.core import epoch
s=json.loads(Path('latest.json').read_text())
validate(s)
rows=read_history('history.csv')
assert len({r['hour'] for r in rows})==len(rows),'Duplicate history hours'
assert s['snapshot_hour'] in {r['hour'] for r in rows},'Missing latest history row'
assert epoch(s['timestamp'])>=epoch(s['collection_started_at'])
g=s['options']['dealer_gex_estimate']
for key in ('gex_model_version','gex_reference_price_method','gex_flip_method',
            'gex_input_snapshot_span_seconds','gamma_reconciliation','data_coverage'):
    assert key in g,'Missing signed-GEX diagnostic '+key
net=s['options']['net_gex_estimate_usd_per_1pct']
regime=s['options']['gamma_regime']
if net['status']=='ok' and regime['status']=='ok':
    expected='long_gamma' if net['value']>0 else ('short_gamma' if net['value']<0 else 'zero_gamma')
    assert regime['value']==expected,'Gamma regime disagrees with current signed-GEX sign'
for window in ('utc_session','rolling_24h','rolling_7d'):
    section=s['market_structure'][window]
    for key in ('vwap','poc','vah','val','spot_location'):
        assert key in section,f'Missing market-structure field {window}.{key}'
latest=next(r for r in rows if r['hour']==s['snapshot_hour'])
for key in ('net_gex_estimate','zero_gamma_flip','vwap_session','vwap_24h','vwap_7d',
            'poc_session','poc_24h','poc_7d','spot_location_24h','spot_location_7d'):
    assert key in latest,'Missing history field '+key
print('Output schema, JSON numerics, UTC and history uniqueness: OK')
