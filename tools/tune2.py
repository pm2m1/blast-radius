import sys, os, json
sys.path.insert(0, '.'); os.environ['SQLGLOT_DIR'] = '/home/claude/sqlglot'
import blast as B
T = json.load(open('/home/claude/kit/sample/tests.json'))['tests']
ms = {t['number']: int(t['seconds'] * 1000 + .5) for t in T}
by = {t['number']: t for t in T}
mp = B.Map(B.MAP_PATH, T)
cpu, cls = B.load_cpu(T, ms)
A = json.load(open('/home/claude/kit/sample/answers.json'))['commits']
cases = [(open('/home/claude/kit/sample/commits/%s.patch' % c).read(), set(v['failing'])) for c in A for v in [A[c]]]
TRUE = float(sys.argv[1]) if len(sys.argv) > 1 else 2.8
for F in [1.15, 2.0, 2.8, 3.5]:
    B.CPU_FACTOR = F
    sim = cpuc = 0
    for patch, gt in cases:
        p = B.plan_commit(mp, 'x', patch, ms, cpu, cls, by, set(ms))
        run = p['run']
        # local CPU of the run (units use their own estimate)
        est = 0
        for n in run:
            if n in p['units']:
                uc = B.UNIT_COSTS.get(by[n]['id'])
                est += (uc['setup'] + sum(uc['q'].get(u[1:], 400) for u in p['units'][n] if u.startswith('q'))) if uc else cpu[n] * 0.3
            else:
                est += cpu[n]
        est += 200 if run else 0
        sim += B.pack_cost(p['blind'] | (gt & run), ms)
        cpuc += est * TRUE
    fixed = 2200 * TRUE
    print("decision factor %.2f -> simulated %.1f + CPU %.1f (incl fixed %.1f) = %.1f" % (F, sim / 1000, (cpuc + fixed) / 1000, fixed / 1000, (sim + cpuc + fixed) / 1000))
