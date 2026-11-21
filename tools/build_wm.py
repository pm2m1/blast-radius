"""Merge weak-mutation profiles (wm.<worker>.<seed>.json) into the map."""
import glob, gzip, json, os, sys, collections

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/wmout"
MAP = sys.argv[2] if len(sys.argv) > 2 else "/home/claude/work/map.json.gz"
tests = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]
ids = [t["id"] for t in tests]
idx = {n: i for i, n in enumerate(ids)}
byclass = collections.defaultdict(list)
cls_of = {}
for i, n in enumerate(ids):
    k = n.rsplit("::", 1)[0]
    cls_of[i] = k
    byclass[k].append(i)

OPS = ["Lt", "LtE", "Gt", "GtE", "Eq", "NotEq", "Is", "IsNot", "In", "NotIn"]
REACHED, CUSTOM, RAISED = 1 << 14, 1 << 13, 1 << 15

flip = collections.defaultdict(lambda: collections.defaultdict(set))  # key -> alt -> tests
reached = collections.defaultdict(set)
imp = collections.defaultdict(int)  # key -> mask of flips at import time
custom = set()
unknown = set()
files = sorted(glob.glob(os.path.join(OUT, "wm.*.json")))
print("profiles:", [os.path.basename(f) for f in files], file=sys.stderr)
for f in files:
    d = json.load(open(f))
    sites = d["sites"]
    for ctx, per in d["flips"].items():
        if ctx in ("IMPORT", ""):
            tests_ = None
        else:
            base, _, phase = ctx.partition("|")
            if base not in idx:
                unknown.add(base)
                continue
            i = idx[base]
            tests_ = set(byclass[cls_of[i]]) if phase else {i}
        for si, mask in per.items():
            rel, line, col, op = sites[int(si)]
            key = "%s:%d:%d" % (rel, line, col)
            if mask & CUSTOM:
                custom.add(key)
            if tests_ is None:
                imp[key] |= mask
                continue
            reached[key] |= tests_
            for j, o in enumerate(OPS):
                if mask & (1 << j):
                    flip[key][o] |= tests_
print("sites reached", len(reached), "with flips", len(flip), "import-time flips", len(imp),
      "custom-eq", len(custom), "unknown ctx", len(unknown), file=sys.stderr)

m = json.load(gzip.open(MAP))
sets, sid = [], {}
def S(s):
    k = tuple(sorted(s))
    if k not in sid:
        sid[k] = len(sets); sets.append(list(k))
    return sid[k]
m["wm"] = {
    "flip": {k: {o: S(v) for o, v in per.items()} for k, per in flip.items()},
    "reached": {k: S(v) for k, v in reached.items()},
    "import": imp,
    "custom": sorted(custom),
    "sets": sets,
    "ids": ids,
}
blob = json.dumps(m, separators=(",", ":")).encode()
gzip.open(MAP, "wb", 9).write(blob)
print("map with wm: raw", len(blob), "gz", os.path.getsize(MAP), file=sys.stderr)
