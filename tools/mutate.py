"""Mutation harness: random realistic patches -> ground truth (full in-process run) vs selector.

usage: python mutate.py N seed [kinds]
"""
import ast
import difflib
import json
import os
import random
import re
import sys
import time

os.environ.setdefault("SQLGLOT_DIR", "/home/claude/sqlglot")
sys.path.insert(0, "/home/claude/work")
import blast as B  # noqa

REPO = B.REPO
TESTS = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]
MS = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in TESTS}
SKIP_GT = {t["number"] for t in TESTS if t["seconds"] > 15 and (os.environ.get("GT_TPCDS") != "1" or "tpcds" not in t["id"])}


def files(prefix, ext=".py"):
    out = []
    for dp, dn, fn in os.walk(os.path.join(REPO, prefix)):
        if "__pycache__" in dp:
            continue
        for f in fn:
            if f.endswith(ext):
                out.append(os.path.relpath(os.path.join(dp, f), REPO))
    return sorted(out)


SQL_PY = [f for f in files("sqlglot") if "__main__" not in f]
TEST_PY = [f for f in files("tests") if os.path.basename(f).startswith("test_")]
FIXTURES = files("tests/fixtures", ".sql")


def make_patch(rel, old, new):
    d = difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/" + rel, "b/" + rel, n=3)
    body = "".join(d)
    if not body:
        return None
    if not body.endswith("\n"):
        body += "\n"
    return "diff --git a/%s b/%s\nindex 0000000..1111111 100644\n%s" % (rel, rel, body)


OPS = [
    (r" == ", " != "), (r" != ", " == "), (r" is None", " is not None"), (r" is not None", " is None"),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
    (r" >= ", " > "), (r" and ", " or "), (r" or ", " and "), (r"\bnot ", ""), (r" \+ 1\b", " - 1"),
    (r" - 1\b", " + 1"), (r"copy=False", "copy=True"), (r"copy=True", "copy=False"), (r"\bif ", "if not "),
    (r"\breturn ([^\n]+)", "return None"), (r"\bbreak\b", "continue"), (r"\.upper\(\)", ".lower()"),
    (r"\.lower\(\)", ".upper()"), (r"\[0\]", "[-1]"), (r"\[-1\]", "[0]"), (r" in ", " not in "),
]


def mut_number(line):
    m = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line))
    if not m:
        return None
    x = random.choice(m)
    return line[:x.start()] + str(int(x.group(1)) + random.choice([1, -1, 2])) + line[x.end():]


def mut_string(line):
    m = list(re.finditer(r"\"([A-Za-z_ ]{2,})\"|'([A-Za-z_ ]{2,})'", line))
    if not m:
        return None
    x = random.choice(m)
    s = x.group(0)
    q = s[0]
    inner = s[1:-1]
    i = random.randrange(len(inner))
    ch = "X" if inner[i] != "X" else "Y"
    return line[:x.start()] + q + inner[:i] + ch + inner[i + 1:] + q + line[x.end():]


def valid(text):
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


def mutate_code(rel, text, scope="func"):
    lines = text.split("\n")
    tree = ast.parse(text)
    # pick lines inside function bodies (scope=func) or class/module level (scope=data)
    func_lines, data_lines = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for s in n.body:
                func_lines.update(range(s.lineno, s.end_lineno + 1))
    for n in ast.walk(tree):
        if isinstance(n, (ast.ClassDef, ast.Module)):
            for s in n.body:
                if isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr)):
                    data_lines.update(range(s.lineno, s.end_lineno + 1))
    pool = sorted(func_lines if scope == "func" else data_lines)
    if not pool:
        return None
    for _ in range(60):
        ln = random.choice(pool)
        line = lines[ln - 1]
        if not line.strip() or line.strip().startswith("#"):
            continue
        r = random.random()
        new = None
        if r < 0.55:
            ops = [(a, b) for a, b in OPS if re.search(a, line)]
            if ops:
                a, b = random.choice(ops)
                ms = list(re.finditer(a, line))
                x = random.choice(ms)
                new = line[:x.start()] + re.sub(a, b, x.group(0)) + line[x.end():]
        elif r < 0.7:
            new = mut_number(line)
        elif r < 0.85:
            new = mut_string(line)
        else:
            # delete the statement line (replace by pass to keep blocks valid)
            ind = re.match(r"\s*", line).group(0)
            if line.rstrip().endswith((":", "(", "[", "{", ",", "\\")):
                continue
            new = ind + "pass"
        if new is None or new == line:
            continue
        cand = lines[:]
        cand[ln - 1] = new
        t = "\n".join(cand)
        if valid(t):
            return t
    return None


