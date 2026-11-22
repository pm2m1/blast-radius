"""Map from the pinned commit: dialect enum value -> every sqlglot source module whose code can run when
that dialect's generator is constructed (class ancestry of dialect/generator/parser/tokenizer + their
transitive sqlglot imports)."""
import ast, json, os, sys, inspect
repo = sys.argv[1]
sys.path.insert(0, repo)
from sqlglot.dialects.dialect import Dialect, Dialects

def rel_of(obj):
    try:
        f = inspect.getsourcefile(obj)
    except TypeError:
        return None
    return os.path.relpath(f, repo) if f and f.startswith(repo) else None

imp_cache = {}
def imports(rel):
    if rel in imp_cache:
        return imp_cache[rel]
    out = set()
    try:
        tree = ast.parse(open(os.path.join(repo, rel)).read())
    except Exception:
        imp_cache[rel] = out
        return out
    pkg = rel.rsplit("/", 1)[0].replace("/", ".")
    for n in ast.walk(tree):
        mods = []
        if isinstance(n, ast.Import):
            mods = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = pkg.split(".")[: len(pkg.split(".")) - (n.level - 1)]
                base = ".".join(parts + ([base] if base else []))
            mods = [base] + [base + "." + a.name for a in n.names]
        for m in mods:
            if not m.startswith("sqlglot"):
                continue
            p = m.replace(".", "/")
            for cand in (p + ".py", p + "/__init__.py"):
                if os.path.exists(os.path.join(repo, cand)):
                    out.add(cand)
    imp_cache[rel] = out
    return out

result = {}
for d in Dialects:
    if not d.value:
        continue
    cls = Dialect.get_or_raise(d.value).__class__
    roots = set()
    for k in (cls, cls.generator_class, cls.parser_class, cls.tokenizer_class):
        for c in k.__mro__:
            r = rel_of(c)
            if r:
                roots.add(r)
    seen, todo = set(), list(roots)
    while todo:
        r = todo.pop()
        if r in seen:
            continue
        seen.add(r)
        todo.extend(imports(r) - seen)
    result[d.value] = sorted(seen)
print(json.dumps(result))
