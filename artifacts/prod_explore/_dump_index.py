import json, datetime
from collections import defaultdict

RAW = 'artifacts/raw_dumps/bench_20260530T084048Z/ecom_responses.2465360.jsonl'

def pt(s):
    return datetime.datetime.fromisoformat(s)

# all records in order
RECORDS = []
with open(RAW) as f:
    for line in f:
        RECORDS.append(json.loads(line))

def reads_for_path(substr, op=None):
    """All (ts, ok, content/stdout) for paths containing substr."""
    out = []
    for r in RECORDS:
        if op and r.get('op') != op:
            continue
        req = r.get('request') or {}
        p = req.get('path', '') or ''
        if substr.lower() in p.lower():
            resp = r.get('response') or {}
            c = None
            if isinstance(resp, dict):
                c = resp.get('content') or resp.get('stdout')
            out.append({'ts': r['ts'], 'op': r['op'], 'path': p, 'ok': r['ok'], 'content': c})
    return out

def get_content(substr, op='read'):
    """Return the (first non-empty) content for a path substr."""
    for r in reads_for_path(substr, op):
        if r['ok'] and r['content']:
            return r['content']
    return None

def all_content(substr, op='read'):
    """Distinct non-empty contents for a path substr."""
    seen = {}
    for r in reads_for_path(substr, op):
        if r['ok'] and r['content']:
            seen.setdefault(r['content'], r['ts'])
    return list(seen.items())

def store_inventory(content):
    d = json.loads(content)
    inv = {}
    for row in d.get('inventory', []):
        inv[row['sku']] = row
    return d, inv

def same_day(row):
    return max(row.get('on_hand', 0) - row.get('reserved', 0), 0)
