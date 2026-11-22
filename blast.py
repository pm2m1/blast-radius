"""Blast Radius selector.

For each patch:
  1. Work out a *candidate* set of tests that could possibly be affected, from a
     map built at the pinned sqlglot commit (per-test line coverage, files each
     test opens, classes each test instantiates, name references, test imports).
  2. Run the cheap candidates for real against the patched code, in a forked
     child of a warm process, and keep only the ones that fail.
  3. Include expensive candidates without running them.
  4. Pack the selection into workers to minimise busiest-worker + 2 s/worker.

When anything is uncertain the selector widens the selection: a miss costs
300 s, an extra test costs milliseconds.
"""

import ast
import builtins
import gzip
import io
import json
import marshal
import os
import re
import resource
import signal
import sys
import time

REPO = os.environ.get("SQLGLOT_DIR", "/app/sqlglot")
DATA = os.environ.get("DATA_DIR", "/data")
_HERE = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.environ.get("MAP_PATH") or (os.path.join(_HERE, "map.marshal") if os.path.exists(os.path.join(_HERE, "map.marshal"))
                                          else os.path.join(_HERE, "map.json.gz"))
MEASURE = os.environ.get("MEASURE") == "1"
CONSUMER_LEVEL = os.environ.get("CONSUMER_LEVEL", "1") == "1"
ATTR_LEVEL = os.environ.get("ATTR_LEVEL", "1") == "1"
WM_LEVEL = os.environ.get("WM_LEVEL", "1") == "1"
LAMBDA_LEVEL = os.environ.get("LAMBDA_LEVEL", "1") == "1"
AR_LEVEL = os.environ.get("AR_LEVEL", "1") == "1"
ESC_BUDGET = float(os.environ.get("ESC_BUDGET", "0.4"))  # CPU seconds for one import-time escalation
LINE_LEVEL = os.environ.get("LINE_LEVEL", "1") == "1"
RUN_MAX_SECONDS = float(os.environ.get("RUN_MAX_SECONDS", "1.0"))  # run tests cheaper than this
WALL_BUDGET = float(os.environ.get("WALL_BUDGET", "120"))
CHILD_MAX = float(os.environ.get("CHILD_MAX", "60"))  # stop running tests after this many seconds
PARALLEL = int(os.environ.get("PARALLEL", "2"))
DEBUG = os.environ.get("SELECT_DEBUG") == "1"
T0 = time.time()
real_io_open = io.open
TPCDS_CACHE = os.environ.get("TPCDS_CACHE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tpcds_cache.pkl"))

CONFIG_FILES = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py", "tests/conftest.py"}


def _cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- map
class Map:
    def __init__(self, path, tests):
        if path.endswith(".marshal"):
            with open(path, "rb") as f:
                m = marshal.loads(f.read())
        else:
            m = json.load(gzip.open(path))
        self.m = m
        self.sets = m["sets"]
        # map-build ids -> numbers in this tests.json
        number = {t["id"]: t["number"] for t in tests}
        self.remap = [number.get(i) for i in m["ids"]]
        self.number_by_id = number
        with open(os.path.join(_HERE, "token_reads.json")) as stream:
            self.token_reads = json.load(stream)
        self.lines = m["lines"]
        self.imp = {f: set(v) for f, v in m["imp"].items()}
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        known = set(m["ids"])
        self.unknown_tests = [t["number"] for t in tests if t["id"] not in known]
        self.number_to_index = {self.remap[i]: i for i in range(len(self.remap)) if self.remap[i] is not None}
        self._mro_rev = None
        number_of = {t["id"]: t["number"] for t in tests}
        self.units = {number_of[k]: v for k, v in m.get("units", {}).items() if k in number_of}
        self.usets = m.get("usets", [])
        self.unit_test_nums = set(self.units)
        self._S_cache = {}
        self.import_lambdas = set(m["import_lambdas"]) if "import_lambdas" in m else None
        self.tpcds_rt_raw = m.get("tpcds_rt")
        self.ar = m.get("ar")
        if self.ar:
            number = {t["id"]: t["number"] for t in tests}
            self.ar_remap = [number.get(i) for i in self.ar["ids"]]
            self.ar_import = set(self.ar["import"])
            self.ar_names = set(self.ar["names"]) - set(self.ar["dyn"])
        self._tpcds_rt = None
        self._cg_lines = None
        self.wm = m.get("wm")
        if self.wm:
            self.wm_remap = [number_of.get(i) for i in self.wm["ids"]]
            self.wm_custom = set(self.wm.get("custom", []))

    def wm_tests(self, sid):
        return {self.wm_remap[i] for i in self.wm["sets"][sid] if self.wm_remap[i] is not None}

    def affected_units(self, num, line_sets):
        """Unit names (TPC-DS queries q<k>, fixture cases c:<file>:<i>) reached by the changed lines;
        None means all of them."""
        u = self.units[num]
        names = u["names"]
        out = set()
        for rel, lines in line_sets:
            lm = u["lines"].get(rel, {})
            for ln in lines:
                sid = lm.get(str(ln))
                if sid is None:
                    if num in self.cover(rel, ln, ln):
                        return None  # covered outside any unit
                    continue
                us = {names[i] for i in self.usets[sid]}
                if "post" in us:
                    return None
                out |= us
        return out

    def ar_readers(self, key):
        sid = self.ar["read"].get(key)
        if sid is None:
            return set()
        return {self.ar_remap[i] for i in self.ar["sets"][sid] if self.ar_remap[i] is not None}

    def codegen_lines(self):
        """query index -> {"plan"|"cg"|"data": {rel: set(lines)}} (only replayable queries)."""
        if self._cg_lines is None:
            self._cg_lines = {}
            try:
                import gzip
                with gzip.open(os.path.join(_HERE, "cg", "codegen_lines.json.gz"), "rt") as f:
                    raw = json.load(f)
                self._cg_lines = {int(q): {ph: {rel: set(ls) for rel, ls in d.items()} for ph, d in v.items()}
                                  for q, v in raw.items()}
            except Exception as e:
                log("codegen profile unavailable:", repr(e)[:80])
        return self._cg_lines

    def tpcds_rt(self):
        """query index -> {rel: set(lines run outside optimize())} for the TPC-DS executor test."""
        if self._tpcds_rt is None and self.tpcds_rt_raw:
            self._tpcds_rt = {int(q): {f: set(ls) for f, ls in v.items()} for q, v in self.tpcds_rt_raw.items()}
        return self._tpcds_rt

    def unit_count(self, num):
        return sum(1 for n in self.units[num]["names"] if n != "post")

    def S(self, sid):
        c = self._S_cache.get(sid)
        if c is None:
            c = frozenset(self.remap[i] for i in self.sets[sid] if self.remap[i] is not None)
            self._S_cache[sid] = c
        return c

    def cover(self, rel, lo, hi):
        lm = self.lines.get(rel)
        out = set()
        if not lm:
            return out
        seen = set()
        for ln in range(lo, hi + 1):
            sid = lm.get(str(ln))
            if sid is not None and sid not in seen:
                seen.add(sid)
                out |= self.S(sid)
        return out

    def import_time(self, rel, lo, hi):
        il = self.imp.get(rel, ())
        return any(ln in il for ln in range(lo, hi + 1))

    def opens(self, rel):
        sid = self.m["opens"].get(rel)
        return set(self.S(sid)) if sid is not None else set()

    def name_tests(self, name):
        sid = self.m["name_tests"].get(name)
        return set(self.S(sid)) if sid is not None else set()

    def name_refs(self, name):
        return self.m["name_refs"].get(name, {})

    def used_at_import(self, key):
        """Was class `key` or a subclass instantiated while importing sqlglot?"""
        if not hasattr(self, "_imp_inst"):
            self._imp_inst = set(self.m.get("import_inst", []))
            self.class_users(key)  # builds _mro_rev
        if not self._imp_inst:
            return True
        return any(k in self._imp_inst for k in self._mro_rev.get(key, [key]) + [key])

    def class_users(self, key):
        """Tests that instantiated class `key` (module:qualname) or any subclass."""
        if self._mro_rev is None:
            rev = {}
            for k, mro in self.m["mro"].items():
                for base in mro:
                    rev.setdefault(base, []).append(k)
            self._mro_rev = rev
        out = set()
        found = False
        for k in self._mro_rev.get(key, [key]):
            sid = self.m["inst"].get(k)
            if sid is not None:
                out |= self.S(sid)
                found = True
        # dynamically created subclasses (e.g. inside tests) are recorded by qualname only
        qual = key.split(":")[-1]
        for k, sid in self.m["inst"].items():
            if k not in self.m["mro"] and k.split(":")[-1] == qual:
                out |= self.S(sid)
        return out, found

    def test_file_tests(self, rel):
        mod = rel[:-3].replace("/", ".")
        files = {rel}
        for m in self.m["importers"].get(mod, []):
            files.add(m.replace(".", "/") + ".py")
        out = set()
        for f in files:
            out |= set(self.by_file.get(f, []))
        return out


# --------------------------------------------------------------------------- patches
class FilePatch:
    def __init__(self):
        self.old = None
        self.new = None
        self.hunks = []
        self.binary = False
        self.new_file = False
        self.deleted = False
        self.mode_only = False

    @property
    def path(self):
        return self.new or self.old


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        if line.startswith("new file mode"):
            cur.new_file = True
        elif line.startswith("deleted file mode"):
            cur.deleted = True
        elif line.startswith("rename from "):
            cur.old = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur.new = line[len("rename to "):]
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.binary = True
        elif line.startswith("--- "):
            p = line[4:].split("\t")[0]
            cur.old = None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
            if p == "/dev/null":
                cur.new_file = True
        elif line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            cur.new = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            if p == "/dev/null":
                cur.deleted = True
        elif line.startswith("@@"):
            m = HUNK_RE.match(line)
            os_, ol, ns, nl = int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)
            body = []
            i += 1
            o_left, n_left = ol, nl
            while i < len(lines) and (o_left > 0 or n_left > 0 or (i < len(lines) and lines[i].startswith("\\"))):
                l = lines[i]
                if l.startswith("\\"):
                    if body:
                        body[-1] = (body[-1][0], body[-1][1], True)
                    i += 1
                    continue
                tag = l[:1] if l else " "
                if tag not in " +-":
                    break
                body.append((tag, l[1:], False))
                if tag in " -":
                    o_left -= 1
                if tag in " +":
                    n_left -= 1
                i += 1
            # trailing "\ No newline" after the hunk
            while i < len(lines) and lines[i].startswith("\\"):
                if body:
                    body[-1] = (body[-1][0], body[-1][1], True)
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    return files


def apply_patch(fp, old_text):
    """Return new text. Raises ValueError if the hunks do not apply."""
    if fp.new_file:
        old_lines = []
    else:
        old_lines = old_text.split("\n")
        # split keeps a trailing "" if text ends with newline
    old_ends_nl = old_text.endswith("\n") if old_text else True
    if old_lines and old_lines[-1] == "" and old_ends_nl:
        old_lines = old_lines[:-1]
    out = []
    pos = 0  # index in old_lines
    new_ends_nl = old_ends_nl
    for os_, ol, ns, nl, body in fp.hunks:
        want = [b[1] for b in body if b[0] in " -"]
        start = max(os_ - 1, 0) if ol > 0 else os_
        cand = None
        for delta in [0] + [d for k in range(1, 200) for d in (k, -k)]:
            s = start + delta
            if s < pos or s + len(want) > len(old_lines):
                continue
            if old_lines[s:s + len(want)] == want:
                cand = s
                break
        if cand is None:
            raise ValueError("hunk does not apply to %s at %d" % (fp.path, os_))
        out.extend(old_lines[pos:cand])
        pos = cand
        for tag, text, nonl in body:
            if tag == " ":
                out.append(old_lines[pos]); pos += 1
                if nonl:
                    new_ends_nl = False
            elif tag == "-":
                pos += 1
                if nonl:
                    new_ends_nl = True
            else:
                out.append(text)
                new_ends_nl = not nonl
    out.extend(old_lines[pos:])
    if pos < len(old_lines):
        new_ends_nl = old_ends_nl
    return "\n".join(out) + ("\n" if (new_ends_nl and out) else "")


def changed_old_lines(fp):
    """Old-file line numbers touched by the patch, including both neighbours of insertions."""
    touched = set()
    for os_, ol, ns, nl, body in fp.hunks:
        o = os_ if ol > 0 else os_ + 1
        prev = o - 1
        for tag, text, _ in body:
            if tag == " ":
                prev = o
                o += 1
            elif tag == "-":
                touched.add(o)
                prev = o
                o += 1
            else:
                touched.add(prev)
                touched.add(o)
    return {l for l in touched if l >= 1}


# --------------------------------------------------------------------------- AST analysis
def strip_doc(body):
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def dump(node):
    return ast.dump(node, annotate_fields=False, include_attributes=False)


def scope_units(tree):
    """Flatten a module into functions, classes and per-scope plain statements."""
    funcs, classes, stmts = {}, {}, {}

    def walk(body, prefix, scope):
        stmts.setdefault(scope, [])
        seen = {}
        for node in strip_doc(body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                k = prefix + node.name
                seen[k] = seen.get(k, 0) + 1
                if seen[k] > 1:
                    k = "%s#%d" % (k, seen[k])
                funcs[k] = (node, scope)
            elif isinstance(node, ast.ClassDef):
                k = prefix + node.name
                classes[k] = (node, scope)
                walk(node.body, k + ".", k)
            else:
                stmts[scope].append(node)

    walk(tree.body, "", "")
    return funcs, classes, stmts


def func_header(node):
    n = ast.FunctionDef(name=node.name, args=node.args, body=[], decorator_list=node.decorator_list,
                        returns=node.returns, type_params=getattr(node, "type_params", []))
    return dump(n)


def needs_reimport(mp, rel, fn):
    """Swapping fn.__code__ in place cannot reach closures/lambdas fn already created: if fn defines
    nested functions and ran while importing, the child must re-import the module to verify."""
    nested = any(isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))
                 for s in fn.body for x in ast.walk(s))
    if any("cache" in dump(d).lower() or "memo" in dump(d).lower() for d in fn.decorator_list):
        return True  # results computed with the old code may be cached
    if not nested:
        return False
    lo, hi = node_range(fn)
    return mp.import_time(rel, lo, hi) or not mp.imp.get(rel)


