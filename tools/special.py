import os, sys, json, re, time
sys.argv = [sys.argv[0]]
import mutate as M
B = M.B
R = M.REPO


def rd(rel):
    return open(os.path.join(R, rel), encoding="utf-8").read()


def sub1(rel, a, b, count=1):
    t = rd(rel)
    assert a in t, (rel, a)
    return M.make_patch(rel, t, t.replace(a, b, count))


cases = {}
# 1. class-level annotation that breaks import
cases["bad-annotation"] = sub1("sqlglot/dialects/tsql.py", "    WEEK_OFFSET = -1", "    WEEK_OFFSET: UndefinedThing = -1")
# 2. remove an import used inside functions
t = rd("sqlglot/optimizer/eliminate_joins.py")
imp = [l for l in t.split("\n") if l.startswith("from ") or l.startswith("import ")]
cases["remove-import"] = M.make_patch("sqlglot/optimizer/eliminate_joins.py", t, t.replace(imp[-1] + "\n", "", 1))
# 3. delete a module-level helper used by others
t = rd("sqlglot/optimizer/helpers.py") if os.path.exists(os.path.join(R, "sqlglot/optimizer/helpers.py")) else None
if t:
    m = re.search(r"\ndef (\w+)\(", t)
    name = m.group(1)
    start = m.start() + 1
    end = t.find("\ndef ", start + 5)
    cases["delete-func " + name] = M.make_patch("sqlglot/optimizer/helpers.py", t, t[:start] + t[end + 1:])
# 4. rename a generator method (dispatch by name)
cases["rename-method"] = sub1("sqlglot/generators/clickhouse.py", "def timestamptrunc_sql(", "def timestamptrunc_sql_old(")
# 5. docstring-only
cases["docstring"] = sub1("sqlglot/transforms.py", '"""https://docs.oracle.com', '"""See https://docs.oracle.com')
# 6. whitespace-only
t = rd("sqlglot/parser.py")
cases["blank-lines"] = M.make_patch("sqlglot/parser.py", t, t.replace("\n\n\n", "\n\n\n\n", 3))
# 7. README
cases["readme"] = sub1("README.md", "SQLGlot", "SQLGlot!")
# 8. new fixture file
cases["new-fixture"] = "diff --git a/tests/fixtures/optimizer/new_case.sql b/tests/fixtures/optimizer/new_case.sql\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/tests/fixtures/optimizer/new_case.sql\n@@ -0,0 +1,2 @@\n+SELECT 1;\n+SELECT 1;\n"
# 9. helpers.py change
cases["tests-helpers"] = sub1("tests/helpers.py", 'if not line.startswith("#"):', 'if not line.startswith("##"):')
# 10. comment with backslash continuation breaking syntax
cases["comment-in-string"] = sub1("sqlglot/generators/sqlite.py", "def jsonextractscalar_sql(self, expression: exp.JSONExtractScalar) -> str:\n", "def jsonextractscalar_sql(self, expression: exp.JSONExtractScalar) -> str:\n        # Cheap check first\n")
# 11. module-level constant change
t = rd("sqlglot/time.py")
cases["module-const"] = None
for line in t.split("\n"):
    if re.match(r"^[A-Z_]+ = ", line):
        cases["module-const"] = M.make_patch("sqlglot/time.py", t, t.replace(line, line + "  # x", 1))
        break
# 12. decorator removed
t = rd("sqlglot/expressions/core.py")
if "    @property\n" in t:
    cases["remove-decorator"] = M.make_patch("sqlglot/expressions/core.py", t, t.replace("    @property\n", "", 1))
# 13. default argument change
t = rd("sqlglot/optimizer/simplify.py")
m = re.search(r"def \w+\([^)]*=True", t)
if m:
    cases["default-arg"] = M.make_patch("sqlglot/optimizer/simplify.py", t, t.replace(m.group(0), m.group(0)[:-4] + "False", 1))
# 14. test method change
cases["test-change"] = sub1("tests/test_transpile.py", "def test_", "def test_", 1)

only = set(os.environ.get("ONLY", "").split(",")) - {""}
if only:
    cases = {k: v for k, v in cases.items() if any(k.startswith(o) for o in only)}
mp = B.Map(B.MAP_PATH, M.TESTS)
by_number = {t["number"]: t for t in M.TESTS}
cpu_ms, class_ms = B.load_cpu(M.TESTS, M.MS)
runner = B.Runner(M.TESTS)
for name, patch in cases.items():
    if not patch:
        print(name, "skipped"); continue
    sel, an, nt, cands, cpu = M.select_one(mp, runner, patch, cpu_ms, class_ms, by_number)
    gt, err = M.ground_truth(runner, nt)
    miss = sorted((gt or set()) - sel)
    print("%-22s gt=%-4d sel=%-4d cands=%-4d cost=%5.1f MISS=%s %s" % (name, len(gt or ()), len(sel), len(cands),
          B.pack_cost(sel, M.MS) / 1000, miss[:6], ("ALL " if an.everything else "") + str(an.reasons[:3])[:120]), flush=True)
