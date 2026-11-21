import sys, os, json, time
os.environ['MEASURE']='1'; os.environ['SQLGLOT_DIR']='/home/claude/sqlglot'
sys.path.insert(0, '/home/claude/work')
import blast as S
tests = json.load(open('/home/claude/kit/sample/tests.json'))['tests']
r = S.Runner(tests)
pid, fd = r.start({t['number'] for t in tests}, {}, {}, False, time.time() + 3000)
os.waitpid(pid, 0)
res = json.loads(S.read_all(fd))
print('failed', res['failed'], res.get('error'))
json.dump(res['meas'], open('/home/claude/work/cpu_meas.json', 'w'))
