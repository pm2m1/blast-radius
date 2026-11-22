"""Build query-input and optimizer-boundary checkpoints from the unmodified test."""

import functools
import inspect
import os
import pickle
import sys
import unittest
from pathlib import Path

from tpcds_replay import RULES, tree_digest


def build(repo, out):
    sys.path.insert(0, str(repo))
    os.chdir(repo)
    os.environ["SKIP_INTEGRATION"] = "0"
    from tpcds_replay import snapshot
    import syncpool

    syncpool.install()
    from sqlglot import executor
    from sqlglot.optimizer.optimizer import optimize, RULES as sequence
    from tests.test_executor import TestExecutor
    from tests.helpers import load_sql_fixture_pairs

    out.mkdir(parents=True, exist_ok=True)
    items = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    with (out / "inputs.pkl").open("wb") as f:
        pickle.dump([(bool(a.get("execute")), sql) for a, sql, _ in items], f)
    calls = {name: [] for name in RULES}
    invalid = set()
    active = []
    outside = {name: set() for name in RULES}
    files = {
        rule.__code__.co_filename: rule.__name__
        for rule in sequence
        if rule.__name__ in RULES
    }

    def observe(code, offset):
        name = files.get(code.co_filename)
        if name:
            for owner, seen in reversed(active):
                if name == owner:
                    seen.add(code.co_qualname)
                    break
            else:
                outside[name].add(code.co_qualname)
        # A function can run both inside and outside a rule. Keep monitoring it.
        if name is None:
            return sys.monitoring.DISABLE

    def wrap(fn):
        name = fn.__name__

        @functools.wraps(fn)
        def captured(expr, **kwargs):
            data = None
            if name not in invalid:
                try:
                    if any(
                        x is not None and type(x) not in (str, bool, int)
                        for x in kwargs.values()
                    ):
                        raise ValueError("mutable rule argument")
                    data = snapshot(expr, kwargs)
                    if tree_digest(pickle.loads(data)[0]) != tree_digest(expr):
                        raise ValueError("lossy expression checkpoint")
                except Exception as error:
                    print(
                        "checkpoint unavailable",
                        name,
                        str(error)[:120],
                        file=sys.stderr,
                    )
                    invalid.add(name)
            reached = set()
            active.append((name, reached))
            try:
                result = fn(expr, **kwargs)
            finally:
                active.pop()
            if data is not None and name not in invalid:
                try:
                    calls[name].append((reached, data, tree_digest(result)))
                except Exception as error:
                    print(
                        "checkpoint unavailable",
                        name,
                        str(error)[:120],
                        file=sys.stderr,
                    )
                    invalid.add(name)
            return result

        captured.__signature__ = inspect.signature(fn)
        return captured

    rules = tuple(wrap(fn) if fn.__name__ in RULES else fn for fn in sequence)

    def recording_optimize(*a, **kw):
        return optimize(*a, **kw, rules=rules)

    executor.optimize = recording_optimize
    tool = 4
    mon = sys.monitoring
    mon.use_tool_id(tool, "TPC-DS rule coverage")
    mon.register_callback(tool, mon.events.PY_START, observe)
    mon.set_events(tool, mon.events.PY_START)
    try:
        result = unittest.TestResult()
        unittest.TestSuite([TestExecutor("test_execute_tpcds")]).run(result)
        if not result.wasSuccessful() or result.skipped:
            raise RuntimeError(str(result.errors + result.failures)[:500])
    finally:
        mon.set_events(tool, 0)
        mon.free_tool_id(tool)
    for name, records in calls.items():
        if name not in invalid and records:
            with (out / (name + ".pkl")).open("wb") as f:
                pickle.dump((outside[name], records), f, protocol=5)
    print(
        "TPC-DS captured calls:",
        {k: len(v) for k, v in calls.items() if k not in invalid},
        file=sys.stderr,
    )


if __name__ == "__main__":
    build(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
