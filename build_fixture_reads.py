"""Record which executor tests consume the query lists loaded in class setup."""

import ast
import json
import os
import sys
import unittest
from pathlib import Path


def build(repo, output):
    sys.path.insert(0, str(repo))
    os.chdir(repo)
    os.environ["SKIP_INTEGRATION"] = "0"
    import syncpool

    syncpool.install()
    from tests.test_executor import TestExecutor

    tree = ast.parse((repo / "tests/test_executor.py").read_text())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TestExecutor"
    )
    setup = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "setUpClass"
    )
    fields = {}
    for st in setup.body:
        if not isinstance(st, ast.Assign) or len(st.targets) != 1:
            continue
        target, call = st.targets[0], st.value
        if not (
            isinstance(target, ast.Attribute)
            and ast.unparse(target.value) == "cls"
            and isinstance(call, ast.Call)
            and ast.unparse(call.func) == "list"
            and len(call.args) == 1
        ):
            continue
        load = call.args[0]
        if (
            isinstance(load, ast.Call)
            and ast.unparse(load.func) == "load_sql_fixture_pairs"
            and len(load.args) == 1
            and isinstance(load.args[0], ast.Constant)
        ):
            fields["tests/fixtures/" + load.args[0].value] = target.attr

    by_path = {str(repo / f): f for f in fields}
    current = None
    in_setup = False
    seen = set()
    reads = {f: set() for f in fields}

    def audit(event, args):
        if event == "open" and not in_setup and args[0] in by_path:
            reads[by_path[args[0]]].add(current)

    class Observed:
        def __init__(self, path, value):
            self.path, self.value = path, value

        def __get__(self, instance, owner):
            reads[self.path].add(current)
            return self.value

    original = TestExecutor.setUpClass

    def install(cls):
        nonlocal in_setup
        in_setup = True
        try:
            original()
        finally:
            in_setup = False
        for path, attr in fields.items():
            setattr(cls, attr, Observed(path, getattr(cls, attr)))

    class Record(unittest.TestResult):
        def startTest(self, test):
            nonlocal current
            current = test.id().rsplit(".", 1)[1]
            seen.add(current)
            super().startTest(test)

        def stopTest(self, test):
            nonlocal current
            super().stopTest(test)
            current = None

    sys.addaudithook(audit)
    TestExecutor.setUpClass = classmethod(install)
    result = Record()
    unittest.defaultTestLoader.loadTestsFromTestCase(TestExecutor).run(result)
    excluded = {t.id().rsplit(".", 1)[1] for t, _ in result.skipped}
    profile = {}
    if result.wasSuccessful():
        for path in fields:
            if None not in reads[path]:
                profile[path] = sorted(
                    "tests/test_executor.py::TestExecutor::" + method
                    for method in seen - excluded - reads[path]
                )
    output.write_text(json.dumps(profile, sort_keys=True))
    print(
        "Executor fixture coverage:",
        {p: len(ids) for p, ids in profile.items()},
        file=sys.stderr,
    )


if __name__ == "__main__":
    build(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
