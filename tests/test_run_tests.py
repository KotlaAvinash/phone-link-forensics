"""Tests of the test harness itself: run_tests.py must never report a check it did not run as passed.

    python tests/test_run_tests.py

Each test runs tests/run_tests.py as a subprocess and checks its output and exit code.
Set PL_UPSTREAM_MAPS to a folder of upstream SQLECmd maps (SQLMap/Maps of a SQLECmd clone)
to also run the passing cross-match case; without it that one test is skipped.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HARNESS = os.path.join(HERE, 'run_tests.py')
OWN_MAPS = sorted(glob.glob(os.path.join(ROOT, 'maps', '*.smap')))


def run(*args):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.run([sys.executable, HARNESS, *args], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=env, timeout=600)
    return p.returncode, p.stdout, p.stderr


def result_line(out):
    lines = [ln for ln in out.splitlines() if ln.startswith('RESULT:')]
    return lines[-1] if lines else ''


class HarnessHonesty(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='harness_test_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertNoTraceback(self, out, err):
        self.assertNotIn('Traceback', out + err, f'harness crashed:\n{err[-2000:]}')

    def test_no_upstream_is_reported_as_skipped(self):
        code, out, err = run()
        self.assertNoTraceback(out, err)
        self.assertEqual(code, 0, out[-2000:])
        self.assertIn('[SKIP] cross-match against upstream maps', out)
        self.assertNotRegex(out, r'\[PASS\][^\n]*exactly one map out of')
        self.assertRegex(result_line(out), r'^RESULT: 0 failure\(s\), [1-9]\d* skipped$')

    def test_empty_upstream_path_fails(self):
        code, out, err = run('--upstream-maps', '')
        self.assertNoTraceback(out, err)
        self.assertNotEqual(code, 0)
        self.assertRegex(out, r"\[FAIL\] upstream maps folder '' does not exist")
        self.assertNotRegex(out, r'\[PASS\][^\n]*exactly one map out of')

    def test_missing_upstream_folder_fails_and_names_it(self):
        missing = os.path.join(self.tmp, 'does', 'not', 'exist')
        code, out, err = run('--upstream-maps', missing)
        self.assertNoTraceback(out, err)
        self.assertNotEqual(code, 0)
        self.assertIn('[FAIL]', out)
        self.assertIn(missing, out)
        self.assertIn('does not exist', out)
        self.assertNotRegex(out, r'\[PASS\][^\n]*exactly one map out of')

    def test_upstream_path_that_is_a_file_fails(self):
        f = os.path.join(self.tmp, 'Maps')
        open(f, 'w').close()
        code, out, err = run('--upstream-maps', f)
        self.assertNoTraceback(out, err)
        self.assertNotEqual(code, 0)
        self.assertIn(f"[FAIL] upstream maps folder '{f}' is not a directory", out)

    def test_too_few_upstream_maps_fails_with_count(self):
        for p in OWN_MAPS[:3]:
            shutil.copy(p, self.tmp)
        code, out, err = run('--upstream-maps', self.tmp)
        self.assertNoTraceback(out, err)
        self.assertNotEqual(code, 0)
        fail = [ln for ln in out.splitlines() if '[FAIL]' in ln and 'too few' in ln]
        self.assertTrue(fail, out[-2000:])
        self.assertIn(self.tmp, fail[0])
        self.assertIn('found 3 ', fail[0])
        self.assertNotRegex(out, r'\[PASS\][^\n]*exactly one map out of')

    def test_cross_match_detects_a_second_matching_map(self):
        # enough maps to pass the count gate, but every one is a renamed copy of ours, so each
        # synthetic database is matched many times: the cross-match itself must catch that
        copies = 0
        while copies < 50:
            for p in OWN_MAPS:
                shutil.copy(p, os.path.join(self.tmp, f'Copy{copies:03d}_{os.path.basename(p)}'))
                copies += 1
        code, out, err = run('--upstream-maps', self.tmp)
        self.assertNoTraceback(out, err)
        self.assertNotEqual(code, 0)
        self.assertIn(f'loaded {copies} upstream maps', out)
        self.assertRegex(out, r'\[FAIL\] each synthetic database matches exactly one map out of \d+[^\n]*more than one')

    def _maps_without(self, key):
        maps = os.path.join(self.tmp, f'maps_without_{key}')
        os.mkdir(maps)
        for p in OWN_MAPS:
            shutil.copy(p, maps)
        victim = os.path.join(maps, os.path.basename(OWN_MAPS[0]))
        with open(victim, encoding='utf-8') as fh:
            m = yaml.safe_load(fh)
        del m[key]
        with open(victim, 'w', encoding='utf-8') as fh:
            yaml.safe_dump(m, fh, sort_keys=False)
        return maps, os.path.basename(victim)

    def test_map_missing_a_key_is_a_clean_failure(self):
        for key in ('Version', 'IdentifyQuery', 'Queries', 'Author'):
            with self.subTest(key=key):
                maps, name = self._maps_without(key)
                code, out, err = run('--maps', maps)
                self.assertNoTraceback(out, err)
                self.assertNotEqual(code, 0)
                self.assertIn(f'[FAIL] {name}: missing key {key}', out)
                self.assertRegex(result_line(out), r'^RESULT: [1-9]\d* failure\(s\)')

    @unittest.skipUnless(os.environ.get('PL_UPSTREAM_MAPS'), 'set PL_UPSTREAM_MAPS to a SQLECmd SQLMap/Maps folder')
    def test_real_upstream_maps_pass_and_report_count(self):
        upstream = os.environ['PL_UPSTREAM_MAPS']
        n = len(glob.glob(os.path.join(upstream, '*.smap')))
        code, out, err = run('--upstream-maps', upstream)
        self.assertNoTraceback(out, err)
        self.assertEqual(code, 0, out[-3000:])
        self.assertIn(f'loaded {n} upstream maps', out)
        self.assertRegex(out, r'\[PASS\][^\n]*exactly one map out of \d+')
        self.assertNotIn('[SKIP] cross-match', out)
        m = re.search(r'exactly one map out of (\d+)', out)
        self.assertGreater(int(m.group(1)), len(OWN_MAPS))


if __name__ == '__main__':
    unittest.main(verbosity=2)
