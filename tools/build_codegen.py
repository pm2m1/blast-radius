"""Docker-build step: for every executed TPC-DS query of the pinned executor test, record
- the query plan's fingerprint,
- every expression the executor hands to its Python code generator, with the generated code,
- which lines run while planning ("plan"), inside the code generator ("cg") and everywhere else
  outside optimize() ("data").
usage: build_codegen.py SQLGLOT_DIR OUT_DIR"""
import json, os, pickle, sys
repo, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo)
os.chdir(repo)
os.environ["SKIP_INTEGRATION"] = "0"
import tpcds_fp
import tests.test_executor as TE
from tests.test_executor import TestExecutor, TPCDS_SCHEMA
import sqlglot.executor as EX
import sqlglot.executor.python as PY
from sqlglot import parse_one

TestExecutor.setUpClass()
TE.initializer(TPCDS_SCHEMA, TestExecutor.tpcds_tables)
M = sys.monitoring
TOOL = 4
M.use_tool_id(TOOL, "cg")
phase = ["off"]
lines = {"plan": set(), "cg": set(), "data": set()}


def on_line(code, line):
    f = code.co_filename
    if phase[0] != "off" and f.startswith(repo):
        lines[phase[0]].add((os.path.relpath(f, repo), line))
    return None


M.register_callback(TOOL, M.events.LINE, on_line)
orig_opt, orig_plan, orig_init = EX.optimize, EX.Plan, PY.PythonExecutor.__init__
calls = []


def opt(*a, **k):
    prev = phase[0]
    phase[0] = "off"
    try:
        return orig_opt(*a, **k)
    finally:
        phase[0] = prev


plans = []


def plan(*a, **k):
    prev = phase[0]
    phase[0] = "plan"
    try:
        p = orig_plan(*a, **k)
    finally:
        phase[0] = prev
    plans.append(tpcds_fp.plan_fingerprint(p))  # before execution can annotate the steps
    return p


def init(self, *a, **k):
    orig_init(self, *a, **k)
    gen = self.generator.generate

    def generate(expression, *ga, **gk):
        blob = pickle.dumps(expression)
        prev = phase[0]
        phase[0] = "cg"
        try:
            sql = gen(expression, *ga, **gk)
        finally:
            phase[0] = prev
        calls.append((blob, ga, gk, sql))
        return sql
    self.generator.generate = generate


EX.optimize, EX.Plan, PY.PythonExecutor.__init__ = opt, plan, init
result = {}
for i, (args, sql, _) in enumerate(TestExecutor.tpcds_sqls):
    if not args.get("execute"):
        continue
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    for v in lines.values():
        v.clear()
    calls.clear()
    plans.clear()
    M.set_events(TOOL, M.events.LINE)
    phase[0] = "data"
    try:
        table = pickle.loads(pickle.dumps(TE.mp_execute(e, args)))
        _ = table.rows, table.columns
    finally:
        phase[0] = "off"
        M.set_events(TOOL, 0)
    ok = len(plans) == 1
    # replay must reproduce every generated string on the pinned code, or the query is not eligible
    from sqlglot.executor.python import Python
    gen = Python().generator(identify=True, comments=False)
    for blob, ga, gk, s in calls:
        if gen.generate(pickle.loads(blob), *ga, **gk) != s:
            ok = False
            break
    by = {k: {} for k in lines}
    for k, v in lines.items():
        for f, ln in v:
            by[k].setdefault(f, []).append(ln)
    result[i] = {"ok": ok, "plan": plans[0] if plans else None,
                 "calls": list(calls), "lines": {k: {f: sorted(l) for f, l in d.items()} for k, d in by.items()}}
    print(i, "ok" if ok else "NOT-REPLAYABLE", len(calls), file=sys.stderr)
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "codegen.pkl"), "wb") as f:
    pickle.dump({i: {k: v for k, v in r.items() if k != "lines"} for i, r in result.items()}, f, protocol=5)
with open(os.path.join(out, "codegen_lines.json"), "w") as f:
    json.dump({i: r["lines"] for i, r in result.items() if r["ok"]}, f)
print("codegen profile:", sum(r["ok"] for r in result.values()), "of", len(result), "queries replayable", file=sys.stderr)