def defaults_only(on, nn):
    """The two headers differ only in constant default values (evaluated once, read by callers)."""
    import copy
    a, b = on.args, nn.args
    da = list(a.defaults) + [d for d in a.kw_defaults if d is not None]
    db = list(b.defaults) + [d for d in b.kw_defaults if d is not None]
    if len(da) != len(db) or not all(isinstance(d, ast.Constant) for d in da + db):
        return False
    c1, c2 = copy.deepcopy(on), copy.deepcopy(nn)
    for c in (c1, c2):
        c.args.defaults = [ast.Constant(value=None) for _ in c.args.defaults]
        c.args.kw_defaults = [None if d is None else ast.Constant(value=None) for d in c.args.kw_defaults]
    return func_header(c1) == func_header(c2)


def func_body(node):
    return [dump(s) for s in strip_doc(node.body)]


def class_header(node):
    return dump(ast.ClassDef(name=node.name, bases=node.bases, keywords=node.keywords, body=[],
                             decorator_list=node.decorator_list, type_params=getattr(node, "type_params", [])))


def node_range(node):
    lo = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return lo, node.end_lineno


def bound_names(stmts):
    names = set()
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                names.add(n.id)
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)):
                names.add(n.attr)
            elif isinstance(n, ast.alias):
                names.add((n.asname or n.name).split(".")[0])
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
    return names


GENERIC_NAMES = {"self", "cls", "exp", "e", "x", "this", "expression", "args", "kwargs", "t", "i", "k", "v",
                 "name", "node", "value", "key", "sql", "dialect", "copy", "result", "None", "True", "False"}


class Analysis:
    """Accumulates candidates and the run mode for one commit."""

    def __init__(self, mp):
        self.mp = mp
        self.cands = set()
        self.everything = False
        self.reasons = []
        self.swap = {}       # module name -> list of function qualnames (body-only changes)
        self.reimport = False
        self.depth_guard = 0
        self.visited = set()
        self.coarse = set()  # function-level superset (informational)
        self.units_all = set()  # unit tests (e.g. tpcds) reached by a non-line rule: all units affected
        self.line_sets = []  # (rel, lines) used for line-level selection
        self.import_check = False  # names may have vanished: import every module in the child
        self.non_body = False  # something other than function/lambda bodies changed
        self.body_lines = []   # (rel, old lines) of every function/lambda body edit
        self.data_readers = set()  # tests that read a changed data file
        self.files = []  # FilePatch objects of the commit

    def add(self, tests, why):
        if tests:
            self.cands |= tests
            if not why.startswith("lines ") and self.mp.unit_test_nums & tests:
                self.units_all |= self.mp.unit_test_nums & tests
        if DEBUG:
            self.reasons.append("%s:%d" % (why, len(tests)))

    def all(self, why):
        self.everything = True
        self.reasons.append("ALL:" + why)


def module_name(rel):
    m = rel[:-3].replace("/", ".")
    return m[:-9] if m.endswith(".__init__") else m


def class_key(rel, qual):
    return module_name(rel) + ":" + qual



_OPS = ["Lt", "LtE", "Gt", "GtE", "Eq", "NotEq", "Is", "IsNot", "In", "NotIn"]
_IS_EQ = {"Is", "IsNot", "Eq", "NotEq"}
_ORDER_OR_EQUAL = frozenset({"Lt", "LtE", "Gt", "GtE", "Eq", "NotEq"})
# Exactly the alternatives tools/wm_plugin.py records (its ALTS table); anything else: no data.
_PROFILED_ALTERNATIVES = {
    **dict.fromkeys(("Lt", "LtE", "Gt", "GtE"), _ORDER_OR_EQUAL),
    **dict.fromkeys(("Eq", "NotEq"), _ORDER_OR_EQUAL | {"Is", "IsNot"}),
    **dict.fromkeys(("Is", "IsNot"), _IS_EQ),
    "In": {"NotIn"},
    "NotIn": {"In"},
}
PERF_SENSITIVE = ("::test_max_nodes", "::test_generator_memory")


def op_swaps(on, nn):
    """[(old Compare node, old op, new op)] if the two functions differ only in comparison operators."""
    import copy

    def norm(node):
        c = copy.deepcopy(node)
        for x in ast.walk(c):
            if isinstance(x, ast.Compare):
                x.ops = [ast.Eq() for _ in x.ops]
        return dump(c)
    if norm(on) != norm(nn):
        return None
    out = []
    for a, b in zip(ast.walk(on), ast.walk(nn)):
        if isinstance(a, ast.Compare) and [type(o) for o in a.ops] != [type(o) for o in b.ops]:
            if len(a.ops) != 1 or len(b.ops) != 1:
                return None
            out.append((a, type(a.ops[0]).__name__, type(b.ops[0]).__name__))
    return out or None


def weak_mutation_tests(mp, rel, on, nn):
    """Tests whose recorded run would observe a difference from the operator swaps, or None if
    the weak-mutation profile cannot decide (fall back to coverage)."""
    swaps = op_swaps(on, nn)
    if not swaps:
        return None
    wm = mp.wm
    out, lines = set(), set()
    for node, old, new in swaps:
        key = "%s:%d:%d" % (rel, node.lineno, node.col_offset)
        if new not in _PROFILED_ALTERNATIVES.get(old, ()) or new == old:
            return None
        imask = wm["import"].get(key, 0)
        if imask & (1 << _OPS.index(new)) or imask & (1 << 15):
            return None  # differs while importing: state built at import changes
        if (old in _IS_EQ and new in _IS_EQ and ({old, new} & {"Is", "IsNot"}) and
                ({old, new} & {"Eq", "NotEq"}) and key in mp.wm_custom):
            return None  # identity <-> equality with a custom __eq__ (side effects such as hash caching)
        reached_sid = wm["reached"].get(key)
        covered = mp.cover(rel, node.lineno, node.end_lineno)
        if reached_sid is None:
            if covered:
                return None  # profile and coverage disagree: do not trust it
            continue
        reached = mp.wm_tests(reached_sid)
        sid = wm["flip"].get(key, {}).get(new)
        if sid is not None:
            out |= mp.wm_tests(sid)
        # tests that measure time or memory can notice an equivalent but costlier operator
        out |= {n for n in reached if any(mp.m["ids"][i].endswith(PERF_SENSITIVE)
                                          for i in [mp.number_to_index.get(n, -1)] if i >= 0)}
        lines.update(range(node.lineno, node.end_lineno + 1))
    return out, lines


def code_shapes(text, filename):
    """co_qualname -> shape tuple for every code object in a module."""
    import types
    try:
        code = compile(text, filename, "exec", dont_inherit=True)
    except SyntaxError:
        return None
    out = {}
    stack = [code]
    while stack:
        c = stack.pop()
        out.setdefault(c.co_qualname, []).append((c.co_varnames, c.co_cellvars, c.co_freevars, c.co_flags,
                                                 c.co_argcount, c.co_posonlyargcount, c.co_kwonlyargcount))
        for k in c.co_consts:
            if isinstance(k, types.CodeType):
                stack.append(k)
    return out


def func_shapes(node):
    """Code-object shapes of one function compiled on its own (old and new compiled alike)."""
    import copy
    n = copy.copy(node)
    n.decorator_list = []
    mod = ast.Module(body=[n], type_ignores=[])
    ast.fix_missing_locations(mod)
    try:
        return code_shapes_from(compile(mod, "<f>", "exec", dont_inherit=True))
    except Exception:
        return None


def code_shapes_from(code):
    import types
    out = {}
    stack = [code]
    while stack:
        c = stack.pop()
        out.setdefault(c.co_qualname, []).append((c.co_varnames, c.co_cellvars, c.co_freevars, c.co_flags,
                                                 c.co_argcount, c.co_posonlyargcount, c.co_kwonlyargcount))
        for k in c.co_consts:
            if isinstance(k, types.CodeType):
                stack.append(k)
    return out


def same_shape(old_shapes, new_shapes, qual):
    if old_shapes is None or new_shapes is None:
        return False
    keys_o = {k: v for k, v in old_shapes.items() if k == qual or k.startswith(qual + ".")}
    keys_n = {k: v for k, v in new_shapes.items() if k == qual or k.startswith(qual + ".")}
    if set(keys_o) != set(keys_n):
        return False
    for k in keys_o:
        if sorted(map(repr, keys_o[k])) != sorted(map(repr, keys_n[k])):
            return False
    return True


def stmt_index(func_node):
    """All statements inside a function, innermost-first lookups by line."""
    stmts = []
    for n in ast.walk(func_node):
        if isinstance(n, ast.stmt) and n is not func_node:
            stmts.append(n)
    return stmts


def header_range(n):
    body = getattr(n, "body", None)
    if body and isinstance(body, list) and body and hasattr(body[0], "lineno"):
        return n.lineno, max(n.lineno, body[0].lineno - 1)
    return n.lineno, n.end_lineno


def innermost(stmts, line):
    best = None
    for n in stmts:
        if n.lineno <= line <= n.end_lineno:
            if best is None or (n.lineno >= best.lineno and n.end_lineno <= best.end_lineno):
                best = n
    return best


def parent_block_lines(func_node, stmts, line):
    """Lines that run whenever execution reaches `line`'s block: enclosing header + its first body line."""
    out = set()
    best = None
    for n in [func_node] + stmts:
        for field in ("body", "orelse", "finalbody", "handlers"):
            blk = getattr(n, field, None)
            if not blk or not isinstance(blk, list):
                continue
            lo = blk[0].lineno if hasattr(blk[0], "lineno") else None
            hi = max(getattr(b, "end_lineno", 0) or 0 for b in blk)
            if lo is None:
                continue
            # a block "contains" the line if it lies between its first statement and its end (inclusive),
            # or directly after the header (insertion at block start)
            hdr_lo, hdr_hi = header_range(n) if n is not func_node else (lo, lo)
            if (lo <= line <= hi) or (hdr_hi <= line <= lo):
                span = hi - min(lo, hdr_hi)
                if best is None or span < best[0]:
                    best = (span, n, blk)
    if best is None:
        return out
    _, n, blk = best
    if n is func_node:
        out.add(blk[0].lineno)
    else:
        a, b = header_range(n)
        out.update(range(a, b + 1))
        out.add(n.body[0].lineno if getattr(n, "body", None) else n.lineno)
        out.add(blk[0].lineno if hasattr(blk[0], "lineno") else n.lineno)
    return out


def line_level_lines(fp_hunks, func_node):
    """Old-file lines whose coverage decides which tests a body change can reach."""
    lo, hi = func_node.lineno, func_node.end_lineno
    stmts = stmt_index(func_node)
    out = set()
    for os_, ol, ns, nl, body in fp_hunks:
        o = os_ if ol > 0 else os_ + 1
        i = 0
        while i < len(body):
            tag = body[i][0]
            if tag == " ":
                o += 1
                i += 1
                continue
            removed = []
            added = 0
            start = o
            while i < len(body) and body[i][0] == "-":
                removed.append(o)
                o += 1
                i += 1
            while i < len(body) and body[i][0] == "+":
                added += 1
                i += 1
            while i < len(body) and body[i][0] == "-":  # interleaved
                removed.append(o)
                o += 1
                i += 1
            prev_line, next_line = start - 1, o
            if not (lo <= start <= hi + 1 or any(lo <= r <= hi for r in removed)):
                continue
            weak = False
            for r in removed:
                n = innermost(stmts, r)
                if n is None:
                    weak = True
                    continue
                a, b = header_range(n)
                if a <= r <= b:
                    out.update(range(a, b + 1))
                    if isinstance(n, (ast.Try,)) or r != a:
                        weak = True
                else:
                    weak = True  # an else:/finally:/except line inside a compound statement
                    out.update(range(a, b + 1))
            if added or weak or not removed:
                for ln in (prev_line, next_line):
                    n = innermost(stmts, ln)
                    if n is not None:
                        a, b = header_range(n)
                        out.update(range(a, b + 1) if a <= ln <= b else [ln])
                out |= parent_block_lines(func_node, stmts, start)
                if removed:
                    out |= parent_block_lines(func_node, stmts, removed[-1])
    return out


