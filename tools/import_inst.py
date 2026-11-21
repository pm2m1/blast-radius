"""Classes instantiated while importing every sqlglot module (offline map step)."""
import sys, os, json, inspect, importlib, pkgutil
sys.path.insert(0, os.environ.get("MAPPER_REPO", "/home/claude/sqlglot"))
seen = set()
import sqlglot  # noqa
from sqlglot.expressions import core  # noqa
from sqlglot.dialects.dialect import Dialect
from sqlglot import tokens, parser, generator
rec = set()
def wrap(cls):
    orig = cls.__dict__["__init__"]
    def __init__(self, *a, **k):
        t = type(self); rec.add(t.__module__ + ":" + t.__qualname__)
        return orig(self, *a, **k)
    cls.__init__ = __init__
for c in (core.Expr if hasattr(core, "Expr") else core.Expression, Dialect, tokens.Tokenizer, parser.Parser, generator.Generator):
    if "__init__" in c.__dict__:
        wrap(c)
orig = Dialect.__dict__["get_or_raise"].__func__
def g(cls, *a, **k):
    d = orig(cls, *a, **k)
    t = d if isinstance(d, type) else type(d); rec.add(t.__module__ + ":" + t.__qualname__)
    return d
Dialect.get_or_raise = classmethod(g)
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if m.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(m.name)
    except Exception as e:
        print("skip", m.name, e, file=sys.stderr)
json.dump(sorted(rec), open(sys.argv[1], "w"))
print(len(rec), "classes instantiated at import", file=sys.stderr)
