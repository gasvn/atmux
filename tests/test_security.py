"""Tests for the security/robustness hardening (runtime-dir ownership, PID
verification, scontrol argv-injection guard)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import paths
from autotmux import cli as autotmux


class SecureDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_creates_and_tightens_perms(self):
        d = os.path.join(self.tmp, 'rt')
        os.makedirs(d, mode=0o755)          # too-loose pre-existing dir
        out = paths._secure_dir(d)
        self.assertEqual(out, d)
        self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)

    def test_rejects_symlink(self):
        real = os.path.join(self.tmp, 'real')
        os.makedirs(real)
        link = os.path.join(self.tmp, 'link')
        os.symlink(real, link)
        with self.assertRaises(RuntimeError):
            paths._secure_dir(link)

    def test_rejects_foreign_owner(self):
        d = os.path.join(self.tmp, 'rt2')
        os.makedirs(d, mode=0o700)
        # Simulate a dir owned by someone else.
        fake = os.stat_result((0o040700, 0, 0, 1, os.getuid() + 12345, 0, 0, 0, 0, 0))
        with mock.patch('autotmux.paths.os.lstat', return_value=fake):
            with self.assertRaises(RuntimeError):
                paths._secure_dir(d)


class ScontrolJobIdGuardTests(unittest.TestCase):
    def test_rejects_non_numeric_without_calling_scontrol(self):
        with mock.patch('autotmux.cli.subprocess.check_output') as m:
            for bad in ('-Q', '--yaml', 'abc', '1;2', '', '../x'):
                self.assertIsNone(autotmux.AutotmuxApp._scontrol_job(bad))
            m.assert_not_called()

    def test_accepts_valid_ids(self):
        sample = "JobId=5 JobName=j\n   BatchFlag=1\n   Command=/x\n   WorkDir=/w\n"
        with mock.patch('autotmux.cli.subprocess.check_output', return_value=sample):
            for good in ('123', '123_4', '123_[5-9]'):
                r = autotmux.AutotmuxApp._scontrol_job(good)
                self.assertIsNotNone(r)
                self.assertEqual(r['command'], '/x')


if __name__ == '__main__':
    unittest.main()
