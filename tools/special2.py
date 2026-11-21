import os, sys, json, re, time
sys.argv = [sys.argv[0]]
sys.path.insert(0, "/home/claude/work")
import mutate as M
B = M.B
R = M.REPO
def rd(rel): return open(os.path.join(R, rel), encoding="utf-8").read()
def sub1(rel, a, b):
    t = rd(rel); assert a in t, (rel, a); return M.make_patch(rel, t, t.replace(a, b, 1))
cases = {}
t = rd("sqlglot/dialects/bigquery.py")
m = re.search(r"\n        QUOTES = .*\n", t)
if m: cases["tokenizer-QUOTES"] = M.make_patch("sqlglot/dialects/bigquery.py", t, t.replace(m.group(0), m.group(0).replace('"\'"', '"`"', 1), 1))
t = rd("sqlglot/expressions/ddl.py")
m = re.search(r'arg_types = \{"this": True', t)
if m: cases["arg_types-required"] = M.make_patch("sqlglot/expressions/ddl.py", t, t.replace('arg_types = {"this": True', 'arg_types = {"this": False', 1))
t = rd("sqlglot/parser.py")
m = re.search(r'\n    __slots__ = \(\n(\s+)"(\w+)",', t)
if m: cases["parser-__slots__"] = M.make_patch("sqlglot/parser.py", t, t.replace('"%s",' % m.group(2), '"%sX",' % m.group(2), 1))
cases["enum-value"] = sub1("sqlglot/dialects/dialect.py", 'BIGQUERY = "bigquery"', 'BIGQUERY = "bigqueryx"')
t = rd("sqlglot/diff.py")
m = re.search(r"@dataclass\(frozen=True\)\nclass (\w+):\n(    \"\"\".*?\"\"\"\n)?    (\w+): ", t, re.S)
if m: cases["dataclass-field"] = M.make_patch("sqlglot/diff.py", t, t.replace("    %s: " % m.group(3), "    %sx: " % m.group(3), 1))
mp = B.Map(B.MAP_PATH, M.TESTS); by_number = {t["number"]: t for t in M.TESTS}
cpu_ms, class_ms = B.load_cpu(M.TESTS, M.MS); runner = B.Runner(M.TESTS)
for name, patch in cases.items():
    if not patch: print(name, "skipped"); continue
    sel, an, nt, cands, cpu = M.select_one(mp, runner, patch, cpu_ms, class_ms, by_number)
    gt, err = M.ground_truth(runner, nt)
    miss = sorted((gt or set()) - sel)
    print("%-20s gt=%-4d sel=%-4d cands=%-4d cost=%5.1f MISS=%s %s" % (name, len(gt or ()), len(sel), len(cands), B.pack_cost(sel, M.MS) / 1000, miss[:6], str(an.reasons[:3])[:110]), flush=True)