def analyze_py(an, rel, old_text, new_text, tree_cache, fp=None):
    mp = an.mp
    try:
        old_tree = (old_tree_of(rel) if old_text is not None and old_text == _old_text_of(rel) else None) \
            or (ast.parse(old_text) if old_text is not None else ast.parse(""))
    except SyntaxError:
        an.non_body = True
        an.all("old syntax " + rel)
        return
    try:
        new_tree = ast.parse(new_text) if new_text is not None else None
    except SyntaxError:
        # the patched module cannot be imported: every test whose import chain includes it fails;
        # the child run will discover that for runnable tests, but be safe.
        an.reimport = True
        an.non_body = True
        an.add(importers_of_module(an, rel), "syntax-error-importers")
        if rel.startswith("sqlglot/"):
            an.all("syntax error in sqlglot module " + rel)
        return
    tree_cache[rel] = old_tree
    if new_tree is None:  # deleted file
        an.non_body = True
        an.reimport = True
        an.import_check = True
        an.add(importers_of_module(an, rel), "deleted-module")
        an.add(mp.cover(rel, 1, 10 ** 6), "deleted-cover")
        if rel.startswith("sqlglot/"):
            an.all("deleted sqlglot module")
        return
    an.add(mp.opens(rel), "opens")  # tests that read the source text itself
    of, oc, os_ = _units_cached(rel, old_tree)
    nf, nc, ns = scope_units(new_tree)
    shapes = [None, None]
    if fp is not None:
        old_touch, new_touch = touched_both(fp)
    else:
        old_touch = new_touch = None

    def hit(node_o, node_n):
        if old_touch is None:
            return True
        if node_o is not None:
            lo, hi = node_range(node_o)
            if any(lo <= l <= hi for l in old_touch):
                return True
        if node_n is not None:
            lo, hi = node_range(node_n)
            if any(lo <= l <= hi for l in new_touch):
                return True
        return False
    is_test = rel.startswith("tests/")
    mod = module_name(rel)
    if rel in ("tests/test_executor.py", "tests/helpers.py", "tests/__init__.py"):
        an.non_body = True

    def scope_change(scope, why):
        if scope == "":
            module_change(an, rel, old_tree, of, oc, os_, ns, why)
        else:
            class_change(an, rel, scope, oc, why)

    # module docstring changes are harmless (no doctests are collected)
    for k in set(of) | set(nf):
        o = of.get(k)
        n = nf.get(k)
        if o is None or n is None:
            scope = (o or n)[1]
            if o is None and scope == "" and "#" not in k and k not in os_names(os_) \
                    and not mp.name_refs(k) and not mp.name_tests(k) and k.count("_") < 50:
                an.reimport = True
                an.add(set(), "new unreferenced function " + k)
                continue
            an.non_body = True
            if o is not None:
                lo, hi = node_range(o[0])
                an.add(mp.cover(rel, lo, hi), "removed-func-cover")
            short = k.split(".")[-1].split("#")[0]
            if short not in GENERIC_NAMES:
                an.add(mp.name_tests(short), "def-name-refs " + short)
            scope_change(scope, "def added/removed " + k)
            an.reimport = True
            an.import_check = True
            continue
        on, nn = o[0], n[0]
        if not hit(on, nn):
            continue  # untouched by the patch: identical text
        if func_header(on) == func_header(nn) and func_body(on) == func_body(nn):
            continue  # identical apart from comments / docstring
        lo, hi = node_range(on)
        body_only = func_header(on) == func_header(nn)
        if (not body_only and not defaults_only(on, nn)) or \
                any("cache" in dump(d).lower() or "memo" in dump(d).lower() for d in on.decorator_list):
            an.non_body = True
        elif not body_only:
            an.body_lines.append((rel, set(range(lo, hi + 1))))
        n_body = len(an.body_lines)
        precise = False
        if body_only and WM_LEVEL and mp.wm:
            wm_res = weak_mutation_tests(mp, rel, on, nn)
            if wm_res is not None:
                tests_wm, lines_wm = wm_res
                an.add(tests_wm, "lines-wm " + k)
                an.line_sets.append((rel, lines_wm))
                an.body_lines.append((rel, set(lines_wm)))
                if needs_reimport(mp, rel, on):
                    an.reimport = True
                else:
                    an.swap.setdefault(mod, []).append(k)
                continue
        if body_only and fp is not None and LINE_LEVEL:
            qual = k
            if "#" not in k and same_shape(func_shapes(on), func_shapes(nn), on.name):
                ll = line_level_lines(fp.hunks, on)
                an.line_sets.append((rel, ll))
                fine = set()
                for ln in ll:
                    fine |= mp.cover(rel, ln, ln)
                an.add(fine, "lines " + k)
                an.body_lines.append((rel, set(ll)))
                an.coarse |= mp.cover(rel, lo, hi)
                precise = True
        if not precise:
            an.add(mp.cover(rel, lo, hi), "func " + k)
            an.body_lines.append((rel, set(range(lo, hi + 1))))
        if func_header(on) != func_header(nn):
            an.reimport = True
            an.import_check = True
            same_decorators = [dump(d) for d in on.decorator_list] == [dump(d) for d in nn.decorator_list] \
                and on.name == nn.name and type(on) is type(nn)
            if not same_decorators:
                scope_change(o[1], "header " + k)
            # defaults / parameters / annotations: only callers can notice, and they ran the body
            an.add(mp.cover(rel, lo, hi), "header-func " + k)
        body_lo = on.body[0].lineno if on.body else on.lineno
        if mp.import_time(rel, body_lo, hi):
            an.reimport = True
            an.non_body = True
            import_time_escalation(an, rel, k, on, o[1], 0)
        if func_header(on) == func_header(nn):
            if needs_reimport(mp, rel, on):
                an.reimport = True
            an.swap.setdefault(mod, []).append(k)
    for k in set(oc) | set(nc):
        o = oc.get(k)
        n = nc.get(k)
        if o is None or n is None:
            an.reimport = True
            an.import_check = True
            an.non_body = True
            scope_change((o or n)[1], "class added/removed " + k)
            if o is not None:
                class_change(an, rel, k, oc, "removed class")
            continue
        if not hit(o[0], n[0]):
            continue
        if class_header(o[0]) != class_header(n[0]):
            an.reimport = True
            an.non_body = True
            class_change(an, rel, k, oc, "class header")
            scope_change(o[1], "class header " + k)
    for scope in set(os_) | set(ns):
        olds = [x for x in os_.get(scope, []) if old_touch is None or hit(x, None)]
        news = [x for x in ns.get(scope, []) if new_touch is None or hit(None, x)]
        if len(os_.get(scope, [])) != len(ns.get(scope, [])):
            olds, news = os_.get(scope, []), ns.get(scope, [])
        a = [dump(s) for s in olds]
        b = [dump(s) for s in news]
        if a != b:
            pairs = lambda_only_change(mp, rel, olds, news) if LAMBDA_LEVEL else None
            if pairs:
                lambda_change(an, rel, mod, pairs)
                continue
            an.non_body = True
            an.reimport = True
            if scope == "":
                an.import_check = True
            old_changed = [s for s in olds if dump(s) not in b]
            changed = old_changed + [s for s in news if dump(s) not in a]
            if scope == "":
                module_change(an, rel, old_tree, of, oc, os_, ns, "module stmts", changed)
            else:
                class_change(an, rel, scope, oc, "class stmts", changed, old_changed)
    if is_test:
        # anything structural in a test module: run the whole file and its importers
        if an.reimport:
            an.add(mp.test_file_tests(rel), "test-module")


def _outer_lambdas(stmts):
    out = []

    def rec(n):
        if isinstance(n, ast.Lambda):
            out.append(n)
            for a in ast.iter_child_nodes(n.args):
                rec(a)
            return
        for c in ast.iter_child_nodes(n):
            rec(c)
    for s in stmts:
        rec(s)
    return out


def _mask_lambdas(stmts):
    import copy
    out = []
    for s in stmts:
        c = copy.deepcopy(s)
        for lam in _outer_lambdas([c]):
            lam.body = ast.Constant(value=None)
        out.append(dump(c))
    return out


def lambda_only_change(mp, rel, olds, news):
    """[(old Lambda, new Lambda)] when the statements differ only inside lambda bodies that are never
    called while modules import; None otherwise."""
    if mp.import_lambdas is None or len(olds) != len(news) or not olds:
        return None
    if _mask_lambdas(olds) != _mask_lambdas(news):
        return None
    ol, nl = _outer_lambdas(olds), _outer_lambdas(news)
    if len(ol) != len(nl):
        return None
    pairs = [(a, b) for a, b in zip(ol, nl) if dump(a.body) != dump(b.body)]
    if not pairs:
        return None
    lam_lines = {}
    for a in ol:
        lam_lines.setdefault(a.lineno, []).append(a)
    for a, b in pairs:
        if "%s:%d" % (rel, a.lineno) in mp.import_lambdas:
            return None
        # several lambdas sharing a first line cannot be told apart in the import profile
        if len(lam_lines.get(a.lineno, ())) > 1 and any(
                "%s:%d" % (rel, x.lineno) in mp.import_lambdas for x in lam_lines[a.lineno]):
            return None
    return pairs


def lambda_change(an, rel, mod, pairs):
    mp = an.mp
    for a, b in pairs:
        if WM_LEVEL and mp.wm:
            wm_res = weak_mutation_tests(mp, rel, a, b)
            if wm_res is not None:
                tests_wm, lines_wm = wm_res
                an.body_lines.append((rel, set(lines_wm)))
                an.add(tests_wm, "lambda-wm %d" % a.lineno)
                an.line_sets.append((rel, lines_wm))
                continue
        lo, hi = a.lineno, a.end_lineno
        an.body_lines.append((rel, set(range(lo, hi + 1))))
        an.add(mp.cover(rel, lo, hi), "lines lambda %d" % a.lineno)
        an.line_sets.append((rel, set(range(lo, hi + 1))))
    an.reimport = True


_NON_EXECUTOR_DIRS = ("sqlglot/dialects/", "sqlglot/generators/", "sqlglot/parsers/", "sqlglot/typing/")
_EXECUTOR_MODULES = ("__init__", "base", "dialect", "python")


def _class_body_ok(old_cls, new_cls):
    """Only assignments, functions and nested classes may differ inside a dialect class body."""
    if ([dump(b) for b in old_cls.bases] != [dump(b) for b in new_cls.bases] or
            [dump(k) for k in old_cls.keywords] != [dump(k) for k in new_cls.keywords] or
            [dump(d) for d in old_cls.decorator_list] != [dump(d) for d in new_cls.decorator_list] or
            old_cls.name != new_cls.name):
        return False
    if len(old_cls.body) != len(new_cls.body):
        return False
    for a, b in zip(old_cls.body, new_cls.body):
        if dump(a) == dump(b):
            continue
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.ClassDef):
            if not _class_body_ok(a, b):
                return False
        elif isinstance(a, (ast.Assign, ast.AnnAssign)):
            targets = a.targets if isinstance(a, ast.Assign) else [a.target]
            if not all(isinstance(t, ast.Name) for t in targets):
                return False
            if [dump(t) for t in targets] != [dump(t) for t in (b.targets if isinstance(b, ast.Assign) else [b.target])]:
                return False
        elif not isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
    return True


_DIALECT_MODULES = None


def _dialect_modules():
    """Pinned-commit map: dialect name -> sqlglot modules that can run while its generator is built."""
    global _DIALECT_MODULES
    if _DIALECT_MODULES is None:
        try:
            with open(os.path.join(_HERE, "dialect_modules.json")) as f:
                _DIALECT_MODULES = json.load(f)
        except Exception:
            _DIALECT_MODULES = {}
    return _DIALECT_MODULES


def duck_side_only(an, new_texts):
    """Every change is inside a dialect-specific module that the TPC-DS executor path never runs:
    the executor's results cannot change; only the DuckDB SQL the test compares against can."""
    if not an.files:
        return False
    for fp in an.files:
        rel = fp.path
        if fp.binary or fp.new_file or fp.deleted or (fp.old and fp.old != rel):
            return False
        if not rel.startswith(_NON_EXECUTOR_DIRS) or not rel.endswith(".py"):
            return False
        if os.path.basename(rel)[:-3] in _EXECUTOR_MODULES:
            return False
        try:
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                old_tree = ast.parse(f.read())
            new_tree = ast.parse(new_texts.get(rel) or "")
        except Exception:
            return False
        if len(old_tree.body) != len(new_tree.body):
            return False
        for a, b in zip(old_tree.body, new_tree.body):
            if dump(a) == dump(b):
                continue
            if type(a) is not type(b):
                return False
            if isinstance(a, ast.ClassDef):
                if not _class_body_ok(a, b):
                    return False
            elif not isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False  # module-level statements could reach shared state
    return True


def os_names(os_):
    return bound_names(os_.get("", []))


def importers_of_module(an, rel):
    """Tests that may import module `rel` (by any route): all tests touching it plus name refs."""
    mp = an.mp
    out = mp.cover(rel, 1, 10 ** 6)
    if rel.startswith("tests/"):
        out |= mp.test_file_tests(rel)
    base = os.path.basename(rel)[:-3]
    if base != "__init__":
        out |= mp.name_tests(base)
    return out



_line_nodes = {}
_STMT_FIELDS = ("body", "orelse", "handlers", "finalbody", "cases")


def stmt_at(tree, line):
    """Innermost statement whose range contains `line` (or None)."""
    best = None
    body = tree.body
    while True:
        nxt = None
        for st in body:
            if isinstance(st, ast.stmt) and st.lineno <= line <= st.end_lineno:
                nxt = st
                break
            if isinstance(st, ast.ExceptHandler) and st.lineno <= line <= st.end_lineno:
                nxt = st
                break
        if nxt is None:
            return best
        if isinstance(nxt, ast.stmt):
            best = nxt
        children = []
        for f in _STMT_FIELDS:
            v = getattr(nxt, f, None)
            if isinstance(v, list):
                children.extend(v)
        if hasattr(nxt, "cases"):
            for c in nxt.cases or []:
                children.extend(c.body)
        if not children:
            return best
        body = children


def nodes_on_line(rel, tree, line):
    """(node, parent, stmt) for expression nodes of the statement at `line` (not nested statements)."""
    key = (rel, id(tree), line)
    if key in _line_nodes:
        return _line_nodes[key]
    st = stmt_at(tree, line)
    out = []
    if st is not None:
        stack = [(c, st) for f, v in ast.iter_fields(st) if f not in _STMT_FIELDS
                 for c in (v if isinstance(v, list) else [v]) if isinstance(c, ast.AST)]
        while stack:
            n, par = stack.pop()
            if isinstance(n, ast.stmt):
                continue
            if getattr(n, "lineno", None) is not None and n.lineno <= line <= (n.end_lineno or n.lineno):
                out.append((n, par, st))
            for c in ast.iter_child_nodes(n):
                stack.append((c, n))
    _line_nodes[key] = out
    return out


def consumer_tests(mp, name, seen=None, depth=0):
    """Tests that execute a line *using* `name`'s value. Lines that only hand the value on
    (x = self.NAME, f(k=self.NAME), self.k = k) are followed to the new name instead."""
    if seen is None:
        seen = set()
    if name in seen:
        return set()
    seen.add(name)
    refs = mp.name_refs(name)
    if not refs or depth > 5 or name in GENERIC_NAMES or len(name) < 4:
        return mp.name_tests(name)
    out = set()
    derived = set()
    for f, lines in refs.items():
        tree = old_tree_of(f)
        if tree is None:
            return mp.name_tests(name)
        seen_nodes = set()
        for ln in lines:
          for node, par, st in nodes_on_line(f, tree, ln):
            if id(node) in seen_nodes:
                continue
            if not ((isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.ctx, ast.Load)) or
                    (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)) or
                    (isinstance(node, ast.Constant) and node.value == name)):
                continue
            seen_nodes.add(id(node))
            handed = None
            if isinstance(par, ast.keyword) and par.arg:
                handed = {par.arg}
            elif isinstance(par, (ast.Assign, ast.AnnAssign)) and par.value is node:
                tg = par.targets if isinstance(par, ast.Assign) else [par.target]
                names = set()
                for t in tg:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
                    elif isinstance(t, ast.Attribute):
                        names.add(t.attr)
                    else:
                        names = None
                        break
                handed = names
            if handed:
                derived |= handed
                continue
            out |= mp.cover(f, st.lineno, st.end_lineno)
    for d in derived:
        out |= consumer_tests(mp, d, seen, depth + 1)
    # tests that name it directly in test code
    out |= mention_word_tests(mp, name)
    return out


