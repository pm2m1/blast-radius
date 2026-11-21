import sys, json, os, random, time
os.environ["SELECT_DEBUG"] = "1"
import mutate as M
import realgen
B = M.B
random.seed(int(sys.argv[2]))
N = int(sys.argv[1])
mp = B.Map(B.MAP_PATH, M.TESTS); bn = {t["number"]: t for t in M.TESTS}
cpu_ms, class_ms = B.load_cpu(M.TESTS, M.MS)
runner = B.Runner(M.TESTS)
TP = [n for n in M.MS if bn[n]["id"].endswith("::test_execute_tpcds")][0]
out = open(sys.argv[3], "a")
tried = 0
while tried < N:
    rel = random.choice(M.SQL_PY)
    old = open(os.path.join(B.REPO, rel)).read()
    new, mk = realgen.mutate(old)
    if not new:
        continue
    patch = M.make_patch(rel, old, new)
    if not patch:
        continue
    cap = {}
    p = B.plan_commit(mp, "x", patch, M.MS, cpu_ms, class_ms, bn, set(M.MS))
    an = p["an"]
    if TP not in p["cands"]:
        continue
    reason = [r for r in an.reasons if r.startswith("tpcds-fp")]
    if not reason or not reason[0].startswith("tpcds-fp E=0"):
        out.write(json.dumps({"mk": mk, "rel": rel, "fp": None, "reason": reason, "non_body": an.non_body}) + "\n"); out.flush()
        continue
    tried += 1
    us = [u for u in B.Map.affected_units(mp, TP, an.body_lines)]
    units = {TP: ["f%s" % u[1:] for u in us] + ["b0"]}
    t0 = time.time()
    pid, _ = runner.start({TP}, p["new"], an.swap, an.reimport, time.time() + 60, units, False)
    os.waitpid(pid, 0)
    res = json.loads(B.read_all(pid) or b"{}")
    eq = TP not in set(res.get("failed", []))
    rec = {"mk": mk, "rel": rel, "fp": eq, "nq": len(us), "cpu": res.get("cpu"), "secs": round(time.time() - t0, 2), "patch": patch}
    out.write(json.dumps(rec) + "\n"); out.flush()
    print(tried, mk, rel, "EQUAL" if eq else "differ", len(us), res.get("cpu"), flush=True)
