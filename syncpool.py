"""In-process stand-in for ProcessPoolExecutor that keeps the process boundary's
pickling (arguments and results are round-tripped through pickle)."""
import pickle
from concurrent.futures import Future


class SyncPicklingExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs)
        self._inited = False

    def _ensure_init(self):
        if not self._inited:
            self._inited = True
            fn, args = self._init
            if fn is not None:
                fn, args = pickle.loads(pickle.dumps((fn, args)))
                fn(*args)

    def submit(self, fn, /, *args, **kwargs):
        f = Future()
        try:
            self._ensure_init()
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            r = fn(*args, **kwargs)
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()
        return False


def install():
    import concurrent.futures
    import concurrent.futures.process
    concurrent.futures.ProcessPoolExecutor = SyncPicklingExecutor
    concurrent.futures.process.ProcessPoolExecutor = SyncPicklingExecutor
