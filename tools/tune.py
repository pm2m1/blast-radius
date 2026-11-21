import sys, os, json
sys.path.insert(0, '.'); os.environ['SQLGLOT_DIR'] = '/home/claude/sqlglot'
import blast as B
T = json.load(open('/home/claude/kit/sample/tests.json'))['tests']
ms = {t['number']: int(t['seconds'] * 1000 + .5) for t in T}
by = {t['number']: t for t in T}
mp = B.Map(B.MAP_PATH, T)
cpu, cls = B.load_cpu(T, ms)
A = json.load(open('/home/claude/kit/sample/answers.json'))['commits']
cases = [(open('/home/claude/kit/sample/commits/%s.patch' % c).read(), set(v['failing'])) for c, v in A.items()]
for f in ['mut_B.jsonl', 'mut_C.jsonl', 'mut_D.jsonl', 'mut_G.jsonl', 'mut_F.jsonl']:
    try:
        for l in open(f):
            r = json.loads(l); cases.append((r['patch'], set(r['gt'])))
    except FileNotFoundError:
        pass
plans = [(B.plan_commit(mp, 'x', p, ms, cpu, cls, by, set(ms)), gt) for p, gt in cases]
print(len(plans), 'cases')
for pa in [0.75]:
    B.P_ANY = pa
    tot = 0
    for pl, gt in plans:
        an = pl['an']; cands = pl['cands']
        run, blind = B.choose(cands, ms, cpu, cls, by, an)
        c = sum(cpu[n] for n in run) * 1.15 + (200 if run else 0)
        c += B.pack_cost(blind | (gt & run), ms)
        tot += c
    print('P_ANY', pa, 'total %.1f' % (tot / 1000))

B.P_ANY = 0.75
def best(ns):
    if not ns: return 0
    b = None
    for k in range(1, 9):
        loads = [0] * k
        for m in sorted((ms[n] for n in ns), reverse=True):
            i = loads.index(min(loads)); loads[i] += m
        c = max(loads) + 2000 * k; b = c if b is None or c < b else b
    return b
cat = {}
rows = []
for (pl, gt), (patch, _) in zip(plans, cases):
    an = pl['an']; run, blind = B.choose(pl['cands'], ms, cpu, cls, by, an)
    runc = sum(cpu[n] for n in run) * 1.15 + (200 if run else 0)
    sel = blind | (gt & run)
    c = runc + B.pack_cost(sel, ms); o = best(gt)
    big = [by[n]['id'].split('::')[-1] for n in blind - gt if ms[n] >= 1000]
    key = 'tpcds' if any('tpcds' in b and 'execute' in b for b in big) else ('other-big' if big else ('run-cpu' if runc > 300 else 'small-blind'))
    cat[key] = cat.get(key, 0) + (c - o)
    rows.append((c - o, key, patch.split('\n')[0][13:60], big[:3], len(pl['cands'])))
print({k: round(v / 1000, 1) for k, v in cat.items()})
for r in sorted(rows, reverse=True)[:12]:
    print(round(r[0] / 1000, 1), r[1:])
