import json, glob, os, datetime
from collections import defaultdict

LOGDIR = 'logs/prod_run3_20260530T100947Z/20260530_100949'
RAW = 'artifacts/raw_dumps/bench_20260530T100949Z/ecom_responses.2496296.jsonl'

def parse_ts(s):
    return datetime.datetime.fromisoformat(s)

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
    refs = None
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
            refs = r.get('grounding_refs') or r.get('refs')
    tasks[tid] = {
        'tid': tid,
        'started_at': meta['started_at'],
        'task_text': task_text or meta.get('intent_head', ''),
        'reads': reads,
        'report': report,
        'outcome': outcome,
        'refs': refs,
    }

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

raw_by_task = defaultdict(list)
if os.path.exists(RAW):
    with open(RAW) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
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
    for tid in order:
        t = tasks[tid]
        rep = t['report'] or {}
        oc = t['outcome'] or {}
        rep_oc = oc.get('reported') or oc.get('outcome') or (rep.get('outcome'))
        print(tid, '|', rep_oc, '|', repr((rep.get('message') or '')[:70]), '| refs=', len(t['refs'] or []), '| reads=', len(raw_by_task[tid]))
