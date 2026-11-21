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


def fingerprint(expression, schema_dict):
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    tree = optimize(expression, ensure_schema(schema_dict, dialect=None), leave_tables_isolated=True, dialect=None)
    out = []
    _ser(tree, out)
    return hashlib.sha1("".join(out).encode()).hexdigest()
