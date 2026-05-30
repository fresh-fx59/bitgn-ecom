import json, glob, os, sys
from collections import defaultdict

LOGDIR = 'logs/prod_run3_20260530T100947Z/20260530_100949'

def load(tid):
    fp = f'{LOGDIR}/{tid}__run0.jsonl'
    with open(fp) as f:
        return [json.loads(l) for l in f]

def summary(tid):
    recs = load(tid)
    meta = recs[0]
    out = {'tid': tid, 'intent': meta.get('intent_head',''), 'version': meta.get('agent_version')}
    report = None
    outcome = None
    archs = []
    ops = []   # ecom_op: actual exec/read calls
    for r in recs:
        k = r.get('kind')
        if k == 'step':
            ns = r.get('next_step') or {}
            fn = ns.get('function') if isinstance(ns, dict) else None
            if isinstance(fn, dict) and fn.get('tool') == 'report_completion':
                report = {'ns_state': ns.get('current_state'), 'msg': fn.get('message'),
                          'refs': fn.get('grounding_refs'), 'just': fn.get('outcome_justification'),
                          'rule': fn.get('rulebook_notes')}
        elif k == 'outcome':
            outcome = {'reported': r.get('reported'), 'bypass': r.get('enforcer_bypassed'),
                       'steps': r.get('total_steps'), 'err': r.get('error_msg')}
        elif k == 'arch':
            archs.append((r.get('category'), r.get('result'), (r.get('details') or '')[:120], r.get('reasons')))
        elif k == 'ecom_op':
            ops.append({'op': r.get('op'), 'path': r.get('path'), 'ok': r.get('ok'),
                        'bytes': r.get('bytes'), 'err': r.get('error_code')})
    out['report'] = report
    out['outcome'] = outcome
    out['archs'] = archs
    out['ops'] = ops
    return out

def show(tid, full=False):
    s = summary(tid)
    print(f"\n========== {tid} :: {s['intent'][:100]}")
    o = s['outcome'] or {}
    print(f"OUTCOME: {o.get('reported')}  steps={o.get('steps')} bypass={o.get('bypass')} err={o.get('err')}")
    for cat, res, det, reasons in s['archs']:
        if cat in ('TERMINAL','REFS_DROP','SECURITY','PREPASS') or res=='REJECT' or 'planned' in det:
            print(f"  ARCH {cat} {res or ''}: {det}" + (f"  REASONS={reasons}" if reasons else ''))
    r = s['report']
    if r:
        print(f"  MSG: {r['msg']}")
        print(f"  REFS: {r['refs']}")
        if full:
            print(f"  JUST: {r['just']}")
            print(f"  STATE: {r['ns_state']}")
    print(f"  OPS ({len(s['ops'])}):")
    for op in s['ops']:
        print(f"    {op['op']:8} ok={str(op['ok']):5} err={op['err']} {op['path']}")

if __name__ == '__main__':
    tids = sys.argv[1:]
    full = False
    if tids and tids[0]=='-f':
        full=True; tids=tids[1:]
    if not tids:
        tids = [os.path.basename(p).split('__')[0] for p in sorted(glob.glob(f'{LOGDIR}/t*__run0.jsonl'))]
    for tid in tids:
        show(tid, full)
