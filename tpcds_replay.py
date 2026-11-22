"""Read-only, module-scoped checkpoints from the pinned executor test."""

import ast
import hashlib
from enum import Enum
from functools import lru_cache
import os
import pickle
import time


# These optimizer passes operate on the expression. Other arguments in the
# executor's calls are immutable. No schema, journal, or connection is replayed.
RULES = frozenset(
    (
        "eliminate_ctes",
        "eliminate_joins",
        "eliminate_subqueries",
        "merge_subqueries",
        "normalize",
        "optimize_joins",
        "pushdown_predicates",
        "pushdown_projections",
        "unnest_subqueries",
    )
)
FIXTURE = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
CACHE_DIR = os.environ.get(
    "TPCDS_REPLAY_DIR", os.path.join(os.path.dirname(__file__), "ds_replay")
)


def snapshot(tree, kwargs):
    import copyreg
    import io
    from sqlglot import exp

    class SaveSlots(pickle.Pickler):
        def reducer_override(self, obj):
            if isinstance(obj, exp.Expr):
                state = {}
                for cls in type(obj).__mro__:
                    for attr in cls.__dict__.get("__slots__", ()):
                        if attr not in ("__weakref__", "__dict__") and hasattr(
                            obj, attr
                        ):
                            state[attr] = (
                                None if attr == "_hash" else getattr(obj, attr)
                            )
                return (
                    copyreg.__newobj__,
                    (type(obj),),
                    (vars(obj) if hasattr(obj, "__dict__") else None, state),
                )
            return NotImplemented

    stream = io.BytesIO()
    SaveSlots(stream, protocol=5).dump((tree, kwargs))
    return stream.getvalue()


def tree_digest(tree):
    """Check structure, types, metadata, argument order and parent links.
    Equality of SQL text alone loses distinctions the Python executor uses.
    """
    from sqlglot import exp

    def freeze(node):
        if isinstance(node, exp.Expr):
            args = []
            for key, value in node.args.items():
                children = value if type(value) is list else (value,)
                for i, child in enumerate(children):
                    if isinstance(child, exp.Expr):
                        if child.parent is not node or child.arg_key != key:
                            raise ValueError("inconsistent expression parent")
                        if type(value) is list and child.index != i:
                            raise ValueError("inconsistent expression index")
                args.append((key, freeze(value)))
            dtype = node._type
            return (
                type(node).__module__,
                type(node).__qualname__,
                tuple(args),
                "self" if dtype is node else freeze(dtype),
                freeze(node.comments),
                freeze(node._meta),
            )
        if type(node) in (list, tuple):
            return (type(node).__name__, tuple(freeze(x) for x in node))
        if type(node) is dict:
            return ("dict", tuple((freeze(k), freeze(v)) for k, v in node.items()))
        if node is None or type(node) in (str, int, bool, float, bytes):
            return (type(node).__name__, node)
        if isinstance(node, Enum):
            return (type(node).__module__, type(node).__qualname__, node.name)
        raise TypeError(type(node))

    return hashlib.sha256(repr(freeze(tree)).encode()).digest()


def local_edit(old_text, new_text):
    """Accept only an operator/constant substitution, preserving bindings, call
    sites and statement structure. Exclude source-position or identity tricks."""

    def shape(text):
        t = ast.parse(text)
        for n in ast.walk(t):
            if isinstance(n, ast.Constant) and type(n.value) in (int, str, bool, float):
                n.value = {int: 0, str: "", bool: False, float: 0.0}[type(n.value)]
            elif isinstance(n, ast.Compare):
                n.ops = [ast.Eq() for _ in n.ops]
            elif isinstance(n, ast.BoolOp):
                n.op = ast.And()
            elif isinstance(n, ast.BinOp):
                n.op = ast.Add()
        return ast.dump(t)

    return shape(old_text) == shape(new_text)


def rule_passes(name, module, changed, limit=2.75):
    """Every observed call must replay to the identical boundary. Fail closed."""
    end = time.process_time() + limit
    with open(os.path.join(CACHE_DIR, name + ".pkl"), "rb") as stream:
        outside, calls = pickle.load(stream)

    def touched(qualnames):
        return any(
            q == fn or q.startswith(fn + ".<locals>.")
            for q in qualnames
            for fn in changed
        )

    if not calls or not changed or touched(outside):
        return False
    fn = getattr(module, name)
    for reached, args_blob, expected in calls:
        if not touched(reached):
            continue
        if time.process_time() > end:
            return False
        expr, kwargs = pickle.loads(args_blob)
        result = fn(expr, **kwargs)
        if tree_digest(result) != expected:
            return False
    return time.process_time() <= end


def changed_queries(module):
    with open(os.path.join(CACHE_DIR, "inputs.pkl"), "rb") as f:
        baseline = pickle.load(f)
    items = [
        (bool(opts.get("execute")), sql)
        for opts, sql, _ in module.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")
    ]
    if len(items) != len(baseline):
        return None
    changed = {i for i, pair in enumerate(items) if pair != baseline[i]}
    return changed if len(changed) <= 4 else None


@lru_cache(maxsize=1)
def _fixture_tools(repo):
    import importlib.util
    import io
    import json
    import types

    with open(os.path.join(CACHE_DIR, "fixtures.json")) as f:
        profile = json.load(f)
    spec = importlib.util.spec_from_file_location(
        "_fixture_reader", os.path.join(repo, "tests/helpers.py")
    )
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    def pairs(rel, source):
        namespace = dict(vars(helper), open=lambda *args, **kwargs: io.StringIO(source))
        reader = types.FunctionType(helper.load_sql_fixture_pairs.__code__, namespace)
        return list(reader(rel.removeprefix("tests/fixtures/")))

    return profile, pairs


def fixture_effect(repo, rel, old, new):
    """Check the class setup on edited text before using baseline field reads."""
    if (
        rel not in (FIXTURE, "tests/fixtures/optimizer/tpc-h/tpc-h.sql")
        or old is None
        or new is None
    ):
        return (), False
    try:
        profile, pairs = _fixture_tools(repo)
        if rel not in profile:
            return (), False
        baseline = pairs(rel, old)
    except Exception:
        return (), False
    try:
        patched = pairs(rel, new)
    except Exception:
        return (), True
    omit = list(profile[rel])

    # The executor parses every input, reads the execute flag, and ignores
    # expected SQL. The optimizer and optimized-TPCH tests read other fields.
    def inputs(items):
        return [(bool(meta.get("execute")), sql) for meta, sql, _ in items]

    if inputs(baseline) == inputs(patched):
        test = "test_execute_tpcds" if rel == FIXTURE else "test_execute_tpch"
        omit.append("tests/test_executor.py::TestExecutor::" + test)
    return omit, False
