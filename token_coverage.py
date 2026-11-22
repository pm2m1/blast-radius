"""Lookup coverage for literal tokenizer settings in the pinned Python implementation."""
import ast


def settings(tree):
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for tokenizer in cls.body:
            if not isinstance(tokenizer, ast.ClassDef) or tokenizer.name != "Tokenizer":
                continue
            for stmt in tokenizer.body:
                if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
                        isinstance(stmt.targets[0], ast.Name) and isinstance(stmt.value, ast.Dict)):
                    yield cls.name + ".Tokenizer", stmt.targets[0].id, stmt.value


def literal_values(node, field):
    try:
        values = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    if len(values) != len(node.keys) or any(type(k) is not str for k in values):
        return None
    if field == "NUMERIC_ESCAPES":
        if any(type(v) is not tuple or len(v) != 4 or
               any(type(x) is not int or not 0 <= x <= 0x10ffff for x in v) for v in values.values()):
            return None
    elif field == "NUMERIC_LITERALS":
        if any(type(v) is not str or len(v) > 128 for v in values.values()):
            return None
    else:
        return None
    return values


def masked_source(source, node):
    # AST columns are UTF-8 byte offsets. Keep every byte outside the dictionary.
    lines = source.encode("utf-8").splitlines(keepends=True)
    left = b"".join(lines[:node.lineno - 1]) + lines[node.lineno - 1][:node.col_offset]
    right = lines[node.end_lineno - 1][node.end_col_offset:] + b"".join(lines[node.end_lineno:])
    return left, right


def changed_reads(profile, module, old_text, new_text, old_tree):
    """Return affected node IDs only for a single value-only edit to a profiled setting."""
    eligible = {(q, f): n for q, f, n in settings(old_tree) if f in profile
                and module + ":" + q in profile[f]["owners"]}
    if not eligible:
        return None
    try:
        new_tree = ast.parse(new_text)
    except SyntaxError:
        return None
    for qual, field, new in settings(new_tree):
        old = eligible.get((qual, field))
        if old is None or (old.lineno, old.end_lineno) != (new.lineno, new.end_lineno):
            continue
        left, right = literal_values(old, field), literal_values(new, field)
        if left is None or right is None or list(left) != list(right):
            continue
        changed = {k for k in left if left[k] != right[k]}
        if not changed or masked_source(old_text, old) != masked_source(new_text, new):
            continue
        info = profile[field]
        ids = set(info["unobserved"])
        for owner in (module + ":" + qual, "*"):
            observed = info["reads"].get(owner, {})
            for key in changed:
                ids.update(observed.get(key, ()))
        return field, ids
    return None
