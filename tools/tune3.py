import sys, os, json
sys.path.insert(0, '.'); os.environ['SQLGLOT_DIR'] = '/home/claude/sqlglot'
import blast as B
T = json.load(open('/home/claude/kit/sample/tests.json'))['tests']
ms = {t['number']: int(t['seconds'] * 1000 + .5) for t in T}
by = {t['number']: t for t in T}
mp = B.Map(B.MAP_PATH, T)
cpu, cls = B.load_cpu(T, ms)
A = json.load(open('/home/claude/kit/sample/answers.json'))['commits']
cases = [(open('/home/claude/kit/sample/commits/%s.patch' % c).read(), set(A[c]['failing'])) for c in A]
big = {n for n in ms if ms[n] > 15000}
for f in ['mut_B.jsonl', 'mut_C.jsonl', 'mut_D.jsonl', 'mut_G.jsonl']:
    try:
        for l in open(f):
            r = json.loads(l); cases.append((r['patch'], set(r["gt"])))
    except FileNotFoundError:
        pass
TRUE = 2.8
plans = []
for patch, gt in cases:
    an, nt = B.analyze_commit(mp, patch)
    plans.append((an, gt))
for pa in [0.4, 0.55, 0.65, 0.75, 0.85]:
    B.P_ANY = pa
    sim = cp = 0
    for an, gt in plans:
        cands = set(ms) if an.everything else set(an.cands)
        run, blind = B.choose(cands, ms, cpu, cls, by, an)
        sim += B.pack_cost(blind | (gt & run), ms)
        cp += (sum(cpu[n] for n in run) + (60 if run else 0)) * TRUE
    print("P_ANY %.2f  sim %.1f  cpu %.1f  total %.1f" % (pa, sim / 1000, cp / 1000, (sim + cp) / 1000))
