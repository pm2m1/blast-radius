# Blast Radius entry

`blast.py` picks, for each sqlglot patch, the tests that can fail.

1. **Candidates** come from a map built once at the pinned commit (`tools/`):
   per-test line coverage (setup/teardown kept per class), files each test opens,
   sqlglot classes each test instantiates or looks up, identifier references, and
   the test-module import graph. A function body change selects the tests that
   execute the changed statements (or the whole function when the change alters
   the compiled shape). Class/module-level changes select the class's users.
   Comment/docstring-only changes select only tests that read the file as text.
2. **Verification**: cheap candidates are actually run against the patched code in
   a forked child of a warm process (patched modules served by an import hook,
   patched data files by an `open` hook, process pools replaced by an in-process
   executor that still pickles across the boundary). Only failures are kept.
3. **Expensive** candidates are included without running when that is cheaper.
4. Selections are packed into workers to minimise busiest worker + 2 s/worker.

Anything uncertain widens the selection. No answers are precomputed per patch.

Rebuild the map: see `tools/build_map.py` (runs sqlglot's suite under coverage).
