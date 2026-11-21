"""Record lambdas (and other functions) called while importing every sqlglot and test module.

Output: import_lambdas.json = sorted list of "rel:firstlineno" for <lambda> code objects called at import."""
import json, os, pkgutil, importlib, sys
REPO = os.environ.get("SQLGLOT_DIR", "/home/claude/sqlglot")
sys.path.insert(0, REPO)
os.chdir(REPO)
called = set()


def prof(frame, event, arg):
    if event == "call":
        co = frame.f_code
        if co.co_name == "<lambda>" and co.co_filename.startswith(REPO):
            called.add("%s:%d" % (os.path.relpath(co.co_filename, REPO), co.co_firstlineno))


sys.setprofile(prof)
import sqlglot
mods = ["sqlglot"]
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    mods.append(m.name)
for dp, dn, fn in os.walk(os.path.join(REPO, "tests")):
    for f in fn:
        if f.endswith(".py"):
            rel = os.path.relpath(os.path.join(dp, f), REPO)[:-3].replace("/", ".")
            mods.append(rel[:-9] if rel.endswith(".__init__") else rel)
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        print("fail", m, type(e).__name__, e, file=sys.stderr)
sys.setprofile(None)
json.dump(sorted(called), open(sys.argv[1] if len(sys.argv) > 1 else "/home/claude/work/import_lambdas.json", "w"))
print(len(called), "lambdas called at import", file=sys.stderr)
