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

Weak-mutation profile: the map also records, per test, whether swapping each comparison
operator (`<`/`<=`, `==`/`!=`, `is`/`==`, ...) would ever have changed that comparison's result during the
test's run at the pinned commit (both seeds, original results always returned). When a patch only swaps
operators, a test whose run never saw a difference is unaffected, step for step, and is not selected.

Anything uncertain widens the selection. No answers are precomputed per patch.

Rebuild the map: see `tools/build_map.py` (runs sqlglot's suite under coverage).

### Tokenizer lookup coverage

A value-only change to a literal tokenizer dictionary can preserve its keys,
order, and truth value. In the pinned Python tokenizer, numeric escape values
are consumed at two dictionary lookups, and numeric suffix values at one.
The dialect metaclass and generator also inspect escape key membership or
dictionary truth. Those observations remain equal for this restricted edit.

`token_reads.json` records the keys observed by baseline tests at the value
lookups. Escape mappings are tracked by object identity across inheritance.
Suffix observations are combined across dialects because some dialects copy
and merge these dictionaries. Source readers, tests that mention the field,
unrecognized tests, and tests with unresolved line coverage are retained.
Other edits use the ordinary selector, including changes to keys, compound
patches, and dictionaries with computed entries.

To rebuild the observations with Python 3.13 and the locked dependencies, run
`python tools/build_token_reads.py /path/to/pinned/sqlglot map.json.gz token_reads.json`.
The builder chooses tests using the existing base-commit line coverage and
requires them to pass. The selector reads only the resulting map at run time.

### Verifying the expensive TPC tests

The Docker build runs the pinned TPC-DS executor test and records inputs and
outputs at nine optimizer passes. For a single optimizer file with only a
literal or operator change in a function body, the selector can replay the
patched pass on the inputs that reached that function. It checks expression
types, ordered arguments, metadata, comments and parent links. If the output
changes, an observed use falls outside the pass, or verification exceeds its
budget, the test stays eligible. Cached DuckDB SQL used for a query fingerprint
must match the baseline SQL for that same query index.

For a change to a TPC-H or TPC-DS SQL fixture, the selector first runs the
pinned fixture reader on the edited file. Errors in shared setup retain the
whole executor class. Otherwise, a build-time profile identifies which
executor methods read the affected fixture. On a TPC-DS input change the
selector can execute the edited queries through the original test. The executor
ignores expected SQL, while the optimizer test checks it, so those tests can
receive different decisions for the same edit.

The Dockerfile builds the profiles from the pinned SQLGlot source. The generated
checkpoints and any evaluation patches are external to the submitted repository.
