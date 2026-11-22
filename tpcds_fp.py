"""Fingerprint of the tree the TPC-DS executor test optimizes (shared by build and runtime)."""
import hashlib


def _ser(node, out):
    from sqlglot import exp
    if isinstance(node, exp.Expr if hasattr(exp, "Expr") else exp.Expression):
        out.append("(" + type(node).__name__)
        t = getattr(node, "_type", None)
        if t is not None:
            out.append("<")
            _ser(t, out)
            out.append(">")
        if node.comments:
            out.append(" #" + repr(node.comments))
        m = getattr(node, "_meta", None)
        if m is not None:
            # "nonnull" is an optimizer-only hint: no line of the executor's runtime phase reads it
            out.append(" @" + repr(sorted((k, repr(v)) for k, v in m.items()
                                          if not (k == "nonnull" and isinstance(v, bool)))))
        for k, v in node.args.items():
            out.append(" " + k + "=")
            _ser(v, out)
        out.append(")")
    elif isinstance(node, list):
        out.append("[")
        for x in node:
            _ser(x, out)
            out.append(",")
        out.append("]")
    else:
        out.append(type(node).__name__ + ":" + repr(node))


def optimized(expression, schema_dict):
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    return optimize(expression, ensure_schema(schema_dict, dialect=None), leave_tables_isolated=True, dialect=None)


def tree_fingerprint(tree):
    out = []
    _ser(tree, out)
    return hashlib.sha1("".join(out).encode()).hexdigest()


def fingerprint(expression, schema_dict):
    tree = optimized(expression, schema_dict)
    out = []
    _ser(tree, out)
    return hashlib.sha1("".join(out).encode()).hexdigest()


def plan_fingerprint(plan):
    """Fingerprint of a planner DAG: every step's type and attributes; expressions via _ser."""
    from sqlglot import exp
    from sqlglot.planner import Step
    order, ids, todo = [], {}, [plan.root]
    while todo:
        s = todo.pop()
        if id(s) in ids:
            continue
        ids[id(s)] = len(order)
        order.append(s)
        todo.extend(sorted(s.dependencies, key=lambda d: (type(d).__name__, d.name or "")))

    def ser(v, out):
        if isinstance(v, (exp.Expr if hasattr(exp, "Expr") else exp.Expression)):
            _ser(v, out)
        elif isinstance(v, Step):
            if id(v) not in ids:
                ids[id(v)] = len(order)
                order.append(v)
            out.append("step#%d" % ids[id(v)])
        elif isinstance(v, dict):
            out.append("{")
            for k in sorted(v, key=repr):
                out.append(repr(k) + ":")
                ser(v[k], out)
                out.append(",")
            out.append("}")
        elif isinstance(v, (list, tuple)):
            out.append("[")
            for x in v:
                ser(x, out)
                out.append(",")
            out.append("]")
        elif isinstance(v, (set, frozenset)):
            parts = []
            for x in v:
                o = []
                ser(x, o)
                parts.append("".join(o))
            out.append("set(" + ",".join(sorted(parts)) + ")")
        else:
            out.append(type(v).__name__ + ":" + repr(v))
    out = []
    i = 0
    while i < len(order):
        s = order[i]
        out.append("<%d %s " % (i, type(s).__name__))
        for k in sorted(vars(s)):
            out.append(k + "=")
            ser(vars(s)[k], out)
            out.append(";")
        out.append(">")
        i += 1
    return hashlib.sha1("".join(out).encode()).hexdigest()
