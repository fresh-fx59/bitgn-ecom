import sys, json, re
sys.path.insert(0, 'artifacts/prod_explore')
from _parse import tasks, order, raw_by_task

def content_for(tid, path_substr=None, op=None, want_stdout=False):
    """Return list of (path, content_or_stdout) for a task's raw reads matching filter."""
    out = []
    seen = set()
    for e in raw_by_task[tid]:
        if op and e['op'] != op:
            continue
        p = e.get('path') or ''
        if path_substr and path_substr.lower() not in p.lower():
            continue
        val = e.get('stdout') if want_stdout else e.get('content')
        if val is None:
            val = e.get('content') or e.get('stdout')
        key = (p, e.get('ts'))
        if key in seen:
            continue
        seen.add(key)
        out.append((p, val))
    return out

def list_paths(tid):
    paths = []
    seen = set()
    for e in raw_by_task[tid]:
        p = e.get('path')
        if p and p not in seen:
            seen.add(p)
            paths.append((e['op'], p, e['ok'], bool(e.get('content') or e.get('stdout'))))
    return paths

def show(tid, path_substr=None, maxlen=3000, op=None):
    print(f'=== {tid} : {tasks[tid]["task_text"][:90]} ===')
    for p, c in content_for(tid, path_substr, op):
        print(f'--- {p} ---')
        print((c or '')[:maxlen])

if __name__ == '__main__':
    tid = sys.argv[1]
    sub = sys.argv[2] if len(sys.argv) > 2 else None
    ml = int(sys.argv[3]) if len(sys.argv) > 3 else 3000
    if sub == 'LS':
        for op, p, ok, hasc in list_paths(tid):
            print(f'{op:6} ok={ok} content={hasc} {p}')
    else:
        show(tid, sub, ml)
