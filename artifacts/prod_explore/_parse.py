import json, glob, os, datetime, re
from collections import defaultdict

LOGDIR = 'logs/prod_run1_20260530T084046Z/20260530_084048'
RAW = 'artifacts/raw_dumps/bench_20260530T084048Z/ecom_responses.2465360.jsonl'

def parse_ts(s):
    return datetime.datetime.fromisoformat(s)

# ---- per-task trace extraction ----
tasks = {}
order = []
for fp in sorted(glob.glob(f'{LOGDIR}/t*__run0.jsonl')):
    tid = os.path.basename(fp).split('__')[0]
    order.append(tid)
    with open(fp) as f:
        recs = [json.loads(l) for l in f]
    meta = recs[0]
    task_text = None
    reads = []
    report = None
    outcome = None
    enforcer_bypassed = None
    for r in recs:
        k = r.get('kind')
        if k == 'task':
            task_text = r.get('task_text')
        elif k == 'ecom_op':
            reads.append({'op': r.get('op'), 'path': r.get('path'), 'ok': r.get('ok'),
                          'bytes': r.get('bytes'), 'origin': r.get('origin'), 'err': r.get('error_code')})
        elif k == 'step':
            ns = r.get('next_step') or {}
            fn = ns.get('function') if isinstance(ns, dict) else None
            if isinstance(fn, dict) and 'outcome' in fn and 'message' in fn:
                report = fn
        elif k == 'outcome':
            outcome = r
            enforcer_bypassed = r.get('enforcer_bypassed')
    tasks[tid] = {
        'tid': tid,
        'started_at': meta['started_at'],
        'task_text': task_text or meta.get('intent_head', ''),
        'reads': reads,
        'report': report,
        'outcome': outcome,
        'enforcer_bypassed': enforcer_bypassed,
    }

# build time windows
windows = []
for i, tid in enumerate(order):
    start = parse_ts(tasks[tid]['started_at'])
    end = parse_ts(tasks[order[i+1]]['started_at']) if i+1 < len(order) else datetime.datetime.max.replace(tzinfo=start.tzinfo)
    windows.append((tid, start, end))

def task_for_ts(ts_str):
    ts = parse_ts(ts_str)
    for tid, s, e in windows:
        if s <= ts < e:
            return tid
    return None

# ---- raw dump: index reads by task ----
raw_by_task = defaultdict(list)  # tid -> list of {path, content, op, stdout, ok}
with open(RAW) as f:
    for line in f:
        rec = json.loads(line)
        tid = task_for_ts(rec['ts'])
        if tid is None:
            continue
        req = rec.get('request') or {}
        resp = rec.get('response') or {}
        entry = {
            'op': rec.get('op'),
            'ok': rec.get('ok'),
            'path': req.get('path') or (resp.get('path') if isinstance(resp, dict) else None),
            'ts': rec['ts'],
        }
        if isinstance(resp, dict):
            entry['content'] = resp.get('content')
            entry['stdout'] = resp.get('stdout')
        raw_by_task[tid].append(entry)

if __name__ == '__main__':
    # quick sanity dump
    for tid in order:
        t = tasks[tid]
        rep = t['report'] or {}
        print(tid, '|', t['outcome']['reported'] if t['outcome'] else '?', '|',
              repr((rep.get('message') or '')[:60]), '| reads=', len(raw_by_task[tid]))
