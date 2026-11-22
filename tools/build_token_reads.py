"""Profile value reads in the pinned Python tokenizer. Usage:
python tools/build_token_reads.py SQLGLOT_DIR map.json.gz token_reads.json
Run with the entry's locked dependencies and Python 3.13.
"""
import ast
import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytest


def main():
    repo, mapping, output = (Path(arg).resolve() for arg in sys.argv[1:])
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(repo))
    os.chdir(repo)
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokenizer_core import TokenizerCore

    rel = "sqlglot/tokenizer_core.py"
    tree = ast.parse((repo / rel).read_text())
    sites = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or not hasattr(TokenizerCore, fn.name):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
                continue
            receiver = ast.unparse(node.func.value)
            if receiver in ("self.numeric_escapes", "numeric_literals"):
                sites[node.lineno] = (fn.name, receiver.split(".")[-1].upper())
    if sorted(field for _, field in sites.values()) != ["NUMERIC_ESCAPES", "NUMERIC_ESCAPES", "NUMERIC_LITERALS"]:
        raise ValueError("Unexpected pinned tokenizer read sites")

    owners = defaultdict(set)
    known = defaultdict(set)
    for dialect in Dialect.classes.values():
        for cls in dialect.tokenizer_class.__mro__:
            for field in ("NUMERIC_ESCAPES", "NUMERIC_LITERALS"):
                if field in vars(cls):
                    target = cls.__module__ + ":" + cls.__qualname__
                    owners[(field, id(vars(cls)[field]))].add(target)
                    known[field].add(target)

    m = json.load(gzip.open(mapping))
    expected = {}
    for line in sites:
        if line in m["imp"].get(rel, []):
            raise ValueError("A value is consumed during import")
        sid = m["lines"][rel].get(str(line))
        expected[line] = set() if sid is None else {m["ids"][i] for i in m["sets"][sid]}
    wanted = sorted(set.union(*expected.values()))
    current = None
    seen = defaultdict(set)
    reads = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    outside = set()

    def observe(_code, line):
        if line not in sites:
            return
        _, field = sites[line]
        if current is None:
            outside.add(field)
            return
        frame = sys._getframe(1)
        loc = frame.f_locals
        seen[line].add(current)
        if field == "NUMERIC_ESCAPES":
            key = "0" if loc["is_octal"] else loc["peek"]
            targets = owners.get((field, id(loc["self"].numeric_escapes)), {"*"})
        else:
            # Athena copies and merges other dialects' suffix mappings at import.
            key = loc["numeric_literal"].upper()
            targets = {"*"}
        for target in targets:
            reads[field][target][key].add(current)

    class Context:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item):
            nonlocal current
            current = item.nodeid
            try:
                yield
            finally:
                current = None

    tool = 4
    mon = sys.monitoring
    mon.use_tool_id(tool, "tokenizer-lookup-coverage")
    mon.register_callback(tool, mon.events.LINE, observe)
    for fn, _ in sites.values():
        mon.set_local_events(tool, getattr(TokenizerCore, fn).__code__, mon.events.LINE)
    try:
        if pytest.main(["-q", "--disable-warnings", *wanted], plugins=[Context()]):
            raise RuntimeError("Baseline tests failed. No profile written.")
    finally:
        mon.free_tool_id(tool)
    out = {}
    for field in known:
        if field in outside:
            continue
        missing = set().union(*(expected[line] - seen[line] for line in sites if sites[line][1] == field))
        out[field] = {
            "owners": sorted(known[field]), "unobserved": sorted(missing),
            "reads": {owner: {key: sorted(ids) for key, ids in keys.items()} for owner, keys in reads[field].items()},
        }
    output.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
