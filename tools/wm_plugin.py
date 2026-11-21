"""Map-build only: weak-mutation profile of every single-operator comparison in sqlglot.

Each `a OP b` is rewritten to __wm__(site, OP, a, b): the original result is returned unchanged,
and for every alternative operator ALT we record, per test, whether `a ALT b` ever gave a different
truth value or raised differently. If a patch swaps OP for ALT at a site and a test's run never saw
a difference there, the patched run of that test is step-for-step identical (deterministic runs).
"""
import ast
import builtins
import importlib.abc
import importlib.util
import json
import operator
import os
import sys

REPO = os.environ["MAPPER_REPO"]
OUT = os.environ["MAPPER_OUT"]

OPS = ["Lt", "LtE", "Gt", "GtE", "Eq", "NotEq", "Is", "IsNot", "In", "NotIn"]
FN = {
    "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
    "Eq": operator.eq, "NotEq": operator.ne, "Is": operator.is_, "IsNot": operator.is_not,
    "In": lambda a, b: a in b, "NotIn": lambda a, b: a not in b,
}
REL = ["Lt", "LtE", "Gt", "GtE", "Eq", "NotEq"]
ALTS = {}
for o in OPS:
    if o in REL:
        alts = [x for x in REL if x != o] + (["Is", "IsNot"] if o in ("Eq", "NotEq") else [])
    elif o in ("Is", "IsNot"):
        alts = ["IsNot" if o == "Is" else "Is", "Eq", "NotEq"]
    else:
        alts = ["NotIn" if o == "In" else "In"]
    ALTS[o] = alts
IDX = {o: i for i, o in enumerate(OPS)}
FULL = {o: sum(1 << IDX[a] for a in ALTS[o]) for o in OPS}
RAISED = 1 << 15  # original comparison raised: every alternative counts as different

_state = {"ctx": "IMPORT"}
_flips = {}        # ctx -> {site_index: mask}
_sites = []        # site_index -> (rel, lineno, col, op)
_M = [0] * 200000  # current context's masks, indexed by site
_touched = []


def _flush():
    if _touched:
        d = _flips.setdefault(_state["ctx"], {})
        for i in _touched:
            d[i] = d.get(i, 0) | _M[i]
            _M[i] = 0
        _touched.clear()


def _set_ctx(ctx):
    _flush()
    _state["ctx"] = ctx


_busy = [False]  # True while evaluating alternatives: nested comparisons are not program execution


def _slow(i, op, a, b, r):
    _busy[0] = True
    try:
        _slow2(i, op, a, b, r)
    finally:
        _busy[0] = False


CUSTOM_EQ = 1 << 13  # an operand had a sqlglot-defined __eq__ (may have side effects, e.g. hash caching)
_EQ_OPS = ("Eq", "NotEq", "Is", "IsNot")


def _custom_eq(x):
    f = getattr(type(x), "__eq__", None)
    mod = getattr(f, "__module__", None) or ""
    return mod.startswith("sqlglot") or mod.startswith("tests")


def _slow2(i, op, a, b, r):
    m = _M[i]
    if m == 0:
        _touched.append(i)
        m = 1 << 14  # reached
    if op in _EQ_OPS and (m & CUSTOM_EQ or _custom_eq(a) or _custom_eq(b)):
        m |= CUSTOM_EQ
        if op in ("Is", "IsNot"):
            # never call a sqlglot __eq__ the real run would not call: it caches hashes, which would
            # make this profiled run diverge from the real one. Count == / != as different.
            m |= (1 << IDX["Eq"]) | (1 << IDX["NotEq"])
    rb = r if r is True or r is False else None
    for alt in ALTS[op]:
        bit = 1 << IDX[alt]
        if m & bit:
            continue
        try:
            r2 = FN[alt](a, b)
            if rb is None or not (r2 is True or r2 is False) or r2 is not rb:
                m |= bit
        except BaseException:
            m |= bit
    _M[i] = m


def _raised(i, op):
    if _M[i] == 0:
        _touched.append(i)
    _M[i] |= FULL[op] | RAISED | (1 << 14)


def _make(op, f):
    full = FULL[op] | (1 << 14)

    def w(i, a, b):
        if _M[i] == full or _busy[0]:
            return f(a, b)
        try:
            r = f(a, b)
        except BaseException:
            _raised(i, op)
            raise
        _slow(i, op, a, b, r)
        return r
    return w


for _op in OPS:
    setattr(builtins, "__wm_%s__" % _op, _make(_op, FN[_op]))


class Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        i = len(_sites)
        _sites.append((self.rel, node.lineno, node.col_offset, op))
        call = ast.Call(func=ast.Name(id="__wm_%s__" % op, ctx=ast.Load()),
                        args=[ast.Constant(i), node.left, node.comparators[0]],
                        keywords=[])
        return ast.copy_location(call, node)


class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not (spec.origin or "").endswith(".py"):
            return spec
        spec.loader = self
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        origin = module.__spec__.origin
        rel = os.path.relpath(origin, REPO)
        src = open(origin, encoding="utf-8").read()
        tree = Rewriter(rel).visit(ast.parse(src, origin))
        ast.fix_missing_locations(tree)
        module.__file__ = origin
        exec(compile(tree, origin, "exec", dont_inherit=True), module.__dict__)


sys.meta_path.insert(0, Finder())

import pytest  # noqa: E402


def pytest_configure(config):
    import syncpool
    syncpool.install()
    import importlib
    import pkgutil
    import sqlglot
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not m.name.endswith("__main__"):
            try:
                importlib.import_module(m.name)
            except Exception as e:
                print("wm: cannot import", m.name, e, file=sys.stderr)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set_ctx(item.nodeid + "|setup"); yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set_ctx(item.nodeid); yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _set_ctx(item.nodeid + "|teardown"); yield
    _set_ctx("IMPORT")


def pytest_unconfigure(config):
    _flush()
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    seed = os.environ.get("PYTHONHASHSEED", "x")
    with open(os.path.join(OUT, "wm.%s.%s.json" % (wid, seed)), "w") as f:
        json.dump({"flips": _flips, "sites": _sites}, f)