CMP_SWAPS = [("==", "!="), ("!=", "=="), (" is not ", " != "), (" is not ", " is "), (" is ", " == "),
             (" is ", " is not "), ("<=", "<"), ("<", "<="), (">=", ">"), (">", ">="), (" not in ", " in "),
             (" in ", " not in ")]


def mutate_cmp(rel, text):
    """Swap one comparison operator inside a function body (the kind weak mutation decides)."""
    tree = ast.parse(text)
    lines = text.split("\n")
    sites = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for c in ast.walk(n):
                if isinstance(c, ast.Compare) and len(c.ops) == 1 and c.lineno == c.end_lineno:
                    sites.append(c)
    random.shuffle(sites)
    for c in sites[:30]:
        line = lines[c.lineno - 1]
        seg = line[c.col_offset:c.end_col_offset]
        opts = [(a, b) for a, b in CMP_SWAPS if a in seg]
        if not opts:
            continue
        a, b = random.choice(opts)
        seg2 = seg.replace(a, b, 1)
        new_line = line[:c.col_offset] + seg2 + line[c.end_col_offset:]
        cand = lines[:]
        cand[c.lineno - 1] = new_line
        t = "\n".join(cand)
        if valid(t) and t != text:
            return t
    return None


def mutate_comment(rel, text):
    lines = text.split("\n")
    for _ in range(30):
        ln = random.randrange(1, len(lines))
        line = lines[ln - 1]
        if random.random() < 0.5:
            ind = re.match(r"\s*", lines[ln - 1] if line.strip() else lines[min(ln, len(lines) - 1)]).group(0)
            cand = lines[:ln - 1] + [ind + "# " + random.choice(["TODO: revisit", "Keep in sync", "noqa", "x == y"])] + lines[ln - 1:]
        else:
            if not line.strip() or '"""' in line or "'''" in line:
                continue
            cand = lines[:]
            cand[ln - 1] = line + "  # " + random.choice(["note", "fast path", "see above"])
        t = "\n".join(cand)
        if valid(t):
            return t
    return None


def mutate_fixture(rel, text):
    lines = text.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("--") and not l.startswith("#")]
    if not idx:
        return None
    i = random.choice(idx)
    line = lines[i]
    r = random.random()
    if r < 0.4:
        new = line.replace(" AS ", " ", 1) if " AS " in line else line + " "
    elif r < 0.7:
        new = mut_number(line) or (line[:-1] if len(line) > 3 else line + "x")
    else:
        toks = line.split(" ")
        if len(toks) > 2:
            j = random.randrange(len(toks))
            toks[j] = toks[j].lower() if toks[j].isupper() else toks[j].upper()
        new = " ".join(toks)
    if new == line:
        new = line + " -- x"
    lines[i] = new
    return "\n".join(lines)


def mutate_add_func(rel, text):
    lines = text.split("\n")
    return text.rstrip("\n") + "\n\n\ndef _unused_helper_%d(x):\n    return x\n" % random.randrange(1000)


LAST_MK = None


def gen(kind):
    if kind in ("code", "data", "comment", "addfunc", "cmp", "real"):
        rel = random.choice(SQL_PY)
    elif kind == "testcode":
        rel = random.choice(TEST_PY)
    else:
        rel = random.choice(FIXTURES)
    old = open(os.path.join(REPO, rel), encoding="utf-8").read()
    global LAST_MK
    LAST_MK = None
    if kind == "real":
        import realgen
        new, LAST_MK = realgen.mutate(old)
    elif kind == "cmp":
        new = mutate_cmp(rel, old)
    elif kind == "code" or kind == "testcode":
        new = mutate_code(rel, old, "func")
    elif kind == "data":
        new = mutate_code(rel, old, "data")
    elif kind == "comment":
        new = mutate_comment(rel, old)
    elif kind == "addfunc":
        new = mutate_add_func(rel, old)
    else:
        new = mutate_fixture(rel, old)
    if new is None:
        return None
    if old.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    return make_patch(rel, old, new)


