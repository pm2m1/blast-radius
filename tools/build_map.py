import coverage, json, os, sys, glob, gzip, inspect, importlib, pkgutil, collections
REPO = "/home/claude/sqlglot"; OUT = "out"
tests = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]
ids = [t["id"] for t in tests]; idx = {n: i for i, n in enumerate(ids)}
cls_of = {}
byclass = collections.defaultdict(list)
for i, n in enumerate(ids):
    parts = n.split("::"); key = "::".join(parts[:-1]) if len(parts) > 2 else parts[0]
    cls_of[i] = key; byclass[key].append(i)

def ctx_tests(ctx):
    if ctx in ("", "IMPORT"):
        return None
    ctx = ctx.split("@")[0]
    base, _, phase = ctx.partition("|")
    if base not in idx:
        return set()
    i = idx[base]
    return set(byclass[cls_of[i]]) if phase else {i}

data = coverage.CoverageData(basename=os.path.join(OUT, "cov.data"))
comb = coverage.Coverage(data_file=os.path.join(OUT, "combined.data"))
cd = coverage.CoverageData(basename=os.path.join(OUT, "combined.data"))
cd.update  # noqa
files = sorted(glob.glob(os.path.join(OUT, "cov.data.*")))
merged = coverage.CoverageData(basename=os.path.join(OUT, "merged.data"))
for f in files:
    d = coverage.CoverageData(basename=f); d.read(); merged.update(d)
unknown = set()
lines = {}; imp = {}; units = {}
for f in merged.measured_files():
    rel = os.path.relpath(f, REPO)
    cbl = merged.contexts_by_lineno(f)
    lm = {}
    il = []
    for ln, ctxs in cbl.items():
        s = set()
        for c in ctxs:
            t = ctx_tests(c)
            if t is None:
                il.append(ln)
            else:
                if not t and c.split("|")[0] not in idx: unknown.add(c)
                s |= t
        if s:
            lm[ln] = s
    lines[rel] = lm; imp[rel] = sorted(set(il))
    for ln, ctxs in cbl.items():
        for c in ctxs:
            if "@" in c:
                base, unit = c.split("@", 1)
                d = units.setdefault(base, {}).setdefault(rel, {}).setdefault(ln, set())
                d.add(unit)
print("unknown contexts", len(unknown), list(unknown)[:5], file=sys.stderr)
ex = {"opens": collections.defaultdict(set), "inst": collections.defaultdict(set)}
for f in glob.glob(os.path.join(OUT, "extra.*.json")):
    d = json.load(open(f))
    for kind in ("opens", "inst"):
        for c, vals in d[kind].items():
            t = ctx_tests(c) or set()
            for v in vals:
                ex[kind][v] |= t
# MRO for sqlglot classes
sys.path.insert(0, REPO)
import sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try: importlib.import_module(m.name)
    except Exception: pass
mro = {}
for mod in list(sys.modules.values()):
    if not getattr(mod, "__name__", "").startswith("sqlglot"): continue
    for o in vars(mod).values():
        if inspect.isclass(o) and o.__module__.startswith("sqlglot"):
            mro[o.__module__ + ":" + o.__qualname__] = [c.__module__ + ":" + c.__qualname__ for c in o.__mro__ if c.__module__.startswith("sqlglot")]
# dedupe sets
sets = []; sid = {}
def S(s):
    k = tuple(sorted(s))
    if k not in sid: sid[k] = len(sets); sets.append(list(k))
    return sid[k]
out = {"ids": ids,
       "lines": {f: {str(l): S(s) for l, s in lm.items()} for f, lm in lines.items()},
       "imp": imp,
       "opens": {p: S(s) for p, s in ex["opens"].items() if s},
       "inst": {c: S(s) for c, s in ex["inst"].items() if s},
       "mro": mro, "sets": sets}
blob = json.dumps(out, separators=(",", ":")).encode()
gzip.open("map.json.gz", "wb", 9).write(blob)
print("files", len(lines), "sets", len(sets), "raw", len(blob), "gz", os.path.getsize("map.json.gz"))
covered = set()
for lm in lines.values():
    for s in lm.values(): covered |= s
print("tests with any coverage", len(covered), "of", len(ids))
print("no-coverage tests", [ids[i] for i in range(len(ids)) if i not in covered][:20])

# ---- name index and test import graph
import ast
def walk_py(root):
    for dp, dn, fn in os.walk(os.path.join(REPO, root)):
        for f in fn:
            if f.endswith(".py"):
                yield os.path.relpath(os.path.join(dp, f), REPO)
name_lines = collections.defaultdict(lambda: collections.defaultdict(set))
test_imports = {}
for rel in list(walk_py("sqlglot")) + list(walk_py("tests")):
    try:
        tree = ast.parse(open(os.path.join(REPO, rel), encoding="utf-8").read())
    except Exception as e:
        print("skip", rel, e, file=sys.stderr); continue
    deps = set()
    for node in ast.walk(tree):
        n = None
        if isinstance(node, ast.Name): n = node.id
        elif isinstance(node, ast.Attribute): n = node.attr
        elif isinstance(node, ast.alias): n = (node.asname or node.name).split(".")[-1]; ln = getattr(node, "lineno", None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): n = node.name
        elif isinstance(node, ast.keyword) and node.arg: n = node.arg
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) >= 3 \
                and node.value.isidentifier(): n = node.value
        if n and getattr(node, "lineno", None):
            for l in range(node.lineno, (getattr(node, "end_lineno", None) or node.lineno) + 1):
                name_lines[n][rel].add(l)
        if rel.startswith("tests") and isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("tests"):
                    deps.add(node.module)
                    for a in node.names: deps.add(node.module + "." + a.name)
            else:
                for a in node.names:
                    if a.name.startswith("tests"): deps.add(a.name)
    if rel.startswith("tests"):
        test_imports[rel[:-3].replace("/", ".")] = sorted(deps)
name_tests = {}; name_refs = {}
for n, per in name_lines.items():
    s = set()
    for f, ls in per.items():
        lm = lines.get(f, {})
        for l in ls:
            if l in lm: s |= lm[l]
    if s: name_tests[n] = S(s)
    refs = {f: sorted(ls) for f, ls in per.items() if f.startswith("sqlglot/")}
    if refs and sum(len(v) for v in refs.values()) < 4000:
        name_refs[n] = refs
# reverse transitive import graph among test modules
mods = set(test_imports)
rev = collections.defaultdict(set)
for m_, deps in test_imports.items():
    for d in deps:
        if d in mods: rev[d].add(m_)
def closure(m_):
    seen = set(); st = [m_]
    while st:
        x = st.pop()
        for y in rev.get(x, ()):
            if y not in seen: seen.add(y); st.append(y)
    return sorted(seen)
importers = {m_: closure(m_) for m_ in mods}
out["units"] = {b: {f: {str(l): sorted(u) for l, u in lm.items()} for f, lm in per.items()} for b, per in units.items()}
out["import_inst"] = json.load(open("import_inst.json"))
out.update({"name_tests": name_tests, "name_refs": name_refs, "importers": importers, "sets": sets})
blob = json.dumps(out, separators=(",", ":")).encode()
gzip.open("map.json.gz", "wb", 9).write(blob)
print("final raw", len(blob), "gz", os.path.getsize("map.json.gz"), "names", len(name_tests), len(name_refs))
