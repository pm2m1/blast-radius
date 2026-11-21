"""Per TPC-DS query: lines executed by the executor test OUTSIDE optimize() (runtime phase), and the
fingerprint of the optimized tree.  usage: tpcds_profile.py out.json"""
import os, sys, json, pickle, hashlib
REPO = os.environ.get("SQLGLOT_DIR", "/home/claude/sqlglot")
sys.path.insert(0, REPO)
os.chdir(REPO)
os.environ["SKIP_INTEGRATION"] = "0"
import pandas as pd, numpy as np
from tests.test_executor import TestExecutor, TPCDS_SCHEMA
import tests.test_executor as TE
import sqlglot.executor as EX
from sqlglot import parse_one, find_tables
import tpcds_fp

TestExecutor.setUpClass()
T = TestExecutor.tpcds_tables
TE.initializer(TPCDS_SCHEMA, T)
M = sys.monitoring
TOOL = 3
M.use_tool_id(TOOL, "rt")
cur = set()
active = [True]


def on_line(code, line):
    if active[0]:
        f = code.co_filename
        if f.startswith(REPO):
            cur.add((f, line))
        return M.DISABLE
    return None  # keep it enabled: it may run later while active


M.register_callback(TOOL, M.events.LINE, on_line)
orig_opt = EX.optimize


def opt(*a, **k):
    active[0] = False
    try:
        return orig_opt(*a, **k)
    finally:
        active[0] = True


EX.optimize = opt
rt, fps = {}, {}
for i, (args, sql, _) in enumerate(TestExecutor.tpcds_sqls):
    if not args.get("execute"):
        continue
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    fps[i] = tpcds_fp.fingerprint(e, TPCDS_SCHEMA)
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    cur.clear()
    M.set_events(TOOL, M.events.LINE)
    M.restart_events()
    table = pickle.loads(pickle.dumps(TE.mp_execute(e, args)))
    df = pd.DataFrame(((np.nan if c is None else c for c in r) for r in table.rows), columns=table.columns)
    M.set_events(TOOL, 0)
    by = {}
    for f, ln in cur:
        by.setdefault(os.path.relpath(f, REPO), []).append(ln)
    rt[i] = {k: sorted(v) for k, v in by.items()}
    print(i, sum(len(v) for v in by.values()), file=sys.stderr)
json.dump({"rt": rt, "fp": fps}, open(sys.argv[1], "w"))
