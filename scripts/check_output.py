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
print('Output schema, JSON numerics, UTC and history uniqueness: OK')