def select_one(mp, runner, patch, cpu_ms, class_ms, by_number):
    p = B.plan_commit(mp, "x", patch, MS, cpu_ms, class_ms, by_number, set(MS))
    failed = set()
    cpu = 0.0
    if p["run"]:
        pid, _ = runner.start(p["run"], p["new"], p["an"].swap, p["an"].reimport, time.time() + 120, p["units"],
                              p["an"].import_check)
        os.waitpid(pid, 0)
        raw = B.read_all(pid)
        res = json.loads(raw) if raw else None
        failed = B.finish_result(p, res, mp, set(MS))
        cpu = (res or {}).get("cpu") or 0
    sel = failed | p["blind"]
    return sel, p["an"], p["new"], p["cands"], cpu


def ground_truth(runner, new_texts, an=None, mp=None):
    nums = set(MS) - SKIP_GT
    if os.environ.get("GT_FAST") == "1" and an is not None:
        # Sound for function-body edits: a test that never runs the edited function cannot change.
        sup = set(an.coarse)
        for rel, lines in an.line_sets:
            t = B.old_tree_of(rel)
            for ln in lines:
                kind, q, nd = B.enclosing(t, ln)
                if kind != "func":
                    sup = set(MS)
                    break
                if kind == "func":
                    lo, hi = B.node_range(nd)
                    sup |= mp.cover(rel, lo, hi)
        if an.everything or not an.line_sets:
            sup = set(MS)
        nums = (sup | set(an.cands)) - SKIP_GT
    pid, _ = runner.start(nums, new_texts, {}, True, time.time() + float(os.environ.get("GT_BUDGET", "900")))
    os.waitpid(pid, 0)
    res = json.loads(B.read_all(pid) or b'{"failed": null}')
    return set(res["failed"]) if res.get("failed") is not None else None, res.get("error")


def main():
    n = int(sys.argv[1])
    random.seed(int(sys.argv[2]))
    kinds = sys.argv[3].split(",") if len(sys.argv) > 3 else ["code"] * 5 + ["data"] * 2 + ["comment", "fixture", "testcode", "addfunc"]
    out = open(sys.argv[4] if len(sys.argv) > 4 else "/home/claude/work/mut_results.jsonl", "a")
    mp = B.Map(B.MAP_PATH, TESTS)
    by_number = {t["number"]: t for t in TESTS}
    cpu_ms, class_ms = B.load_cpu(TESTS, MS)
    runner = B.Runner(TESTS)
    done = 0
    while done < n:
        kind = random.choice(kinds)
        patch = gen(kind)
        if not patch:
            continue
        t0 = time.time()
        sel, an, new_texts, cands, cpu = select_one(mp, runner, patch, cpu_ms, class_ms, by_number)
        gt, err = ground_truth(runner, new_texts, an, mp)
        if gt is None:
            continue
        miss = sorted(gt - sel)
        rec = {"kind": kind, "patch": patch, "gt": sorted(gt), "sel_n": len(sel), "cands_n": len(cands),
               "miss": miss, "cost": B.pack_cost(sel, MS), "oracle": B.pack_cost(gt, MS), "mk": LAST_MK, "cpu": cpu, "reasons": an.reasons[:20],
               "everything": an.everything, "gt_err": err, "secs": round(time.time() - t0, 1)}
        out.write(json.dumps(rec) + "\n")
        out.flush()
        done += 1
        kind = LAST_MK or kind
        print("%3d %-8s gt=%-3d sel=%-4d cands=%-4d miss=%s cost=%.1f cpu=%.1f %s" % (
            done, kind, len(gt), len(sel), len(cands), miss[:5], rec["cost"] / 1000, cpu,
            patch.split("\n")[0][11:]), flush=True)


if __name__ == "__main__":
    main()
