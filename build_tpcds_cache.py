"""Docker-build step: DuckDB's expected result for every TPC-DS query, keyed by the exact
DuckDB SQL that the pinned sqlglot produces. At runtime it is used only when the patched
sqlglot transpiles a query to the identical SQL string; otherwise DuckDB runs for real."""
import os, pickle, sys
repo = sys.argv[1]
out = sys.argv[2]
os.environ.setdefault("SKIP_INTEGRATION", "0")
sys.path.insert(0, repo)
os.chdir(repo)
import tests.test_executor as te
from sqlglot import transpile
te.TestExecutor.setUpClass()
cls = te.TestExecutor
cache = {}
duck = {}  # query index -> the DuckDB SQL the pinned sqlglot produces
for i, (args, sql, _) in enumerate(cls.tpcds_sqls):
    try:
        duck_sql = transpile(sql, write="duckdb")[0]
        cache[duck_sql] = cls.tpcds_conn.execute(duck_sql).fetchdf()
        duck[i] = duck_sql
    except Exception as e:  # not compared at the pinned commit; runtime falls back to DuckDB
        print("skip query", i + 1, type(e).__name__, file=sys.stderr)
# Fingerprints of the optimized tree per executed query (the exact call the executor makes).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pickle as _p
import tpcds_fp
from sqlglot import parse_one
from tests.helpers import TPCDS_SCHEMA
fps = {}
for i, (args, sql, _) in enumerate(cls.tpcds_sqls):
    if args.get("execute"):
        try:
            fps[i] = tpcds_fp.fingerprint(_p.loads(_p.dumps(parse_one(sql))), TPCDS_SCHEMA)
        except Exception as e:
            print("no fingerprint", i, type(e).__name__, file=sys.stderr)
cache["__fp__"] = fps
cache["__duck__"] = duck
with open(out, "wb") as f:
    pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
print("tpcds cache:", len(cache), "queries", os.path.getsize(out), "bytes", file=sys.stderr)
