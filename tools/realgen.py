"""Sample-faithful mutation generator (operators seen in the 30 sample commits)."""
import ast, random

CMP_SWAP = {ast.Is: "==", ast.IsNot: "!=", ast.Eq: "!=", ast.NotEq: "==", ast.Lt: "<=", ast.LtE: "<",
            ast.Gt: ">=", ast.GtE: ">", ast.In: "not in", ast.NotIn: "in"}
WEIGHTS = {"cmp": 5, "boolop": 4, "not": 1, "int": 3, "bool": 4}


def _seg(lines, a_line, a_col, b_line, b_col):
    if a_line != b_line:
        return None
    return lines[a_line - 1][a_col:b_col]


def candidates(text):
    tree = ast.parse(text)
    lines = text.split("\n")
    out = {k: [] for k in WEIGHTS}
    in_func = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for s in ast.walk(n):
                in_func.add(id(s))
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant) and isinstance(n.value.value, str):
            continue
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in CMP_SWAP:
            a, b = n.left, n.comparators[0]
            s = _seg(lines, a.end_lineno, a.end_col_offset, b.lineno, b.col_offset)
            if s is not None and s.strip() in ("is", "is not", "==", "!=", "<", "<=", ">", ">=", "in", "not in"):
                out["cmp"].append((a.end_lineno, a.end_col_offset, b.col_offset, " %s " % CMP_SWAP[type(n.ops[0])], id(n) in in_func))
        elif isinstance(n, ast.BoolOp) and len(n.values) >= 2:
            i = random.randrange(len(n.values) - 1)
            a, b = n.values[i], n.values[i + 1]
            s = _seg(lines, a.end_lineno, a.end_col_offset, b.lineno, b.col_offset)
            if s is not None and s.strip().strip("()").strip() in ("and", "or") and s.strip() in ("and", "or"):
                new = " or " if s.strip() == "and" else " and "
                out["boolop"].append((a.end_lineno, a.end_col_offset, b.col_offset, new, id(n) in in_func))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            s = _seg(lines, n.lineno, n.col_offset, n.operand.lineno, n.operand.col_offset)
            if s is not None and s.strip() == "not":
                out["not"].append((n.lineno, n.col_offset, n.operand.col_offset, "", id(n) in in_func))
        elif isinstance(n, ast.Constant) and n.lineno == n.end_lineno:
            s = lines[n.lineno - 1][n.col_offset:n.end_col_offset]
            if isinstance(n.value, bool):
                out["bool"].append((n.lineno, n.col_offset, n.end_col_offset, str(not n.value), id(n) in in_func))
            elif isinstance(n.value, int) and s.isdigit():
                out["int"].append((n.lineno, n.col_offset, n.end_col_offset, str(n.value + 1), id(n) in in_func))
    return out


def mutate(text, where=None):
    c = candidates(text)
    kinds = [k for k in WEIGHTS if c[k] and (where is None or any(x[4] == (where == "func") for x in c[k]))]
    if not kinds:
        return None, None
    k = random.choices(kinds, [WEIGHTS[x] for x in kinds])[0]
    pool = [x for x in c[k] if where is None or x[4] == (where == "func")]
    ln, a, b, new, infunc = random.choice(pool)
    lines = text.split("\n")
    L = lines[ln - 1]
    lines[ln - 1] = L[:a] + new + L[b:] if k != "not" else L[:a] + L[b:]
    if k in ("cmp", "boolop"):
        # keep original spacing style: collapse doubled spaces we introduced
        lines[ln - 1] = lines[ln - 1][:a] + lines[ln - 1][a:].replace("  ", " ", 2) if "  " not in L else lines[ln - 1]
    new_text = "\n".join(lines)
    try:
        ast.parse(new_text)
    except SyntaxError:
        return None, None
    return new_text, k + ("-func" if infunc else "-data")