_word_cache = {}


def mention_word_tests(mp, word):
    if word not in _word_cache:
        mention_search(mp, "x/__none__")  # loads test sources
        rx = re.compile(r"\b%s\b" % re.escape(word))
        hits = set()
        for f, src in _test_sources.items():
            if word in src and rx.search(src):
                hits |= set(mp.by_file[f])
        _word_cache[word] = hits
    return _word_cache[word]


def _is_noop_stmt(st):
    return isinstance(st, ast.Pass) or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))


def simple_assign_targets(stmts):
    """Names assigned by plain class-body assignments, or None if any statement is something else."""
    names = set()
    for st in stmts:
        if _is_noop_stmt(st):
            continue
        if isinstance(st, ast.Assign):
            tg = st.targets
        elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
            tg = [st.target]
        else:
            return None
        for t in tg:
            if not isinstance(t, ast.Name):
                return None
            names.add(t.id)
    return names


_SAFE_CLASS_DECORATORS = {"trait", "mypyc_attr"}


def narrowable_class(mp, rel, qual, oc, attrs):
    """Attribute-level narrowing relies on the value being read by name. Python consumes some class
    attributes implicitly: dunders (__slots__, __hash__, ...), enum members, dataclass fields."""
    if any(a.startswith("__") and a.endswith("__") for a in attrs):
        return False
    node = oc.get(qual, (None,))[0]
    parts = qual.split(".")
    for i in range(len(parts), 0, -1):  # the class and its enclosing classes
        n = oc.get(".".join(parts[:i]), (None,))[0]
        if n is None:
            continue
        for d in n.decorator_list:
            name = d.id if isinstance(d, ast.Name) else (d.func.id if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) else
                                                          getattr(getattr(d, "func", d), "attr", None))
            if name not in _SAFE_CLASS_DECORATORS:
                return False
    if node is not None:
        for b in node.bases:
            bn = b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
            if "Enum" in bn or bn in ("AutoName", "NamedTuple", "TypedDict"):
                return False
    mro = mp.m["mro"].get(class_key(rel, qual), [])
    if any(m.endswith(":AutoName") for m in mro):
        return False
    return True


