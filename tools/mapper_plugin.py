"""pytest plugin used only at map-build time (offline). Records per test:
coverage lines (via coverage contexts), files opened, sqlglot classes instantiated."""
import os, sys, json, threading

REPO = os.environ["MAPPER_REPO"]
OUT = os.environ["MAPPER_OUT"]
_state = {"ctx": None}
_opens = {}
_inst = {}
_cov = None


def _audit(event, args):
    if event == "open" and _state["ctx"]:
        p = args[0]
        if isinstance(p, bytes):
            p = p.decode(errors="replace")
        if isinstance(p, str):
            p = os.path.abspath(p)
            if p.startswith(REPO):
                _opens.setdefault(_state["ctx"], set()).add(os.path.relpath(p, REPO))


def _wrap_inits():
    import inspect, importlib, pkgutil, sqlglot
    mods = []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.startswith("sqlglot.executor") or True:
            try:
                mods.append(importlib.import_module(m.name))
            except Exception as e:
                print("mapper: cannot import", m.name, e, file=sys.stderr)
    seen = set()
    for mod in list(sys.modules.values()):
        if not getattr(mod, "__name__", "").startswith("sqlglot"):
            continue
        for obj in list(vars(mod).values()):
            if inspect.isclass(obj) and obj.__module__.startswith("sqlglot") and obj not in seen:
                seen.add(obj)
                init = obj.__dict__.get("__init__")
                if init is None or not inspect.isfunction(init):
                    continue

                def make(orig):
                    def __init__(self, *a, **k):
                        c = _state["ctx"]
                        if c:
                            t = type(self)
                            _inst.setdefault(c, set()).add(t.__module__ + ":" + t.__qualname__)
                        return orig(self, *a, **k)
                    __init__.__wrapped_orig__ = orig
                    __init__.__qualname__ = orig.__qualname__
                    __init__.__name__ = orig.__name__
                    return __init__
                setattr(obj, "__init__", make(init))
    # dialect instances are cached: record every lookup too
    from sqlglot.dialects.dialect import Dialect
    orig = Dialect.__dict__["get_or_raise"].__func__
    def get_or_raise(cls, *a, **k):
        d = orig(cls, *a, **k)
        c = _state["ctx"]
        if c and d is not None:
            t = d if isinstance(d, type) else type(d)
            _inst.setdefault(c, set()).add(t.__module__ + ":" + t.__qualname__)
        return d
    Dialect.get_or_raise = classmethod(get_or_raise)


def pytest_configure(config):
    global _cov
    if os.environ.get("PYTEST_XDIST_WORKER") is None and config.getoption("numprocesses", None):
        return  # xdist controller
    import coverage
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    _cov = coverage.Coverage(data_file=os.path.join(OUT, "cov.data"), data_suffix=wid,
                             source=[os.path.join(REPO, "sqlglot"), os.path.join(REPO, "tests")])
    _cov.start()
    _cov.switch_context("IMPORT")
    import syncpool; syncpool.install()
    sys.addaudithook(_audit)
    _wrap_inits()


UNIT_TESTS = ("test_execute_tpcds",)
_unit = {"q": -1}


def _unit_active():
    c = _state["ctx"]
    return c and "|" not in c and c.split("@")[0].endswith(UNIT_TESTS)


def _install_unit_hooks():
    import unittest, re as _re
    import tests.test_executor as te
    orig_parse = te.parse_one

    def parse_one(*a, **k):
        if _unit_active():
            _unit["q"] += 1
            _switch(_state["ctx"].split("@")[0] + "@q%d" % _unit["q"])
        return orig_parse(*a, **k)
    te.parse_one = parse_one
    orig_sub = unittest.TestCase.subTest
    import contextlib

    @contextlib.contextmanager
    def subTest(self, msg=None, **params):
        m = _re.match(r"tpc-ds (\d+)$", str(msg)) if _unit_active() else None
        if m:
            base = _state["ctx"].split("@")[0]
            _switch(base + "@q%d" % (int(m.group(1)) - 1))
            try:
                with orig_sub(self, msg, **params):
                    yield
            finally:
                _switch(base + "@post")
        else:
            with orig_sub(self, msg, **params):
                yield
    unittest.TestCase.subTest = subTest


def _switch(ctx):
    _state["ctx"] = ctx
    if _cov:
        _cov.switch_context(ctx)


def _set(ctx):
    _unit["q"] = -1
    _state["ctx"] = ctx
    os.environ["MAPPER_CTX"] = ctx or "IMPORT"
    if _cov:
        _cov.switch_context(ctx or "IMPORT")


import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set(item.nodeid + "|setup"); yield


def pytest_collection_finish(session):
    try:
        _install_unit_hooks()
    except Exception as e:
        print("mapper: unit hooks failed", e, file=sys.stderr)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set(item.nodeid); yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _set(item.nodeid + "|teardown"); yield; _set(None)


def pytest_unconfigure(config):
    if _cov is None:
        return
    _cov.stop(); _cov.save()
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(OUT, f"extra.{wid}.json"), "w") as f:
        json.dump({"opens": {k: sorted(v) for k, v in _opens.items()},
                   "inst": {k: sorted(v) for k, v in _inst.items()}}, f)