def derived_closure(oc, qual, attrs, stmts):
    """Add class-body names computed from the changed ones (SHOW_TRIE = new_trie(... SHOW_PARSERS))."""
    node = oc.get(qual, (None,))[0]
    bodies = [node.body] if node is not None else []
    bodies.append([st for st in stmts])
    attrs = set(attrs)
    changed = True
    while changed:
        changed = False
        for body in bodies:
            for st in body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                used = {n.id for n in ast.walk(st) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                if used & attrs:
                    tg = st.targets if isinstance(st, ast.Assign) else [getattr(st, "target", None)]
                    for t in tg:
                        if isinstance(t, ast.Name) and t.id not in attrs:
                            attrs.add(t.id)
                            changed = True
    return attrs


def _root_attr(node, first):
    """For klass.X..., klass.X[...], klass.X.method: return X if rooted at `first`, else None/False."""
    chain = []
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        if isinstance(node, ast.Attribute):
            chain.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            node = node.func
    if isinstance(node, ast.Name) and node.id == first and chain:
        return chain[-1]
    return False if isinstance(node, ast.Name) else None


def meta_taint(fn, attr):
    """Taint analysis inside a metaclass method: which klass attributes are computed from `attr`?
    Returns (dangerous, derived). Dangerous when the value may escape anywhere else."""
    first = fn.args.args[0].arg if fn.args.args else None
    if first is None:
        return True, set()
    if fn.name == "__new__":  # the class being created: klass = super().__new__(...)
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and \
                    isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "__new__" and \
                    len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                first = n.targets[0].id
                break
    tainted = set()
    derived = set()

    def reads(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and n.attr == attr:
                return True
            if isinstance(n, ast.Constant) and n.value == attr:
                return True
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in tainted:
                return True
        return False

    def visit(body, ctx):
        bad = False
        for st in body:
            if isinstance(st, (ast.If, ast.While)):
                c = ctx or reads(st.test)
                bad |= visit(st.body, c) | visit(st.orelse, c)
            elif isinstance(st, (ast.For, ast.AsyncFor)):
                c = ctx or reads(st.iter)
                if c:
                    for n in ast.walk(st.target):
                        if isinstance(n, ast.Name):
                            tainted.add(n.id)
                bad |= visit(st.body, c) | visit(st.orelse, c)
            elif isinstance(st, (ast.With, ast.Try)):
                bad |= visit(getattr(st, "body", []), ctx)
                for h in getattr(st, "handlers", []):
                    bad |= visit(h.body, ctx)
                bad |= visit(getattr(st, "orelse", []), ctx) | visit(getattr(st, "finalbody", []), ctx)
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if reads(st):
                    bad = True
            else:
                if not (ctx or reads(st)):
                    continue
                if isinstance(st, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                    for t in tg:
                        for n in (t.elts if isinstance(t, ast.Tuple) else [t]):
                            if isinstance(n, ast.Name):
                                tainted.add(n.id)
                            else:
                                r = _root_attr(n, first)
                                if r:
                                    derived.add(r)
                                else:
                                    bad = True
                elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
                    r = _root_attr(st.value.func, first)
                    if r:
                        derived.add(r)
                    else:
                        bad = True
                elif isinstance(st, ast.Return):
                    pass
                else:
                    bad = True
        return bad

    bad = False
    for _ in range(3):  # loops: iterate to a fixpoint
        before = (len(tainted), len(derived))
        bad = visit(fn.body, False)
        if bad or (len(tainted), len(derived)) == before:
            break
    return bad, derived


def read_at_import_in_code(mp, attr, cls_key=None, derived=None):
    """Is `attr` named inside a function body that runs at import time in a way that can carry its
    value into other state? Metaclass reads of the form `klass.B = f(klass.A)` add B to `derived`
    instead. Ordinary functions running at import only matter if the changed class was in use then."""
    for f, ls in mp.name_refs(attr).items():
        il = mp.imp.get(f, ())
        hits = [l for l in ls if l in il]
        if not hits:
            continue
        t = old_tree_of(f)
        if t is None:
            return True
        for l in hits:
            kind, q, nd = enclosing(t, l)
            if kind != "func":
                continue
            if cls_key is None:
                return True
            owner = q.rsplit(".", 1)[0] if "." in q else ""
            owner_node = scope_units(t)[1].get(owner, (None,))[0] if owner else None
            is_meta = (owner_node is not None and any(
                (isinstance(b, ast.Name) and b.id == "type") for b in owner_node.bases)) or \
                nd.name in ("__init_subclass__", "__set_name__")
            if is_meta:
                bad, der = meta_taint(nd, attr)
                if bad:
                    return True
                if derived is not None:
                    derived.update(der)
                continue
            if mp.used_at_import(cls_key):
                return True
    return False


def class_change(an, rel, qual, oc, why, stmts=None, old_stmts=()):
    an.non_body = True
    mp = an.mp
    an.reimport = True
    key = class_key(rel, qual)
    users, found = mp.class_users(key)
    attrs = simple_assign_targets(stmts) if stmts else None
    if attrs is not None and not attrs:
        attrs = None
    if attrs and not narrowable_class(mp, rel, qual, oc, attrs):
        attrs = None
    dangerous = False
    if attrs:
        attrs = derived_closure(oc, qual, attrs, stmts)
        seen_a = set()
        while True:
            todo = attrs - seen_a
            if not todo:
                break
            for a in todo:
                seen_a.add(a)
                extra = set()
                if read_at_import_in_code(mp, a, key, extra):
                    dangerous = True
                attrs |= extra
            if dangerous or len(attrs) > 40:
                dangerous = True
                break
    ar_res = None
    if attrs and AR_LEVEL and mp.ar and stmts:
        ar_res = ar_class_readers(an, rel, qual, key, attrs, stmts, old_stmts)
        if ar_res is not None and (dangerous or not ATTR_LEVEL):
            an.add(ar_res, "ar-readers " + qual + " " + ",".join(sorted(attrs)))
            return
    if attrs and ATTR_LEVEL and not dangerous:
        narrowed = set()
        for a in attrs:
            narrowed |= consumer_tests(mp, a) if CONSUMER_LEVEL else mp.name_tests(a)
        # the changed lines themselves (lambdas in dict literals run at test time)
        for st in old_stmts:
            narrowed |= mp.cover(rel, st.lineno, st.end_lineno)
        if ar_res is not None:
            # both are supersets of the affected tests: so is their intersection
            stmt_cover = set()
            for st in old_stmts:
                stmt_cover |= mp.cover(rel, st.lineno, st.end_lineno)
            an.add(ar_res & ((users & narrowed) | stmt_cover), "ar-readers&attr " + qual + " " + ",".join(sorted(attrs)))
            return
        an.add(users & narrowed, "class-attr-users " + qual + " " + ",".join(sorted(attrs)))
        for st in old_stmts:
            an.add(mp.cover(rel, st.lineno, st.end_lineno), "class-stmt-cover")
        found = found or bool(users)
        users = set()  # already added (narrowed)
        if "." in qual:
            an.add(mp.class_users(class_key(rel, qual.rsplit(".", 1)[0]))[0] & narrowed, "outer-attr-users")
            stmts_for_outer = None
        _copies(an, rel, qual, stmts)
        if not found:
            module_change(an, rel, None, None, oc, None, None, "class without recorded users " + qual)
        return
    an.add(users, "class-users " + qual)
    node = oc.get(qual, (None,))[0]
    if node is not None:
        lo, hi = node_range(node)
        an.add(mp.cover(rel, lo, hi), "class-cover " + qual)
    short = qual.split(".")[-1]
    if "." not in qual and short not in GENERIC_NAMES and short not in ("Tokenizer", "Parser", "Generator"):
        an.add(mp.name_tests(short), "class-name-refs " + short)
    _copies(an, rel, qual, stmts)
    if rel.startswith("tests/"):
        an.add(mp.test_file_tests(rel), "test-class")
    # nested class: the outer class's metaclass may copy attributes from it
    if "." in qual:
        class_change(an, rel, qual.rsplit(".", 1)[0], oc, "outer of " + qual)
    if not found and not users:
        module_change(an, rel, None, None, oc, None, None, "class without recorded users " + qual)


def ar_class_readers(an, rel, qual, key, attrs, stmts, old_stmts):
    """Tests that read one of `attrs` resolving to class `key` (or via super()) while running,
    from the attribute-read profile; None when the profile cannot decide."""
    mp = an.mp
    old_ids = {id(s) for s in old_stmts}
    new_stmts = [s for s in stmts if id(s) not in old_ids]
    t_old = simple_assign_targets(list(old_stmts))
    t_new = simple_assign_targets(new_stmts)
    if not t_old or t_old != t_new:
        return None  # attributes added or removed: lookups elsewhere in the MRO change
    if any(isinstance(s, ast.AugAssign) for s in stmts):
        return None
    out = set()
    for a in attrs:
        if a not in mp.ar_names:
            return None
        k = "%s|%s" % (key, a)
        if k in mp.ar_import or ("*|" + a) in mp.ar_import:
            return None  # consumed while modules import (derived state)
        out |= mp.ar_readers(k) | mp.ar_readers("*|" + a)
        out |= mention_word_tests(mp, a)  # test code reads it directly (not instrumented)
    for st in old_stmts:  # lambdas inside the changed statements run at test time
        out |= mp.cover(rel, st.lineno, st.end_lineno)
    return out


def _copies(an, rel, qual, stmts):
    mp = an.mp
    if stmts and an.depth_guard < 6:
        # attributes copied at import time into *other* classes (e.g. KEYWORDS = {**X.Tokenizer.KEYWORDS})
        an.depth_guard += 1
        for n in bound_names(stmts):
            if n in GENERIC_NAMES:
                continue
            for f, ls in mp.name_refs(n).items():
                il = mp.imp.get(f, ())
                hits = [l for l in ls if l in il]
                if not hits:
                    continue
                t = old_tree_of(f)
                if t is None:
                    continue
                units = None
                owners = set(qual.split("."))
                for l in hits[:200]:
                    if not reads_attr_of(t, l, n, owners, f):
                        continue
                    kind, q, nd = enclosing(t, l)
                    if kind == "class" and not (f == rel and (q == qual or q.startswith(qual + "."))):
                        if units is None:
                            units = scope_units(t)
                        key2 = (f, q)
                        if key2 not in an.visited:
                            an.visited.add(key2)
                            class_change(an, f, q, units[1], "copies %s.%s" % (qual, n))
        an.depth_guard -= 1


_attr_cache = {}


def reads_attr_of(tree, line, attr, owners, rel="?"):
    """Does `line` contain `<owner>.<attr>` (owner = changed class or its outer class)?"""
    for node, par, st in nodes_on_line(rel, tree, line):
        if isinstance(node, ast.Attribute) and node.attr == attr:
            v = node.value
            name = v.id if isinstance(v, ast.Name) else (v.attr if isinstance(v, ast.Attribute) else None)
            if name in owners:
                return True
    return False


def module_simple_names(stmts):
    names = set()
    for st in stmts:
        if _is_noop_stmt(st):
            continue
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            for a in st.names:
                if a.name == "*":
                    return None
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(st, ast.Assign):
            for t in st.targets:
                if not isinstance(t, ast.Name):
                    return None
                names.add(t.id)
        elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
            if not isinstance(st.target, ast.Name):
                return None
            names.add(st.target.id)
        else:
            return None
    return names


def module_name_consumers(mp, rel, modname, name):
    """Tests running lines that use module-level `name` of `rel`: in `rel` itself, or in modules that
    import it from `rel` (other modules' same-named variables are different objects)."""
    refs = mp.name_refs(name)
    if not refs:
        return mp.name_tests(name)
    out = set()
    for f, lines in refs.items():
        t = old_tree_of(f)
        if t is None:
            return mp.name_tests(name)
        if f != rel and not imports_name_from(t, modname, name):
            continue
        for ln in lines:
            st = stmt_at(t, ln)
            if st is not None:
                out |= mp.cover(f, st.lineno, st.end_lineno)
            else:
                out |= mp.cover(f, ln, ln)
    out |= mention_word_tests(mp, name)
    return out


def imports_name_from(tree, modname, name):
    """Does this module import `name` from `modname` (or import the module itself)?"""
    last = modname.rsplit(".", 1)[-1]
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if mod == modname or (n.level and mod.endswith(last)) or modname.endswith("." + mod) and mod:
                if any(a.name in (name, "*") for a in n.names):
                    return True
            parent = modname.rsplit(".", 1)[0]
            if (mod == parent or (n.level and modname.endswith(mod))) and any(a.name == last for a in n.names):
                return True
        elif isinstance(n, ast.Import):
            if any(a.name == modname or a.name.startswith(modname + ".") for a in n.names):
                return True
    return False


def module_change(an, rel, old_tree, of, oc, os_, ns, why, stmts=None):
    an.non_body = True
    mp = an.mp
    an.reimport = True
    names = module_simple_names(stmts) if (stmts and ATTR_LEVEL and old_tree is not None) else None
    if names and any(n.startswith("__") and n.endswith("__") for n in names):
        names = None
    if names and not (names & GENERIC_NAMES) and not rel.endswith("__init__.py") \
            and not any(read_at_import_in_code(mp, n) for n in names):
        # same-module derived constants (TRIE = new_trie(MAPPING))
        derived = set(names)
        grow = True
        while grow:
            grow = False
            for st in os_.get("", []) if os_ else []:
                used = {x.id for x in ast.walk(st) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
                if used & derived:
                    for t in (st.targets if isinstance(st, ast.Assign) else [getattr(st, "target", None)]):
                        if isinstance(t, ast.Name) and t.id not in derived:
                            derived.add(t.id)
                            grow = True
        modname0 = module_name(rel)
        for n in derived:
            an.add(module_name_consumers(mp, rel, modname0, n), "module-name " + n)
        old_set = {dump(x) for x in os_.get("", [])} if os_ else set()
        for st in stmts:
            if dump(st) in old_set and hasattr(st, "lineno"):
                an.add(mp.cover(rel, st.lineno, st.end_lineno), "module-stmt-cover")
        # class bodies in this module that read the names at import time
        t = old_tree
        modname = module_name(rel)
        for n in derived:
            for f, ls in mp.name_refs(n).items():
                il = mp.imp.get(f, ())
                tt = old_tree_of(f) if f != rel else t
                if tt is None:
                    continue
                if f != rel and not imports_name_from(tt, modname, n):
                    continue
                units = None
                for l in ls:
                    if l not in il:
                        continue
                    kind, q, nd = enclosing(tt, l)
                    if kind == "class" and (f, q, l) not in an.visited:
                        an.visited.add((f, q, l))
                        if units is None:
                            units = scope_units(tt)
                        body_st = [x for x in nd.body if x.lineno <= l <= x.end_lineno]
                        class_change(an, f, q, units[1], "reads module name " + n, body_st, body_st)
        if rel.startswith("tests/"):
            an.add(mp.test_file_tests(rel), "test-module")
        return
    an.add(mp.cover(rel, 1, 10 ** 6), "module-cover")
    mod = module_name(rel)
    for k, sid in mp.m["inst"].items():
        if k.startswith(mod + ":"):
            an.add(mp.class_users(k)[0], "module-classes")
    base = os.path.basename(rel)[:-3]
    if base != "__init__":
        an.add(mp.name_tests(base), "module-name-refs")
    if stmts:
        for n in bound_names(stmts):
            if n not in GENERIC_NAMES:
                an.add(mp.name_tests(n), "module-names " + n)
        # statements that do work at import (calls, attribute stores) can touch anything
        for s in stmts:
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
                an.all("module-level call in " + rel)
    if rel.startswith("tests/"):
        an.add(mp.test_file_tests(rel), "test-module")
    if rel in ("sqlglot/__init__.py", "sqlglot/expressions/__init__.py", "sqlglot/dialects/__init__.py",
               "sqlglot/_typing.py", "sqlglot/errors.py", "sqlglot/helper.py"):
        an.all("foundational module " + rel)


def enclosing(tree, line):
    """Innermost def/class containing `line`: returns (kind, qualname, node)."""
    best = ("module", "", tree)

    def walk(body, prefix):
        nonlocal best
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                lo, hi = node_range(node)
                if lo <= line <= hi:
                    kind = "class" if isinstance(node, ast.ClassDef) else "func"
                    best = (kind, prefix + node.name, node)
                    walk(node.body, prefix + node.name + ".")
                    return
            elif hasattr(node, "body") and hasattr(node, "lineno") and node.lineno <= line <= node.end_lineno:
                walk(getattr(node, "body", []) + getattr(node, "orelse", []) + getattr(node, "finalbody", []) +
                     [h for hh in getattr(node, "handlers", []) for h in hh.body], prefix)
                return

    walk(tree.body, "")
    return best


_tree_cache = {}
_text_cache = {}
_units_cache = {}


def _old_text_of(rel):
    if rel not in _text_cache:
        try:
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                _text_cache[rel] = f.read()
        except Exception:
            _text_cache[rel] = None
    return _text_cache[rel]


def _units_cached(rel, tree):
    key = (rel, id(tree))
    if key not in _units_cache:
        _units_cache[key] = scope_units(tree)
    return _units_cache[key]


def touched_both(fp):
    """Old-file and new-file line numbers inside or next to every hunk change."""
    old_t, new_t = set(), set()
    for os_, ol, ns_, nl, body in fp.hunks:
        o = os_ if ol > 0 else os_ + 1
        n = ns_ if nl > 0 else ns_ + 1
        for tag, text, _ in body:
            if tag == " ":
                o += 1
                n += 1
            elif tag == "-":
                old_t.update((o - 1, o, o + 1))
                new_t.update((n - 1, n))
                o += 1
            else:
                new_t.update((n - 1, n, n + 1))
                old_t.update((o - 1, o))
                n += 1
    return old_t, new_t


def old_tree_of(rel):
    if rel not in _tree_cache:
        try:
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                _tree_cache[rel] = ast.parse(f.read())
        except Exception:
            _tree_cache[rel] = None
    return _tree_cache[rel]


def import_time_escalation(an, rel, qual, node, scope, depth):
    """A function that runs at import time changed: whatever it built is now different.
    Follow its callers to the classes/modules whose definitions call it."""
    mp = an.mp
    if depth > 4:
        an.all("import-time escalation too deep " + qual)
        return
    if depth == 0:
        an.esc_start = time.process_time()
    elif time.process_time() - getattr(an, "esc_start", time.process_time()) > ESC_BUDGET:
        an.all("import-time escalation budget " + qual)
        return
    if an.everything:
        return
    name = qual.split(".")[-1].split("#")[0]
    if scope:  # a method: treat as a change to its class (covers metaclass methods via users)
        tree = old_tree_of(rel)
        units = scope_units(tree)[1] if tree else {}
        class_change(an, rel, scope, units, "import-time method " + qual)
        if name.startswith("__") and name.endswith("__"):
            # dunder on a metaclass or base class: classes declaring it as metaclass/base are affected
            cls_short = scope.split(".")[-1]
            for f, ls in mp.name_refs(cls_short).items():
                t = old_tree_of(f)
                if t is None:
                    continue
                for l in ls[:400]:
                    kind, q, nd = enclosing(t, l)
                    if kind == "class":
                        class_change(an, f, q, scope_units(t)[1], "uses " + cls_short)
    refs = mp.name_refs(name)
    if not refs:
        if not scope:
            an.all("import-time function without refs " + qual)
        return
    for f, ls in refs.items():
        t = old_tree_of(f)
        if t is None:
            continue
        units = None
        for l in ls:
            kind, q, nd = enclosing(t, l)
            if f == rel and q == qual:
                continue
            if kind == "func":
                lo, hi = node_range(nd)
                an.add(mp.cover(f, lo, hi), "caller " + q)
                body_lo = nd.body[0].lineno if nd.body else nd.lineno
                if mp.import_time(f, body_lo, hi) and depth < 4:
                    if units is None:
                        units = scope_units(t)
                    sc = units[0].get(q, (None, ""))[1]
                    import_time_escalation(an, f, q, nd, sc, depth + 1)
            elif kind == "class":
                if units is None:
                    units = scope_units(t)
                class_change(an, f, q, units[1], "calls import-time " + name)
            else:
                if units is None:
                    units = scope_units(t)
                module_change(an, f, t, units[0], units[1], units[2], None, "module calls " + name)


def analyze_commit(mp, patch_text):
    an = Analysis(mp)
    files = parse_patch(patch_text)
    new_texts = {}  # rel -> new text (None = deleted)
    if not files:
        an.all("unparseable patch")
        return an, new_texts
    an.files = list(files)
    for fp in files:
        rel = fp.path
        old_rel = fp.old
        if fp.binary:
            an.non_body = True
            an.add(mp.opens(rel), "binary-opens")
            if rel.startswith(("sqlglot/", "tests/")) and not mp.opens(rel):
                an.all("binary file in source tree " + rel)
            continue
        old_text = None
        if not fp.new_file and old_rel:
            try:
                with open(os.path.join(REPO, old_rel), encoding="utf-8") as f:
                    old_text = f.read()
            except FileNotFoundError:
                old_text = None
            except UnicodeDecodeError:
                an.all("undecodable " + old_rel)
                continue
        try:
            new_text = None if fp.deleted else apply_patch(fp, old_text or "")
        except Exception as e:
            log("apply failed", rel, e)
            an.all("patch does not apply " + rel)
            continue
        if fp.old and fp.new and fp.old != fp.new:  # rename
            an.non_body = True
            new_texts[fp.old] = None
            an.add(mp.opens(fp.old), "renamed-from-opens")
            if fp.old.endswith(".py"):
                analyze_py(an, fp.old, old_text, None, _tree_cache)
        new_texts[rel] = new_text
        base = os.path.basename(rel)
        if (len(files) == 1 and fp.old == fp.new and old_text and new_text and
                rel.startswith("sqlglot/dialects/") and rel.endswith(".py") and
                any(field in old_text for field in mp.token_reads)):
            from token_coverage import changed_reads
            tree = old_tree_of(rel)
            match = changed_reads(mp.token_reads, module_name(rel), old_text, new_text, tree) if tree else None
            if match is not None:
                field, ids = match
                readers = {mp.number_by_id[t] for t in ids if t in mp.number_by_id}
                readers.update(mp.opens(rel))
                readers.update(mention_word_tests(mp, field))
                readers.update(mp.unknown_tests)
                an.non_body = an.reimport = True
                an.add(readers, "tokenizer lookup coverage")
                continue
        if rel in CONFIG_FILES or base == "conftest.py":
            if "pytest" in patch_text or base == "conftest.py":
                an.all("pytest config " + rel)
            continue
        if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            if fp.new_file and old_text is None:
                # a new module: only matters if something imports it
                an.non_body = True
                an.reimport = True
                an.add(mp.name_tests(os.path.basename(rel)[:-3]), "new-module-name")
                if rel.startswith("tests/") and os.path.basename(rel).startswith("test_"):
                    pass  # new tests are not in tests.json
                if base == "__init__.py" or rel.startswith("sqlglot/dialects/") or \
                        rel.startswith("sqlglot/parsers/") or rel.startswith("sqlglot/generators/"):
                    an.add(mp.name_tests(os.path.basename(os.path.dirname(rel))), "new-module-pkg")
                continue
            analyze_py(an, rel, old_text, new_text, _tree_cache, fp)
            continue
        # data files
        readers = mp.opens(rel)
        an.data_readers |= readers
        if rel.endswith(".py"):
            an.non_body = True
        if readers:
            an.add(readers, "readers")
            an.reimport = an.reimport or rel.endswith(".py")
            continue
        if rel.startswith("tests/") or rel.startswith("sqlglot/"):
            # unknown data file in the source tree: tests that mention it or its folder
            hits = mention_search(mp, rel)
            an.add(hits, "mentions")
            if rel.startswith("sqlglot/") and not rel.endswith((".md", ".txt", ".typed")):
                an.all("unknown file in sqlglot package " + rel)
            continue
        # anything else (docs, CHANGELOG, benchmarks, posts, .github, sqlglotc...) is not read by tests
        if rel.startswith("sqlglotc/"):
            an.add(set(mp.by_file.get("tests/test_sqlglotc.py", [])), "sqlglotc")
    return an, new_texts


_test_sources = None


def mention_search(mp, rel):
    global _test_sources
    if _test_sources is None:
        _test_sources = {}
        for f in mp.by_file:
            try:
                with open(os.path.join(REPO, f), encoding="utf-8") as h:
                    _test_sources[f] = h.read()
            except Exception:
                _test_sources[f] = ""
    out = set()
    base = os.path.basename(rel)
    stem = os.path.splitext(base)[0]
    parent = os.path.basename(os.path.dirname(rel))
    for f, src in _test_sources.items():
        if base in src or (len(stem) > 3 and stem in src):
            out |= set(mp.by_file[f])
    return out


# --------------------------------------------------------------------------- runtime
class Runner:
    """Warm parent: sqlglot and every test module imported once. Each commit runs in a fork."""

    def __init__(self, tests, mp=None, plans=()):
        self.tests = tests
        self.mp = mp
        self.plans = plans
        self.by_number = {t["number"]: t for t in tests}
        self._duck_conn = None
        self._cg = None
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import syncpool
        syncpool.install()
        sys.path.insert(0, REPO)
        os.chdir(REPO)
        self.warm()
        import gc
        gc.collect()
        gc.freeze()  # children never scan the parent's objects (map, modules) in gc.collect()

    def warm(self):
        """Import only what the planned runs need: their test modules (and so their libraries)
        and the sqlglot modules that swap mode will patch in place."""
        import importlib
        import unittest  # noqa
        importlib.import_module("sqlglot")
        if self.mp is None:  # tools: no plan, import everything
            self.base_import_failures, self.failed_imports = set(), set()
            for f in sorted({t["file"] for t in self.tests}):
                try:
                    importlib.import_module(f[:-3].replace("/", "."))
                except Exception:
                    self.failed_imports.add(f[:-3].replace("/", "."))
            import pkgutil
            import sqlglot
            for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
                if not info.name.endswith("__main__"):
                    try:
                        importlib.import_module(info.name)
                    except Exception:
                        self.base_import_failures.add(info.name)
            return
        base_fail = self.mp.m.get("base_fail", {})
        self.base_import_failures = set(base_fail.get("sqlglot", []))
        self.failed_imports = {f[:-3].replace("/", ".") for f in base_fail.get("tests", [])}
        mods = set()
        swap_mods = set()
        for p in self.plans:
            for n in p["run"]:
                mods.add(self.by_number[n]["file"][:-3].replace("/", "."))
            if p["an"].swap and not p["an"].reimport:
                swap_mods.update(p["an"].swap)
        for name in sorted(mods):
            if name in self.failed_imports:
                continue
            try:
                importlib.import_module(name)
            except Exception as e:
                log("warm import failed", name, e)
                self.failed_imports.add(name)
        for name in sorted(swap_mods):
            try:
                importlib.import_module(name)
            except Exception as e:
                log("warm swap import failed", name, e)

    # -- in the child -------------------------------------------------------
    @staticmethod
    def _install_sources(new_texts):
        import importlib.abc
        import importlib.util

        py = {}
        data = {}
        for rel, text in new_texts.items():
            ab = os.path.join(REPO, rel)
            if rel.endswith(".py"):
                py[module_name(rel)] = (ab, text, rel.endswith("__init__.py"))
            else:
                data[ab] = text

        class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
            def find_spec(self, fullname, path, target=None):
                if fullname in py:
                    ab, text, pkg = py[fullname]
                    if text is None:
                        return None
                    spec = importlib.util.spec_from_loader(fullname, self, origin=ab, is_package=pkg)
                    spec.has_location = True
                    if pkg:
                        spec.submodule_search_locations = [os.path.dirname(ab)]
                    return spec
                return None

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                ab, text, pkg = py[module.__name__]
                module.__file__ = ab
                code = compile(text, ab, "exec", dont_inherit=True)
                exec(code, module.__dict__)

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname in py and py[fullname][1] is None:
                    raise ModuleNotFoundError("No module named %r" % fullname)
                return None

        sys.meta_path.insert(0, Blocker())
        sys.meta_path.insert(0, Finder())
        if data:
            real_open = io.open

            def patched_open(file, mode="r", *a, **k):
                try:
                    ab = os.path.abspath(os.fspath(file)) if not isinstance(file, int) else None
                except TypeError:
                    ab = None
                if ab in data and "r" in mode and "+" not in mode:
                    text = data[ab]
                    if text is None:
                        raise FileNotFoundError(2, "No such file or directory", file)
                    if "b" in mode:
                        return io.BytesIO(text.encode("utf-8"))
                    return io.StringIO(text)
                return real_open(file, mode, *a, **k)

            builtins.open = patched_open
            io.open = patched_open

    @staticmethod
    def _swap(swaps, new_texts):
        """Replace function code in already-imported modules. Returns False if not possible."""
        import types
        for mod, quals in swaps.items():
            module = sys.modules.get(mod)
            rel = [r for r in new_texts if r.endswith(".py") and module_name(r) == mod]
            if module is None or not rel:
                return False
            ab = os.path.join(REPO, rel[0])
            try:
                code = compile(new_texts[rel[0]], ab, "exec", dont_inherit=True)
            except SyntaxError:
                return False
            by_q = {}
            stack = [code]
            while stack:
                c = stack.pop()
                for k in c.co_consts:
                    if isinstance(k, types.CodeType):
                        by_q.setdefault(k.co_qualname, []).append(k)
                        stack.append(k)
            for q in quals:
                if "#" in q:
                    return False
                cands = by_q.get(q, [])
                if len(cands) != 1:
                    return False
                newc = cands[0]
                obj = module
                parts = q.split(".")
                for p in parts[:-1]:
                    obj = getattr(obj, p, None)
                    if obj is None:
                        return False
                f = (obj.__dict__ if isinstance(obj, type) else vars(obj)).get(parts[-1])
                f = Runner._unwrap(f, q)
                if f is None or len(newc.co_freevars) != len(f.__code__.co_freevars):
                    return False
                f.__code__ = newc
        return True

    @staticmethod
    def _unwrap(f, q):
        import types
        seen = 0
        while f is not None and seen < 6:
            seen += 1
            if isinstance(f, types.FunctionType):
                if f.__code__.co_qualname == q:
                    return f
                f = getattr(f, "__wrapped__", None)
                continue
            if isinstance(f, (staticmethod, classmethod)):
                f = f.__func__
            elif isinstance(f, property):
                for g in (f.fget, f.fset, f.fdel):
                    if g is not None and getattr(g, "__code__", None) is not None and g.__code__.co_qualname == q:
                        return g
                return None
            elif hasattr(f, "func") and hasattr(f, "attrname"):  # cached_property
                f = f.func
            elif hasattr(f, "__wrapped__"):
                if hasattr(f, "cache_clear"):
                    f.cache_clear()
                f = f.__wrapped__
            else:
                return None
        return None

    def _patch_tpcds(self, module, units):
        """test_execute_tpcds in the child: optional query filter, early stop, and DuckDB results
        reused when the patched sqlglot emits exactly the pinned DuckDB SQL."""
        keep = None
        fq = []
        gq = set()
        dq = []
        budget = None
        uc = {"q": {}, "setup": 1000}
        for n, us in (units or {}).items():
            if self.by_number[n]["id"].endswith("::test_execute_tpcds"):
                keep = {int(u[1:]) for u in us if u.startswith("q")}
                fq = [int(u[1:]) for u in us if u.startswith("f") or u.startswith("g")]
                gq = {int(u[1:]) for u in us if u.startswith("g")}
                dq = [int(u[1:]) for u in us if u.startswith("d")]
                budget = next((float(u[1:]) for u in us if u.startswith("b")), None)
                uc = UNIT_COSTS.get(self.by_number[n]["id"]) or uc
        special = next((u for n, values in (units or {}).items()
                        if self.by_number[n]["id"].endswith("::test_execute_tpcds")
                        for u in values if u.startswith("replay:")), None)
        if special:
            import tpcds_replay as replay
            try:
                kind = special.split(":", 1)[1]
                if kind != "fixture":
                    import importlib
                    name, *changed = kind.split("|")
                    passed = replay.rule_passes(name, importlib.import_module("sqlglot.optimizer." + name), changed)
                    if passed:
                        return "skip"
                    if not fq:
                        return "include"
                else:
                    keep = replay.changed_queries(module)
                    if keep is None:
                        return "include"
                    if not keep:
                        return "skip"
            except Exception:
                return "include"
        cache = {}
        try:
            import pickle
            with real_io_open(TPCDS_CACHE, "rb") as f:
                cache = pickle.load(f)
        except Exception as e:  # no cache: DuckDB runs for real
            log("tpcds cache unavailable:", repr(e)[:100])
        if dq:
            bad = self._tpcds_duck_check(module, dq, cache)
            log("tpcds duckdb-side check:", len(dq), "queries,", len(bad), "differ")
            if not bad:
                return "skip"
            keep = (keep or set()) | bad
            fq = []
        if fq:
            qcost = lambda q: uc["q"].get(str(q), UNIT_MS)  # noqa
            fq.sort(key=lambda q: -qcost(q))
            base = uc["setup"] + sum(qcost(q) for q in keep)
            bad = self._tpcds_fp_check(module, fq, cache, base, qcost, budget, gq)
            if bad is None:
                log("tpcds fingerprint check: over budget, including the test")
                return "include"
            log("tpcds fingerprint check:", len(fq), "queries,", len(bad), "differ")
            keep |= bad
            if not keep:
                return "skip"  # every affected query provably unchanged: the test passes
        if keep is not None:
            orig = module.load_sql_fixture_pairs

            def load_sql_fixture_pairs(filename):
                items = list(orig(filename))
                if "tpc-ds" in filename:
                    return [x for i, x in enumerate(items) if i in keep]
                return items
            module.load_sql_fixture_pairs = load_sql_fixture_pairs

        cls = module.TestExecutor
        orig_ce = cls.cached_execute

        def cached_execute(self, sql, tpch=True):
            if tpch:
                return orig_ce(self, sql, tpch)
            if sql not in self.cache:
                duck_sql = module.transpile(sql, write="duckdb")[0]
                hit = cache.get(duck_sql)
                self.cache[sql] = hit.copy() if hit is not None else self.tpcds_conn.execute(duck_sql).fetchdf()
            return self.cache[sql]
        cls.cached_execute = cached_execute

        # Early stop: the test passes only if every query matches, so stop at the first mismatch.
        # parse/execute errors already end the original test; only subtest assertions continue.
        orig_mp = cls._mp_execute

        def _mp_execute(self, schema, tables, sqls, tpch):
            if tpch:
                return orig_mp(self, schema, tables, sqls, tpch)
            with module.ProcessPoolExecutor(mp_context=module.get_context("spawn"),
                                            initializer=module.initializer,
                                            initargs=(schema, tables)) as pool:
                for i, (args, sql, _) in enumerate(sqls):
                    table = pool.submit(module.mp_execute, module.parse_one(sql), args).result()
                    if table is not None:
                        _, sql_i, _ = self.tpcds_sqls[i]
                        a = self.cached_execute(sql_i, tpch=False)
                        b = module.pd.DataFrame(
                            ((module.np.nan if c is None else c for c in r) for r in table.rows),
                            columns=table.columns,
                        )
                        module.assert_frame_equal(a, b, check_dtype=False, check_index_type=False)
        cls._mp_execute = _mp_execute
        return "run"

    def _duck_same(self, module, i, new_sql, cache):
        """True when DuckDB returns exactly the pinned result table for query i's new DuckDB SQL."""
        old_sql = (cache.get("__duck__") or {}).get(i)
        old = cache.get(old_sql) if old_sql else None
        if old is None:
            return False
        if self._duck_conn is None:
            import duckdb
            conn = duckdb.connect()
            for table, columns in module.TPCDS_SCHEMA.items():
                conn.execute(f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{module.DIR_TPCDS}{table}.csv.gz', delim='|', header=True, columns={columns})
                """)
            self._duck_conn = conn
        new = self._duck_conn.execute(new_sql).fetchdf()
        return list(new.columns) == list(old.columns) and new.dtypes.tolist() == old.dtypes.tolist() \
            and new.equals(old)

    def _tpcds_duck_check(self, module, idxs, cache):
        """The executor side is untouched (only non-executor dialect modules changed): a query passes
        when its DuckDB SQL is unchanged, or DuckDB returns exactly the pinned table for the new SQL."""
        bad = set()
        try:
            items = list(module.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
        except BaseException:  # noqa
            return set(idxs)
        for i in idxs:
            try:
                args, sql, _ = items[i]
                if not args.get("execute"):
                    continue
                new_sql = module.transpile(sql, write="duckdb")[0]
                if new_sql in cache and new_sql == (cache.get("__duck__") or {}).get(i):
                    continue
                if not self._duck_same(module, i, new_sql, cache):
                    bad.add(i)
            except BaseException:  # noqa
                bad.add(i)
        return bad

    def _codegen_same(self, i, tree):
        """The patched planner builds the pinned plan, and the patched code generator writes the
        pinned Python code for every expression the pinned executor generated."""
        import pickle
        if self._cg is None:
            import gzip
            try:
                with gzip.open(os.path.join(_HERE, "cg", "codegen.pkl.gz"), "rb") as f:
                    self._cg = pickle.load(f)
            except Exception:
                self._cg = {}
        rec = self._cg.get(i)
        if not rec or not rec.get("ok") or not rec.get("plan"):
            return False
        import tpcds_fp
        from sqlglot.planner import Plan
        from sqlglot.executor.python import Python
        if tpcds_fp.plan_fingerprint(Plan(tree)) != rec["plan"]:
            return False
        gen = Python().generator(identify=True, comments=False)
        for blob, ga, gk, sql in rec["calls"]:
            if gen.generate(pickle.loads(blob), *ga, **gk) != sql:
                return False
        return True

    def _tpcds_fp_check(self, module, idxs, cache, base=0.0, qcost=None, budget=None, gq=()):
        """Queries whose patched parse / DuckDB SQL / optimized tree differ from the pinned commit
        (or that error or time out). The others reach the executor's runtime phase with identical
        input, and the caller already knows no changed line runs in that phase for them."""
        import pickle
        fps = cache.get("__fp__")
        if not fps:
            return set(idxs)
        try:
            sys.path.insert(0, _HERE)
            import tpcds_fp
        except Exception:
            return set(idxs)
        bad = set()

        class FpTimeout(Exception):
            pass

        def on_alarm(signum, frame):
            raise FpTimeout()
        old = signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL, 30)
        try:
            items = list(module.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
            need = base
            for i in idxs:
                if budget is not None and bad and need > budget:
                    return None
                try:
                    args, sql, _ = items[i]
                    e = pickle.loads(pickle.dumps(module.parse_one(sql)))
                    if args.get("execute"):
                        new_sql = module.transpile(sql, write="duckdb")[0]
                        if new_sql not in cache and not self._duck_same(module, i, new_sql, cache):
                            bad.add(i)
                            need += qcost(i) if qcost else 0
                            continue
                        tree = tpcds_fp.optimized(e, module.TPCDS_SCHEMA)
                        if tpcds_fp.tree_fingerprint(tree) != fps.get(i) or \
                                (i in gq and not self._codegen_same(i, tree)):
                            bad.add(i)
                            need += qcost(i) if qcost else 0
                except FpTimeout:
                    raise
                except BaseException:  # noqa
                    bad.add(i)
                    need += qcost(i) if qcost else 0
            if budget is not None and bad and need > budget:
                return None
        except BaseException:  # noqa
            bad = set(idxs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
        return bad

    def _patch_fixture_cases(self, module, units, current):
        """Fixture-case filter for optimizer check_file tests, active only while that test runs."""
        keep = {}
        for n, us in (units or {}).items():
            per = {}
            for u in us:
                if u.startswith("c:"):
                    fname, i = u[2:].rsplit(":", 1)
                    per.setdefault(fname, set()).add(int(i))
            if per:
                keep[n] = per
        if not keep:
            return
        orig = module.load_sql_fixture_pairs

        def load_sql_fixture_pairs(filename):
            k = keep.get(current.get("num"), {}).get(filename)
            for i, item in enumerate(orig(filename), start=1):
                if k is None or i in k:
                    yield item
        module.load_sql_fixture_pairs = load_sql_fixture_pairs

    @staticmethod
    def _purge():
        for name in list(sys.modules):
            if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
                del sys.modules[name]

    def child_run(self, numbers, new_texts, swaps, reimport, wfd, deadline):
        import importlib
        import unittest
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        results = {"failed": [], "error": None, "mode": None, "unrun": []}
        try:
            self._install_sources(new_texts)
            mode = "swap"
            if reimport or not swaps or not self._swap(swaps, new_texts):
                mode = "reimport"
                if any(r.endswith(".py") for r in new_texts):
                    self._purge()
            results["mode"] = mode
            if mode == "reimport":
                bad = []
                for rel in new_texts:
                    if rel.endswith(".py") and rel.startswith("sqlglot/") and new_texts[rel] is not None:
                        try:
                            importlib.import_module(module_name(rel))
                        except BaseException as e:  # noqa
                            bad.append("%s: %r" % (rel, e))
                try:
                    importlib.import_module("sqlglot")
                except BaseException as e:  # noqa
                    bad.append("sqlglot: %r" % (e,))
                if self.import_check and not bad:
                    import pkgutil
                    try:
                        import sqlglot as _sg
                        for info in pkgutil.walk_packages(_sg.__path__, "sqlglot."):
                            if info.name.endswith("__main__") or info.name in self.base_import_failures:
                                continue
                            try:
                                importlib.import_module(info.name)
                            except BaseException as e:  # noqa
                                bad.append("%s: %r" % (info.name, e))
                    except BaseException as e:  # noqa
                        bad.append("walk: %r" % (e,))
                    for f in sorted({t["file"] for t in self.tests}):
                        name = f[:-3].replace("/", ".")
                        if name in self.failed_imports:
                            continue
                        try:
                            importlib.import_module(name)
                        except BaseException:
                            results.setdefault("bad_test_files", []).append(f)
                if bad:
                    results["import_error"] = bad[:3]
            numbers = sorted(numbers)  # tests.json order == pytest order
            by_mod = {}
            for n in numbers:
                t = self.by_number[n]
                parts = t["id"].split("::")
                by_mod.setdefault(t["file"][:-3].replace("/", "."), []).append((n, parts[1], parts[2]))
            failed = set()
            id_to_num = {}
            current = {}
            suite = unittest.TestSuite()
            for modname, items in by_mod.items():
                try:
                    module = importlib.import_module(modname)
                except BaseException:
                    failed |= {n for n, _, _ in items}
                    continue
                if modname == "tests.test_optimizer":
                    self._patch_fixture_cases(module, self.units, current)
                if modname == "tests.test_generator":
                    for n, us in (self.units or {}).items():
                        if self.by_number[n]["id"].endswith("::test_generator_memory"):
                            names = {u[3:] for u in us if u.startswith("dl:")}
                            if names:
                                # only these dialects can construct generators differently
                                module.Dialects = [d for d in module.Dialects if d.value in names]
                if modname == "tests.test_executor" and any(
                        self.by_number[n]["id"].endswith("::test_execute_tpcds") for n, _, _ in items):
                    verdict = self._patch_tpcds(module, self.units)
                    if verdict in ("skip", "include"):
                        tp = {x[0] for x in items if self.by_number[x[0]]["id"].endswith("::test_execute_tpcds")}
                        items = [x for x in items if x[0] not in tp]
                        if verdict == "include":
                            failed |= tp
                for n, cls, meth in items:
                    try:
                        case = getattr(module, cls)(meth)
                    except BaseException:
                        failed.add(n)
                        continue
                    id_to_num[case.id()] = n
                    suite.addTest(case)
            by_cls = {}
            for tid, n in id_to_num.items():
                by_cls.setdefault(tid.rsplit(".", 1)[0], []).append(n)

            class Timeout(Exception):
                pass

            def on_alarm(signum, frame):
                raise Timeout("test timed out")

            signal.signal(signal.SIGALRM, on_alarm)
            seconds = {n: self.by_number[n]["seconds"] for n in numbers}
            ran = set()

            class R(unittest.TestResult):
                def _num(self, test):
                    n = id_to_num.get(test.id())
                    if n is None:
                        desc = str(test)
                        m = re.search(r"\((.*)\)", desc)
                        if m:  # class/module fixture failure: fail every test in it
                            key = m.group(1)
                            for k, ns in by_cls.items():
                                if k == key or k.startswith(key + "."):
                                    failed.update(ns)
                    return n

                def startTest(self, test):
                    super().startTest(test)
                    n = id_to_num.get(test.id())
                    if MEASURE:
                        now = time.process_time()
                        meas["gap_" + test.id().rsplit(".", 1)[0]] = meas.get("gap_" + test.id().rsplit(".", 1)[0], 0) + now - meas["last"]
                        meas["last"] = now
                    current["num"] = n
                    if n is not None:
                        ran.add(n)
                        if time.time() > deadline:
                            failed.add(n)  # out of time: include it rather than guess
                            raise Timeout("out of budget")
                        budget = max(5.0, 30 * seconds.get(n, 0.1))
                        signal.setitimer(signal.ITIMER_REAL, budget)

                def stopTest(self, test):
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    if MEASURE:
                        now = time.process_time()
                        meas[test.id()] = now - meas["last"]
                        meas["last"] = now
                    super().stopTest(test)

                def addError(self, test, err):
                    n = self._num(test)
                    if n is not None:
                        failed.add(n)

                def addFailure(self, test, err):
                    n = self._num(test)
                    if n is not None:
                        failed.add(n)

                def addSubTest(self, test, subtest, err):
                    if err is not None:
                        n = id_to_num.get(test.id())
                        if n is not None:
                            failed.add(n)

                def addUnexpectedSuccess(self, test):
                    n = self._num(test)
                    if n is not None:
                        failed.add(n)

            result = R()
            meas = {"last": time.process_time()}
            try:
                suite.run(result)
            except BaseException as e:  # noqa
                results["error"] = repr(e)
            results["unrun"] = sorted(set(id_to_num.values()) - ran)
            results["failed"] = sorted(failed)
            if MEASURE:
                results["meas"] = meas
        except BaseException as e:
            results["error"] = repr(e)
            results["failed"] = sorted(numbers)
        ru = resource.getrusage(resource.RUSAGE_SELF)
        results["cpu"] = ru.ru_utime + ru.ru_stime
        path = "/tmp/blast_res_%d.json" % os.getpid()
        with real_io_open(path + ".tmp", "w") as f:
            f.write(json.dumps(results))
        os.rename(path + ".tmp", path)
        os._exit(0)

    def start(self, numbers, new_texts, swaps, reimport, deadline, units=None, import_check=False):
        self.units = units or {}
        self.import_check = import_check
        r, w = 0, 0
        ru = resource.getrusage(resource.RUSAGE_SELF)
        self.cpu_at_fork = ru.ru_utime + ru.ru_stime
        pid = os.fork()
        if pid == 0:
            try:
                self.child_run(numbers, new_texts, swaps, reimport, w, deadline)
            finally:
                os._exit(1)
        return pid, pid


def read_all(pid):
    path = "/tmp/blast_res_%d.json" % pid
    try:
        with real_io_open(path, "rb") as f:
            data = f.read()
        os.unlink(path)
        return data
    except OSError:
        return b""


# --------------------------------------------------------------------------- packing
# Scoring machines charge ~2.8x the CPU of the machine cpu.json was measured on
# (two practice submissions: server CPU / local CPU on the same work).
CPU_FACTOR = float(os.environ.get("CPU_FACTOR", "1.9"))  # measured: 4.1 s local -> +7.5 s charged
RUN_OVERHEAD_MS = float(os.environ.get("RUN_OVERHEAD_MS", "60"))
NEVER_RUN_MS = float(os.environ.get("NEVER_RUN_MS", "15000"))
P_ANY = float(os.environ.get("P_ANY", "0.75"))
UNIT_SETUP_MS = float(os.environ.get("UNIT_SETUP_MS", "2000"))
UNIT_MS = float(os.environ.get("UNIT_MS", "400"))
TPCDS_FP_LEVEL = os.environ.get("TPCDS_FP_LEVEL", "1") == "1"
TPCDS_DUCK_LEVEL = os.environ.get("TPCDS_DUCK_LEVEL", "1") == "1"
TPCDS_CG_LEVEL = os.environ.get("TPCDS_CG_LEVEL", "1") == "1"
GENMEM_LEVEL = os.environ.get("GENMEM_LEVEL", "1") == "1"
GENMEM_DIALECT_MS = float(os.environ.get("GENMEM_DIALECT_MS", "140"))
DUCK_BASE_MS = float(os.environ.get("DUCK_BASE_MS", "300"))
DUCK_QUERY_MS = float(os.environ.get("DUCK_QUERY_MS", "4"))
FP_BASE_MS = float(os.environ.get("FP_BASE_MS", "250"))
FP_QUERY_MS = float(os.environ.get("FP_QUERY_MS", "37"))
FP_EXEC_PASS = float(os.environ.get("FP_EXEC_PASS", "0.25"))
FP_P_EQUAL = float(os.environ.get("FP_P_EQUAL", "0.5"))


UNIT_COSTS = {}


def load_cpu(tests, ms):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpu.json")
    try:
        with open(path) as f:
            c = json.load(f)
    except Exception:
        c = {"test": {}, "class": {}}
    cpu_ms = {t["number"]: c["test"].get(t["id"], ms[t["number"]] * 1.5) for t in tests}
    UNIT_COSTS.update(c.get("units", {}))
    return cpu_ms, c["class"]


def choose(cands, ms, cpu_ms, class_ms, by_number, an, p_over=None):
    """Split candidates into (run, blind) minimising estimated CPU + selection cost."""
    if not cands:
        return set(), set()
    never_run = {n for n in cands if ms[n] >= NEVER_RUN_MS and cpu_ms[n] >= NEVER_RUN_MS}
    pool = [n for n in cands if n not in never_run]

    def est(run):
        if not run:
            return 0.0
        c = sum(cpu_ms[n] for n in run) * CPU_FACTOR
        classes = {by_number[n]["id"].rsplit("::", 1)[0] for n in run}
        c += sum(class_ms.get(k, 0) for k in classes)
        return c + (RUN_OVERHEAD_MS if an.reimport else RUN_OVERHEAD_MS / 4)

    best = None
    orders = [sorted(pool, key=lambda n: -ms[n]),
              sorted(pool, key=lambda n: -(cpu_ms[n] - ms[n])),
              sorted(pool, key=lambda n: -(cpu_ms[n] * CPU_FACTOR - ms[n])),
              sorted(pool, key=lambda n: -cpu_ms[n])]
    # ~55% of commits break something among their candidates (samples + synthetic patches);
    # spread that over the candidates as an independent per-test probability.
    p_fail = 1 - (1 - P_ANY) ** (1.0 / max(1, len(cands)))

    def expected_output(run, blind):
        base = pack_cost(blind, ms)
        if not run:
            return base
        po = p_over or {}
        extra = sum(po.get(n, p_fail) * (ms[n] + (2000 if n in po else 0)) for n in run)
        p_any = 1 - (1 - p_fail) ** len(run)
        if not blind:
            extra += 2000 * p_any
        return base + extra

    for order in orders:
        L = len(order)
        ks = sorted(set(range(0, min(L, 40) + 1)) | set(range(max(0, L - 40), L + 1)))
        for k in reversed(ks):  # small runs first: they set a low bound that prunes the big ones
            run = set(order[k:])
            e = est(run)
            if best is not None and e >= best[0]:
                continue
            blind = set(order[:k]) | never_run
            cost = e + expected_output(run, blind)
            if best is None or cost < best[0]:
                best = (cost, run, blind)
    return best[1], best[2]


def pack_cost(numbers, ms):
    bins = pack(numbers, ms)
    if not bins:
        return 0
    return max(sum(ms[n] for n in b) for b in bins) + 2000 * len(bins)


def _best_two_way(items, ms):
    """Exact split into two workers minimising the larger load (subset-sum bitset)."""
    total = sum(ms[n] for n in items)
    reach = 1
    hist = []
    for n in items:
        hist.append(reach)
        reach |= reach << ms[n]
    target = total // 2
    mask = reach & ((1 << (target + 1)) - 1)
    s = mask.bit_length() - 1
    a = []
    for k in range(len(items) - 1, -1, -1):
        w = ms[items[k]]
        if s >= w and (hist[k] >> (s - w)) & 1:
            a.append(items[k])
            s -= w
    aset = set(a)
    return [a, [n for n in items if n not in aset]]


def pack(numbers, ms):
    """Choose the worker count and split minimising max load + 2 s per worker."""
    if not numbers:
        return []
    items = sorted(numbers, key=lambda n: -ms[n])
    total = sum(ms[n] for n in items)
    longest = ms[items[0]]
    best_cost = total + 2000
    best_bins = [items]
    for k in range(2, min(8, len(items)) + 1):
        # Adding workers can never shorten the longest test; the average load
        # is a second lower bound. Do no construction for dominated counts.
        if longest + 2000 * k >= best_cost:
            break
        if (total + k - 1) // k + 2000 * k >= best_cost:
            continue
        if k == 2 and len(items) <= 400 and total <= 200000:
            bins = _best_two_way(items, ms)
            load = max(sum(ms[n] for n in b) for b in bins)
        else:
            loads = [0] * k
            bins = [[] for _ in range(k)]
            for n in items:
                i = loads.index(min(loads))
                loads[i] += ms[n]
                bins[i].append(n)
            load = max(loads)
        cost = load + 2000 * k
        if cost < best_cost:
            best_cost, best_bins = cost, bins
    return [b for b in best_bins if b]


# --------------------------------------------------------------------------- main
def plan_commit(mp, cid, text, ms, cpu_ms, class_ms, by_number, all_numbers):
    try:
        an, new_texts = analyze_commit(mp, text)
    except Exception as e:  # never crash: fall back to everything
        log("analysis crashed", cid, repr(e))
        an = Analysis(mp)
        an.all("crash")
        new_texts = {}
    cands = set(all_numbers) if an.everything else set(an.cands)
    cands |= set(mp.unknown_tests) if (an.cands or an.everything) else set()
    setup_failures = set()
    if len(new_texts) == 1 and not an.everything:
        rel, source = next(iter(new_texts.items()))
        if rel.startswith("tests/fixtures/optimizer/tpc-") and rel.endswith(".sql"):
            import tpcds_replay as replay
            omit, broken = replay.fixture_effect(REPO, rel, _old_text_of(rel), source)
            cands.difference_update(mp.number_by_id[tid] for tid in omit if tid in mp.number_by_id)
            if broken:
                setup_failures = {n for n, t in by_number.items()
                                  if t["id"].startswith("tests/test_executor.py::TestExecutor::")}
                cands.difference_update(setup_failures)
    unit_filter = {}
    local_cpu = cpu_ms
    p_over = {}
    duck_only = None
    for un in mp.unit_test_nums & cands:
        tid = by_number[un]["id"]
        if TPCDS_DUCK_LEVEL and tid.endswith("::test_execute_tpcds") and not an.everything and mp.tpcds_rt() \
                and os.path.exists(TPCDS_CACHE):
            if duck_only is None:
                duck_only = duck_side_only(an, new_texts)
            if duck_only:
                qs = sorted(mp.tpcds_rt())
                unit_filter[un] = ["d%d" % q for q in qs]
                if local_cpu is cpu_ms:
                    local_cpu = dict(cpu_ms)
                local_cpu[un] = DUCK_BASE_MS + DUCK_QUERY_MS * len(qs) + 0.25 * 3000
                p_over[un] = 0.25
                cls_key = tid.rsplit("::", 1)[0]
                if not any(n != un and by_number[n]["id"].rsplit("::", 1)[0] == cls_key for n in cands):
                    class_ms = dict(class_ms)
                    class_ms[cls_key] = class_ms.get(cls_key, 0) * 0.25
                an.reasons.append("tpcds-duckdb-side %d" % len(qs))
                continue
        if (TPCDS_FP_LEVEL and tid.endswith("::test_execute_tpcds") and not an.everything and not an.non_body
                and an.body_lines and un not in an.data_readers and mp.tpcds_rt() and os.path.exists(TPCDS_CACHE)):
            us_fp = mp.affected_units(un, an.body_lines)
            if us_fp is not None:
                if not us_fp:
                    cands.discard(un)
                    continue
                rt = mp.tpcds_rt()
                changed = [(rel, l) for rel, ls in an.body_lines for l in ls]
                E, F, G = [], [], []
                cgl = mp.codegen_lines() if TPCDS_CG_LEVEL else {}
                for u in us_fp:
                    if not u.startswith("q"):
                        E = None
                        break
                    q = int(u[1:])
                    r = rt.get(q)
                    if r is None or not any(l in r.get(rel, ()) for rel, l in changed):
                        F.append(q)
                        continue
                    ph = cgl.get(q)
                    if ph is not None and not any(l in ph["data"].get(rel, ()) for rel, l in changed) and \
                            all(l in ph["plan"].get(rel, ()) or l in ph["cg"].get(rel, ()) or
                                l not in r.get(rel, ()) for rel, l in changed):
                        G.append(q)  # only planning / code generation touched: replay them exactly
                    else:
                        E.append(q)
                if E is not None:
                    if local_cpu is cpu_ms:
                        local_cpu = dict(cpu_ms)
                    budget = FP_EXEC_PASS * ms[un] / CPU_FACTOR
                    unit_filter[un] = ["q%d" % q for q in sorted(E)] + ["f%d" % q for q in sorted(F)] + \
                        ["g%d" % q for q in sorted(G)] + ["b%d" % budget]
                    F = F + G
                    uc = UNIT_COSTS.get(tid) or {"q": {}, "setup": 1000}
                    total_units = max(1, mp.unit_count(un))
                    full = sum(uc["q"].get(str(q), UNIT_MS) for q in E)
                    if E and uc["setup"] + full > budget:
                        unit_filter.pop(un)
                        if local_cpu is not cpu_ms:
                            local_cpu[un] = cpu_ms[un]
                        local_cpu = dict(local_cpu)
                        local_cpu[un] = 10 ** 9  # never worth running: include it
                        continue
                    p_u = (0.15 + 0.5 * len(E) / total_units) if E else 0.0
                    p_u = 1 - (1 - p_u) * (FP_P_EQUAL if F else 1.0)
                    c = FP_BASE_MS + FP_QUERY_MS * len(F) * (FP_P_EQUAL + (1 - FP_P_EQUAL) * 0.3)
                    cls_key = tid.rsplit("::", 1)[0]
                    if not E and not any(n != un and by_number[n]["id"].rsplit("::", 1)[0] == cls_key for n in cands):
                        # the class setup (CSV loading) only runs if a fingerprint differs
                        class_ms = dict(class_ms)
                        class_ms[cls_key] = class_ms.get(cls_key, 0) * (1 - FP_P_EQUAL) * 0.3
                    if E:
                        c += uc["setup"] + (1 - p_u) * full + p_u * min(full, 1500)
                    local_cpu[un] = c
                    p_over[un] = p_u
                    an.reasons.append("tpcds-fp E=%d F=%d" % (len(E), len(F)))
                    continue
        if un in an.units_all or an.everything:
            continue
        us = mp.affected_units(un, an.line_sets)
        if us is None:
            continue
        if not us:
            cands.discard(un)
            continue
        unit_filter[un] = sorted(us)
        if local_cpu is cpu_ms:
            local_cpu = dict(cpu_ms)
        uc = UNIT_COSTS.get(by_number[un]["id"])
        total_units = max(1, mp.unit_count(un))
        p_u = 0.15 + 0.5 * len(us) / total_units
        if uc:
            full = sum(uc["q"].get(u[1:], UNIT_MS) for u in us if u.startswith("q"))
            setup = uc["setup"]
        else:  # fixture cases: share of the test's measured CPU
            full, setup = cpu_ms[un] * len(us) / total_units, 50
        # early stop: a failing run usually ends within the first few queries
        local_cpu[un] = setup + (1 - p_u) * full + p_u * min(full, 1500)
        p_over[un] = p_u
    gm = next((n for n in cands if by_number[n]["id"].endswith("::test_generator_memory")), None)
    if GENMEM_LEVEL and gm is not None and not an.everything:
        if duck_only is None:
            duck_only = duck_side_only(an, new_texts)
        dmods = _dialect_modules() if duck_only else None
        if dmods:
            changed_files = {fp.path for fp in an.files}
            affected = sorted(d for d, mods in dmods.items() if changed_files & set(mods))
            if affected:
                unit_filter[gm] = ["dl:" + d for d in affected]
                if local_cpu is cpu_ms:
                    local_cpu = dict(cpu_ms)
                local_cpu[gm] = 100 + GENMEM_DIALECT_MS * len(affected)
                p_over[gm] = 0.15
                an.reasons.append("genmem-dialects %d" % len(affected))
    run, blind = choose(cands, ms, local_cpu, class_ms, by_number, an, p_over)
    if len(new_texts) == 1 and not an.everything:
        import tpcds_replay as replay
        rel, source = next(iter(new_texts.items()))
        mode = None
        if rel == replay.FIXTURE and source is not None:
            mode = "fixture"
        elif rel.startswith("sqlglot/optimizer/") and source and an.swap and not an.non_body and not an.reimport:
            rule = rel.rsplit("/", 1)[-1].removesuffix(".py")
            if (rule in replay.RULES and os.path.isfile(os.path.join(replay.CACHE_DIR, rule + ".pkl"))
                    and replay.local_edit(_old_text_of(rel), source)):
                mode = "|".join([rule, *an.swap[module_name(rel)]])
        if mode:
            for n in mp.unit_test_nums & (blind | run):
                if not by_number[n]["id"].endswith("::test_execute_tpcds"):
                    continue
                # If omitting the test barely changes the bill, just include it.
                if n in blind and pack_cost(blind, ms) - pack_cost(blind - {n}, ms) < 4000:
                    continue
                fallback = []
                if n in run and mode != "fixture":
                    # A wide plan fingerprint reoptimizes each query from scratch.
                    # Try the narrower rule boundary first and keep the original
                    # verifier when the rule produces a different result.
                    fallback = unit_filter.get(n, [])
                    if sum(u.startswith("f") for u in fallback) < 24:
                        continue
                run.add(n)
                blind.discard(n)
                unit_filter[n] = ["replay:" + mode, *fallback]
                an.reasons.append("TPC-DS replay " + mode)
    unit_filter = {k: v for k, v in unit_filter.items() if k in run}
    if an.import_check and not run and not an.everything:
        pick = min(cands, key=lambda n: cpu_ms[n]) if cands else min(all_numbers, key=lambda n: cpu_ms[n])
        run = {pick}
        blind.discard(pick)
    blind.update(setup_failures)
    return {"id": cid, "an": an, "new": new_texts, "run": run, "blind": blind, "units": unit_filter, "cands": cands}


def finish_result(p, res, mp, all_numbers):
    """Failed set for a finished child (res = parsed result or None)."""
    if res is None:
        return set(p["run"])
    failed = set(res.get("failed", [])) | set(res.get("unrun", []))
    if res.get("error"):
        log(p["id"], "child error", str(res["error"])[:200])
    if res.get("import_error"):
        log(p["id"], "patched module fails to import:", res["import_error"])
        failed |= all_numbers
    for f in res.get("bad_test_files", []):
        log(p["id"], "test module fails to import:", f)
        failed |= set(mp.by_file.get(f, []))
    return failed


def main():
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)  # anything else printed to stdout goes to stderr
    sys.stdout = sys.stderr
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    all_numbers = {t["number"] for t in tests}
    log("start cpu %.2f" % _cpu())
    mp = Map(MAP_PATH, tests)
    log("map loaded cpu %.2f" % _cpu())
    by_number = {t["number"]: t for t in tests}
    cpu_ms, class_ms = load_cpu(tests, ms)
    commits = sorted(os.listdir(os.path.join(DATA, "commits")))
    commits = [c for c in commits if c.endswith(".patch")]
    plans = []
    for c in commits:
        cid = c[:-6]
        with open(os.path.join(DATA, "commits", c), encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
        plans.append(plan_commit(mp, cid, text, ms, cpu_ms, class_ms, by_number, all_numbers))
        an = plans[-1]["an"]
        cands = plans[-1]["cands"]
        run, blind = plans[-1]["run"], plans[-1]["blind"]

        if DEBUG:
            log(cid, "cands", len(cands), "run", len(run), "blind", sorted(blind), an.reasons[:12])
    log("analysis done in %.2fs cpu %.2f" % (time.time() - T0, _cpu()))

    runner = None
    if any(p["run"] for p in plans):
        runner = Runner(tests, mp, plans)
    log("warm done in %.2fs cpu %.2f" % (time.time() - T0, _cpu()))
    pending = [p for p in plans if p["run"]]
    active = {}
    deadline = T0 + WALL_BUDGET
    while pending or active:
        while pending and len(active) < PARALLEL:
            p = pending.pop(0)
            if time.time() > deadline:
                p["failed"] = set(p["run"])  # no time left: include everything we would have run
                continue
            pid, fd = runner.start(p["run"], p["new"], p["an"].swap, p["an"].reimport, deadline, p["units"],
                                   p["an"].import_check)
            active[pid] = (p, fd, time.time())
        if not active:
            break
        pid, status = os.waitpid(-1, os.WNOHANG)
        if pid == 0:
            now = time.time()
            for apid, (ap, afd, started) in list(active.items()):
                if now - started > CHILD_MAX or now > deadline + 20:
                    log(ap["id"], "child killed after %.1fs" % (now - started))
                    try:
                        os.kill(apid, signal.SIGKILL)
                    except OSError:
                        pass
            time.sleep(0.005)
            continue
        if pid not in active:
            continue
        p, fd, _ = active.pop(pid)
        raw = read_all(fd)
        try:
            res = json.loads(raw.decode())
            p["mode"] = res.get("mode")
            p["cpu"] = res.get("cpu")
        except Exception:
            log(p["id"], "child died", status)
            res = None
        failed = finish_result(p, res, mp, all_numbers)
        p["failed"] = failed
    for p in plans:
        sel = set(p.get("failed", set())) | p["blind"]
        out.write(json.dumps({"commit": p["id"], "workers": pack(sel, ms)}) + "\n")
        if DEBUG:
            log(p["id"], p.get("mode"), "run", len(p["run"]), "est_ms", int(sum(cpu_ms[n] for n in p["run"])),
                "cpu", p.get("cpu"), "selected", sorted(sel)[:20])
    out.flush()
    ru = resource.getrusage(resource.RUSAGE_SELF)
    rc = resource.getrusage(resource.RUSAGE_CHILDREN)
    log("done in %.2fs wall, cpu self %.2f children %.2f" % (time.time() - T0, ru.ru_utime + ru.ru_stime,
                                                             rc.ru_utime + rc.ru_stime))


if __name__ == "__main__":
    main()
